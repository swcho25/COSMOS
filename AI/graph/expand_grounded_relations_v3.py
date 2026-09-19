"""CPU-only relation candidates from all multi-company parent B-include articles.

Writes a NEW development artifact. Existing 72 assertions are preserved with
their original payload; new assertions have explicit v3 provenance. This is
not a recall measurement or training approval. News-level candidate_ready is
not required: both endpoints must be B-include article candidates and an exact
explicit grammatical relationship must prove the relation-specific roles.
Parent news decisions and sentiments are NEVER promoted or overwritten. Dates,
article boundaries, prior unresolved identity and rejected name spans remain
blocking. The unambiguous allowed-type alias dictionary is bound once. Full source
sentences, aliases, hashes, dates and offsets are bound before output. No model,
database update, network request or production publication is performed.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import kg_grounded_relations_v3 as extractor
import kg_target_aliases as alias_policy


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf8')).hexdigest()


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load(path):
    return json.loads(path.read_text(encoding='utf8'))


def readonly(path):
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Source has a nonempty WAL: ' + str(path))
    return sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)


def check_span(raw, start, end, quote):
    assert type(start) is int and type(end) is int and 0 <= start < end <= len(raw)
    assert raw[start:end] == quote


IDENTITY_SPAN_REASONS = frozenset({
    'explicit_name_collision_or_sports_team', 'analyst_affiliation_only',
    'group_or_local_issuer_attribution_unverified', 'historical_name_requires_dated_identity',
    'executive_affiliation_without_target_management_event', 'past_employment_or_biography_requires_review',
})


def endpoint_blocked(pair, prefix, edge, article):
    """Only the actual relation mention is checked, not unrelated article spans."""
    if any(reason.startswith('historical_identity_') for reason in pair['reasons']):
        return 'historical_issuer_identity_pending'
    left, right = edge[prefix + '_start'], edge[prefix + '_end']
    for item in [*pair.get('rejected_mentions', []), *pair.get('unresolved_mentions', [])]:
        if item['reason'] not in IDENTITY_SPAN_REASONS:
            continue
        offset = len(article['title']) + 1 if item['source'] == 'body' else 0
        if item['start'] + offset <= left < right <= item['end'] + offset:
            return item['reason']
    return None


def run(revision, out, limit=None):
    revision, out = revision.resolve(), out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    manifest, protocol = load(revision / 'manifest.json'), load(revision / 'protocol.json')
    assert manifest['artifact_complete'] is True
    parent = Path(protocol['parent_kg']['directory'])
    assert sha(parent / 'manifest.json') == protocol['parent_kg']['manifest_sha256']
    bindings = {name: sha(path) for name, path in {
        'extractor': Path(extractor.__file__), 'runner': Path(__file__),
        'source_manifest': revision / 'manifest.json', 'source_protocol': revision / 'protocol.json',
        'parent_manifest': parent / 'manifest.json',
        'alias_policy': Path(alias_policy.__file__), 'alias_dictionary': alias_policy.ALIAS_FILE,
    }.items()}
    alias_registry = alias_policy._read(str(alias_policy.ALIAS_FILE), bindings['alias_dictionary'])
    snapshots = out / 'source_snapshots'
    snapshots.mkdir()
    for code in (Path(__file__), Path(extractor.__file__), Path(alias_policy.__file__)):
        (snapshots / code.name).write_bytes(code.read_bytes())
    baseline_path = revision / manifest['files']['relationship_evidence']['path']
    assert sha(baseline_path) == manifest['files']['relationship_evidence']['sha256']
    baseline = {r['edge_id']: r for r in map(json.loads, baseline_path.read_text(encoding='utf8').splitlines())}
    source = readonly(parent / 'state.sqlite3')
    reviews = [{'article_id': row[0]} for row in source.execute('SELECT article_id FROM pairs GROUP BY article_id HAVING COUNT(*)>=2 ORDER BY article_id')]
    eligible_articles = len(reviews)
    parent_articles = source.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
    if limit is not None:
        reviews = reviews[:limit]
    assert len({r['article_id'] for r in reviews}) == len(reviews)
    blocked = {(x['article_id'], x['ticker']) for x in protocol['endpoint_policy']['article_companies']}
    merged = dict(baseline)
    new_rows, reasons, patterns, recounted = [], Counter(), Counter(), Counter()
    original_flags = {'graph_quality_validated': False, 'human_gold': False,
                      'ready_for_gnn_training': False, 'production_approved': False,
                      'price_effect_established': False}
    check_counts = Counter()
    for index, review in enumerate(reviews, 1):
        aid = review['article_id']
        source_sha, cleaned_sha, article_text = source.execute('SELECT source_sha,cleaned_sha,payload FROM articles WHERE article_id=?', (aid,)).fetchone()
        article = json.loads(article_text)
        original = {k: article[k] for k in ('article_id', 'title', 'body', 'published_at', 'source_url')}
        assert source_sha == digest(original)
        assert cleaned_sha == digest({'title': article['cleaned_title'], 'body': article['cleaned_body']})
        if not article['date_audit']['usable_day']:
            reasons['invalid_or_epoch_publication_date_article'] += 1
            continue
        if article['cleaning_audit']['quarantine']:
            reasons['article_boundary_ambiguous_article'] += 1
            continue
        raw = article['title'] + '\n' + article['body']
        cleaned = ' '.join((article['cleaned_title'] + '\n' + article['cleaned_body']).split())
        pairs, entities = {}, []
        for ticker, pair_text in source.execute("SELECT ticker,payload FROM pairs WHERE article_id=? ORDER BY ticker", (aid,)):
            pair = json.loads(pair_text)
            assert pair['upstream_decision'] == 'include' and pair['article_id'] == aid
            assert pair['source_input_sha256'] == digest({**original, 'ticker': ticker, 'company_name': pair['company_name']})
            if (aid, ticker) in blocked:
                continue
            pairs[ticker] = pair
            entities.append({'ticker': ticker, 'company_name': pair['company_name'], 'aliases': alias_registry.get(ticker, ())})
        result = extractor.extract_grounded_relations(article['title'], article['body'], entities)
        reasons.update(r['reason'] for r in result['review_candidates'])
        for edge in result['relationships']:
            assert edge['rel_type'] in {'SUPPLY', 'INVEST', 'PARTNER', 'COMPETE'}
            assert edge['src_ticker'] != edge['dst_ticker']
            assert edge['directed'] == (edge['rel_type'] in {'SUPPLY', 'INVEST'})
            check_span(raw, edge['sentence_start'], edge['sentence_end'], edge['sentence'])
            endpoint_validation = {}
            blocked_reasons = []
            for prefix, ticker in [('source', edge['src_ticker']), ('target', edge['dst_ticker'])]:
                assert ticker in pairs
                check_span(raw, edge[prefix + '_start'], edge[prefix + '_end'], edge[prefix + '_quote'])
                assert edge['sentence_start'] <= edge[prefix + '_start'] < edge[prefix + '_end'] <= edge['sentence_end']
                pair = pairs[ticker]
                assert edge[prefix + '_quote'].casefold() in {v.casefold() for v in [pair['company_name'], *alias_registry.get(ticker, ())]}
                block_reason = endpoint_blocked(pair, prefix, edge, article)
                if block_reason:
                    blocked_reasons.append(block_reason)
                endpoint_validation[prefix] = {'ticker': ticker, 'source_input_sha256': pair['source_input_sha256'],
                    'upstream_result_sha256': pair['upstream_result_sha256'],
                    'parent_decision': pair['materialization_decision'], 'parent_reasons': pair['reasons'],
                    'parent_news_decision_unchanged': True, 'parent_sentiment_unchanged': True,
                    'relation_role_proof': 'explicit_literal_grammatical_relation',
                    'alias_dictionary_sha256': bindings['alias_dictionary'],
                    'identity_scope': pair['target_context']['identity_scope'],
                    'historical_resolution': pair['target_context']['historical_resolution']}
            if blocked_reasons:
                reasons.update('endpoint_identity_block:' + r for r in blocked_reasons)
                continue
            normalized = ' '.join(edge['sentence'].split())
            if normalized not in cleaned:
                reasons['relation_sentence_not_retained_in_cleaned_article'] += 1
                continue
            edge_id = 'relationship:' + digest([aid, edge['src_ticker'], edge['dst_ticker'], edge['rel_type'], normalized])
            payload = {**edge, 'edge_id': edge_id, 'article_id': aid, 'news_node': 'news:' + aid,
                       'source_node': 'company:' + edge['src_ticker'], 'target_node': 'company:' + edge['dst_ticker'],
                       'published_at': article['published_at'], 'source_url': article['source_url'],
                       'source_article_sha256': source_sha, 'source_cleaned_sha256': cleaned_sha,
                       'endpoint_validation': endpoint_validation, 'event_time': None, 'event_time_status': 'not_extracted',
                       'offset_basis': 'original_title_newline_body', 'duplicate_evidence_spans': [],
                       'scope': 'expanded_b_include_relation_candidates', 'extraction_version': extractor.POLICY_VERSION,
                       **original_flags}
            recounted[edge['rel_type']] += 1
            if edge_id not in merged:
                merged[edge_id] = payload
                new_rows.append(payload)
                patterns[edge['pattern']] += 1
            check_counts['source_and_endpoint_checks'] += 1
        if index % 5000 == 0:
            print(canonical({'processed': index, 'articles': len(reviews), 'new_edges': len(new_rows), 'elapsed_seconds': round(time.monotonic() - started, 2)}), flush=True)
    source.close()
    assert bindings['extractor'] == sha(Path(extractor.__file__)) and bindings['runner'] == sha(Path(__file__))
    assert bindings['source_manifest'] == sha(revision / 'manifest.json')
    assert bindings['parent_manifest'] == sha(parent / 'manifest.json')
    assert bindings['alias_dictionary'] == sha(alias_policy.ALIAS_FILE)
    files = {}
    groups = defaultdict(list)
    for edge in merged.values():
        groups[(edge['src_ticker'], edge['dst_ticker'], edge['rel_type'], edge['directed'])].append(edge)
    aggregates = [{'src_ticker': s, 'dst_ticker': t, 'source_node': 'company:' + s, 'target_node': 'company:' + t,
                   'rel_type': k, 'directed': d, 'evidence_count': len(g),
                   'news_count': len({e['article_id'] for e in g}),
                   'evidence_count_unit': 'article_specific_normalized_statements_not_independent_events',
                   'scope': 'expanded_b_include_relation_candidates', **original_flags}
                  for (s, t, k, d), g in sorted(groups.items())]
    for name, values in [('relationship_evidence', sorted(merged.values(), key=lambda r:r['edge_id'])),
                         ('new_relationship_evidence', new_rows), ('company_relationships', aggregates)]:
        path = out / (name + '.jsonl')
        with path.open('x', encoding='utf8', newline='\n') as handle:
            for row in values:
                handle.write(canonical(row) + '\n')
        files[name] = {'path': path.name, 'rows': len(values), 'sha256': sha(path), 'bytes': path.stat().st_size}
    final = {'schema': 'cosmos-grounded-relations-v3-expanded-candidates-1', 'artifact_complete': True,
             'full_population_complete': False, 'scope': 'expanded_b_include_relation_candidates',
             'code_and_source_bindings': bindings, 'files': files,
             'parent_kg': protocol['parent_kg'], 'frozen_baseline_revision': str(revision),
             'multi_b_include_articles_processed': len(reviews), 'multi_b_include_articles_available': eligible_articles,
             'parent_articles_considered_by_company_count': parent_articles,
             'baseline_evidence': len(baseline), 'new_evidence': len(new_rows), 'combined_evidence': len(merged),
             'new_type_counts': dict(Counter(e['rel_type'] for e in new_rows)),
             'new_pattern_counts': dict(patterns), 'combined_type_counts': dict(Counter(e['rel_type'] for e in merged.values())),
             'combined_aggregate_counts': dict(Counter(a['rel_type'] for a in aggregates)),
             'review_reason_counts': dict(reasons), 'checks': dict(check_counts),
             'elapsed_seconds': time.monotonic() - started, **original_flags,
             'limitations': ['Nonrandom development recovery; no accuracy or recall estimate.',
                            'News-level candidate_ready is not required; explicit relation roles are required. B include gate retained.',
                            'Parent news relevance and sentiment decisions remain unchanged.',
                            'Historical issuer mapping and present validity remain unverified.',
                            'All baseline evidence preserved; new rows need independent semantic review.']}
    with (out / 'manifest.json').open('x', encoding='utf8') as handle:
        handle.write(canonical(final) + '\n')
    print(canonical(final), flush=True)
    return final


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    run(args.revision, args.out, args.limit)
