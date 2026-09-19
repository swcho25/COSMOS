"""Resumable CPU scan of broader literal relation evidence in frozen B inputs.

Keeps the existing v4 candidate payloads byte-for-byte semantically unchanged.
V5 expands textual coverage, never the news/sentiment decision or company
identity policy. All candidates require contextual review before integration.
The scan is local and read-only on source artifacts. A SQLite transaction per
article stores both new edges and counters, so restart cannot double-count.
STOP is checked between articles. Source/code hashes are bound at initialization
and rechecked before resume/completion; completed artifacts are never rewritten.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3
import time

import kg_grounded_relations_v5 as extractor
import kg_target_aliases as aliases
from expand_grounded_relations_v3 import canonical, digest, sha, load, readonly, check_span, endpoint_blocked


FLAGS = dict(graph_quality_validated=False, human_gold=False, production_approved=False,
             ready_for_gnn_training=False, price_effect_established=False,
             contextual_review_required=True)


def validate_edge(article, edge, pairs, registry, source_sha, cleaned_sha, blocked):
    """Return an auditable candidate or explicit identity/cleaning block reason."""
    raw = article['title'] + '\n' + article['body']
    assert edge['rel_type'] in {'SUPPLY', 'INVEST', 'PARTNER', 'COMPETE'}
    assert edge['src_ticker'] != edge['dst_ticker']
    assert edge['directed'] == (edge['rel_type'] in {'SUPPLY', 'INVEST'})
    check_span(raw, edge['sentence_start'], edge['sentence_end'], edge['sentence'])
    validations = {}
    for prefix, ticker in [('source', edge['src_ticker']), ('target', edge['dst_ticker'])]:
        assert ticker in pairs
        if (article['article_id'], ticker) in blocked:
            return None, 'frozen_endpoint_identity_block'
        pair = pairs[ticker]
        left, right, quote = (edge[prefix + '_' + part] for part in ('start', 'end', 'quote'))
        check_span(raw, left, right, quote)
        assert edge['sentence_start'] <= left < right <= edge['sentence_end']
        assert quote.casefold() in {x.casefold() for x in [pair['company_name'], *registry.get(ticker, ())]}
        reason = endpoint_blocked(pair, prefix, edge, article)
        if reason:
            return None, 'endpoint_identity_block:' + reason
        validations[prefix] = dict(ticker=ticker, source_input_sha256=pair['source_input_sha256'],
            upstream_result_sha256=pair['upstream_result_sha256'], parent_decision=pair['materialization_decision'],
            parent_reasons=pair['reasons'], parent_news_decision_unchanged=True, parent_sentiment_unchanged=True,
            relation_role_proof='literal_clause_or_explicit_bounded_coreference',
            identity_scope=pair['target_context']['identity_scope'],
            historical_resolution=pair['target_context']['historical_resolution'])
    derivation_keys = ('expansion_mode','relation_clause_span','anchor_spans',
                       'collective_role_participants','assertion_scope','pattern')
    # The existing integrator carries endpoint_validation through to GraphRAG.
    # Keep the antecedent and clause proof there, rather than dropping it.
    validations['extraction'] = {k:edge[k] for k in derivation_keys if k in edge}
    for span in [*edge.get('anchor_spans',[]), *([edge['relation_clause_span']] if edge.get('relation_clause_span') else [])]:
        check_span(raw,span['start'],span['end'],span['quote'])
        assert edge['sentence_start'] <= span['start'] < span['end'] <= edge['sentence_end']
    normalized = ' '.join(edge['sentence'].split())
    cleaned = ' '.join((article['cleaned_title'] + '\n' + article['cleaned_body']).split())
    if normalized not in cleaned:
        return None, 'evidence_not_retained_in_cleaned_article'
    aid = article['article_id']
    edge_id = 'relationship:' + digest([aid, edge['src_ticker'], edge['dst_ticker'], edge['rel_type'], normalized])
    return {**edge, 'edge_id': edge_id, 'article_id': aid, 'news_node': 'news:' + aid,
        'source_node': 'company:' + edge['src_ticker'], 'target_node': 'company:' + edge['dst_ticker'],
        'published_at': article['published_at'], 'source_url': article['source_url'],
        'source_article_sha256': source_sha, 'source_cleaned_sha256': cleaned_sha,
        'endpoint_validation': validations, 'event_time': None, 'event_time_status': 'not_extracted',
        'offset_basis': 'original_title_newline_body', 'duplicate_evidence_spans': [],
        'scope': 'v5_expanded_literal_relation_candidates', **FLAGS}, None


def atomic_json(path, obj):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(canonical(obj) + '\n', encoding='utf8')
    temp.replace(path)


def run(baseline, identity_revision, out, limit=None, resume=False):
    baseline, identity_revision, out = (p.resolve() for p in (baseline, identity_revision, out))
    assert limit is None or limit > 0
    frozen, identity = load(baseline/'manifest.json'), load(identity_revision/'protocol.json')
    assert frozen['artifact_complete'] is True
    parent_binding = frozen['parent_kg']
    assert parent_binding == identity['parent_kg']
    parent = Path(parent_binding['directory']).resolve()
    assert sha(parent/'manifest.json') == parent_binding['manifest_sha256']
    assert sha(parent/'protocol.json') == parent_binding['protocol_sha256']
    spec = frozen['files']['relationship_evidence']
    source_edges = baseline/spec['path']
    assert sha(source_edges) == spec['sha256']
    with source_edges.open(encoding='utf8') as stream:
        originals = {r['edge_id']: r for r in map(json.loads, stream)}
    assert len(originals) == spec['rows']
    paths = {'runner': Path(__file__), 'v5': Path(extractor.__file__),
        'v4': Path(__file__).with_name('kg_grounded_relations_v4.py'),
        'v3': Path(__file__).with_name('kg_grounded_relations_v3.py'),
        'v2': Path(__file__).with_name('kg_grounded_relations_v2.py'),
        'v1': Path(__file__).with_name('kg_grounded_relations.py'),
        'shared_checks': Path(__file__).with_name('expand_grounded_relations_v3.py'),
        'alias_policy': Path(aliases.__file__), 'alias_dictionary': aliases.ALIAS_FILE,
        'baseline_manifest': baseline/'manifest.json', 'identity_protocol': identity_revision/'protocol.json',
        'parent_manifest': parent/'manifest.json', 'parent_protocol': parent/'protocol.json',
        'parent_database': parent/'state.sqlite3', 'baseline_evidence': source_edges}
    binding = {'schema': 'cosmos-v5-relation-scan-protocol-1', 'source_paths': {k: str(v) for k,v in paths.items()},
        'hashes': {k: sha(v) for k,v in paths.items()}, 'parent_kg': parent_binding,
        'baseline_directory': str(baseline), 'limit': limit,
        'endpoint_policy': 'existing_B_include_plus_frozen_v4_payloads_only',
        'new_endpoint_restoration': False, 'source_mutation': False}
    assert binding['hashes']['parent_database'] == parent_binding['state_sqlite_sha256']
    assert not Path(str(parent/'state.sqlite3')+'-wal').exists() or Path(str(parent/'state.sqlite3')+'-wal').stat().st_size==0
    if resume:
        assert out.is_dir() and load(out/'protocol.json') == binding, 'Resume source/code/config differs'
        if (out/'manifest.json').exists():
            complete = load(out/'manifest.json')
            assert complete['artifact_complete'] is True
            for entry in complete['files'].values():
                assert sha(out/entry['path']) == entry['sha256']
            return complete
    else:
        out.mkdir(parents=True, exist_ok=False)
        atomic_json(out/'protocol.json', binding)
        snapshots = out/'source_snapshots'; snapshots.mkdir()
        for key in ('runner', 'v5', 'v4', 'v3', 'v2', 'v1', 'shared_checks', 'alias_policy'):
            (snapshots/paths[key].name).write_bytes(paths[key].read_bytes())
    source = readonly(parent/'state.sqlite3')
    state = sqlite3.connect(out/'scan.sqlite3')
    state.execute('PRAGMA journal_mode=WAL')
    state.execute('PRAGMA synchronous=NORMAL')
    state.executescript('CREATE TABLE IF NOT EXISTS done(article_id TEXT PRIMARY KEY, counters TEXT NOT NULL, receipt TEXT NOT NULL);'
                       'CREATE TABLE IF NOT EXISTS edges(edge_id TEXT PRIMARY KEY, article_id TEXT NOT NULL, payload TEXT NOT NULL);'
                       'CREATE INDEX IF NOT EXISTS edges_by_article ON edges(article_id);')
    registry = aliases._read(str(aliases.ALIAS_FILE), binding['hashes']['alias_dictionary'])
    blocked = {(x['article_id'], x['ticker']) for x in identity['endpoint_policy']['article_companies']}
    ids = [x[0] for x in source.execute('SELECT article_id FROM pairs GROUP BY article_id HAVING COUNT(*)>=2 ORDER BY article_id')]
    available = len(ids); population = source.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
    if limit: ids = ids[:limit]
    done = {x[0] for x in state.execute('SELECT article_id FROM done')}
    assert done <= set(ids)
    if resume:
        verify_checkpoint(state,source,registry,blocked)
    started = time.monotonic(); count = len(done)
    try:
        for aid in ids:
            if aid in done: continue
            if (out/'STOP').exists():
                atomic_json(out/'progress.json', dict(status='paused', processed=count, articles=len(ids)))
                return {'status': 'paused', 'processed': count, 'articles': len(ids)}
            source_sha, cleaned_sha, payload = source.execute('SELECT source_sha,cleaned_sha,payload FROM articles WHERE article_id=?',(aid,)).fetchone()
            article = json.loads(payload); original = {k: article[k] for k in ('article_id','title','body','published_at','source_url')}
            assert source_sha == digest(original)
            assert cleaned_sha == digest({'title':article['cleaned_title'],'body':article['cleaned_body']})
            counts = Counter(); candidates = []
            if not article['date_audit']['usable_day'] or article['cleaning_audit']['quarantine']:
                counts['blocked_article_date_or_boundary'] += 1
            else:
                pairs, entities = {}, []
                for ticker, text in source.execute('SELECT ticker,payload FROM pairs WHERE article_id=? ORDER BY ticker',(aid,)):
                    pair = json.loads(text)
                    assert pair['upstream_decision']=='include' and pair['article_id']==aid
                    assert pair['source_input_sha256']==digest({**original,'ticker':ticker,'company_name':pair['company_name']})
                    if (aid,ticker) in blocked: continue
                    pairs[ticker]=pair
                    entities.append({'ticker':ticker,'company_name':pair['company_name'],'aliases':registry.get(ticker,())})
                result = extractor.extract_grounded_relations(article['title'], article['body'], entities)
                counts.update('review:'+r['reason'] for r in result['review_candidates'])
                for edge in result['relationships']:
                    checked, reason = validate_edge(article,edge,pairs,registry,source_sha,cleaned_sha,blocked)
                    if reason: counts[reason]+=1; continue
                    if checked['edge_id'] in originals: continue
                    for prefix in ('source','target'):
                        checked['endpoint_validation'][prefix]['alias_dictionary_sha256']=binding['hashes']['alias_dictionary']
                    candidates.append(checked)
            with state:
                for edge in candidates:
                    inserted = state.execute('INSERT OR IGNORE INTO edges VALUES (?,?,?)',(edge['edge_id'],aid,canonical(edge))).rowcount
                    if inserted:
                        counts['new:'+edge['rel_type']]+=1
                        counts['pattern:'+edge['pattern']]+=1
                stored=[json.loads(x[0]) for x in state.execute('SELECT payload FROM edges WHERE article_id=? ORDER BY edge_id',(aid,))]
                receipt=digest({'article_id':aid,'counters':dict(counts),'source_sha':source_sha,'edges':stored})
                state.execute('INSERT INTO done VALUES (?,?,?)',(aid,canonical(dict(counts)),receipt))
            count += 1
            if count%2000==0:
                progress=dict(status='running',processed=count,articles=len(ids),new_evidence=state.execute('SELECT COUNT(*) FROM edges').fetchone()[0],elapsed_seconds=round(time.monotonic()-started,2))
                atomic_json(out/'progress.json',progress); print(canonical(progress),flush=True)
        totals = Counter()
        for row in state.execute('SELECT counters FROM done'): totals.update(json.loads(row[0]))
        new = [json.loads(x[0]) for x in state.execute('SELECT payload FROM edges ORDER BY edge_id')]
        assert len(ids)==state.execute('SELECT COUNT(*) FROM done').fetchone()[0]
    finally:
        source.close(); state.close()
    for key,path in paths.items(): assert sha(path)==binding['hashes'][key], 'Bound input changed during scan: '+key
    combined = {**originals, **{r['edge_id']:r for r in new}}
    assert all(combined[k]==v for k,v in originals.items())
    groups=defaultdict(list)
    for row in combined.values(): groups[(row['src_ticker'],row['dst_ticker'],row['rel_type'],row['directed'])].append(row)
    aggregates=[dict(src_ticker=s,dst_ticker=t,rel_type=k,directed=d,evidence_count=len(rows),news_count=len({r['article_id'] for r in rows}),**FLAGS) for (s,t,k,d),rows in sorted(groups.items())]
    files={}
    for name,rows in [('relationship_evidence',sorted(combined.values(),key=lambda x:x['edge_id'])),('new_relationship_evidence',new),('company_relationships',aggregates)]:
        path=out/(name+'.jsonl')
        with path.open('w',encoding='utf8',newline='\n') as stream:
            for row in rows: stream.write(canonical(row)+'\n')
        files[name]=dict(path=path.name,rows=len(rows),bytes=path.stat().st_size,sha256=sha(path))
    result=dict(schema='cosmos-grounded-relations-v5-candidates-1',artifact_complete=True,parent_kg=parent_binding,
        protocol_sha256=sha(out/'protocol.json'),files=files,baseline_evidence=len(originals),new_evidence=len(new),combined_evidence=len(combined),
        multi_b_include_articles_processed=len(ids),multi_b_include_articles_available=available,parent_articles_considered_by_company_count=population,
        full_population_complete=False,eligible_scan_complete=len(ids)==available,counts=dict(totals),
        combined_aggregate_counts=dict(Counter(r['rel_type'] for r in aggregates)),
        limitations=['Provisional literal candidates; contextual review required for every new assertion.',
                     'B include endpoint restriction remains; no general issuer restoration.',
                     'Precision/recall not measured against human gold; original news and sentiments unchanged.'],**FLAGS)
    atomic_json(out/'manifest.json',result); atomic_json(out/'progress.json',dict(status='complete',processed=len(ids),articles=len(ids),new_evidence=len(new)))
    print(canonical({k:result[k] for k in ('artifact_complete','new_evidence','combined_evidence','combined_aggregate_counts')}),flush=True)
    return result


def verify_checkpoint(state,source,registry,blocked):
    """Do not trust mutable resume rows as prevalidated completed work."""
    assert state.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    assert not state.execute('SELECT edge_id FROM edges WHERE article_id NOT IN (SELECT article_id FROM done) LIMIT 1').fetchall()
    for aid,encoded_counts,receipt in state.execute('SELECT article_id,counters,receipt FROM done ORDER BY article_id'):
        counts=json.loads(encoded_counts)
        source_sha,cleaned_sha,text=source.execute('SELECT source_sha,cleaned_sha,payload FROM articles WHERE article_id=?',(aid,)).fetchone()
        records=state.execute('SELECT edge_id,article_id,payload FROM edges WHERE article_id=? ORDER BY edge_id',(aid,)).fetchall()
        rows=[json.loads(x[2]) for x in records]
        assert receipt==digest({'article_id':aid,'counters':counts,'source_sha':source_sha,'edges':rows}), 'Resume receipt differs'
        expected=Counter()
        if rows:
            article=json.loads(text)
            pairs={t:json.loads(p) for t,p in source.execute('SELECT ticker,payload FROM pairs WHERE article_id=?',(aid,))}
            for record,row in zip(records,rows):
                assert record[:2]==(row['edge_id'],aid) and row['article_id']==aid
                assert row['source_article_sha256']==source_sha and row['source_cleaned_sha256']==cleaned_sha
                checked,reason=validate_edge(article,row,pairs,registry,source_sha,cleaned_sha,blocked)
                assert reason is None and checked['edge_id']==row['edge_id']
                expected['new:'+row['rel_type']]+=1; expected['pattern:'+row['pattern']]+=1
        assert {k:v for k,v in counts.items() if k.startswith(('new:','pattern:'))}==dict(expected)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,required=True); p.add_argument('--identity-revision',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True); p.add_argument('--limit',type=int); p.add_argument('--resume',action='store_true')
    args=p.parse_args(); run(args.baseline,args.identity_revision,args.out,args.limit,args.resume)
