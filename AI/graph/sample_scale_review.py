"""Deterministic, outcome-blind semantic review of old quality export candidates.

Selection uses old accepted/rejected membership; review packets never expose it.
The script does not decide semantics, train, inspect prices, or tune any rule.
"""
import argparse
import collections
import csv
import datetime
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / 'artifacts/news_scale_review_20260916'
SEED = 'scale-semantic-review-20260916-v1:'


def rows(path):
    with path.open(encoding='utf8') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def all_record_ids(value):
    if isinstance(value, dict):
        if isinstance(value.get('record_id'), str):
            yield value['record_id']
        for child in value.values():
            yield from all_record_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_record_ids(child)


def indexed_prior_ids():
    """Resolve the old target audit's numeric article indexes, without outcomes."""
    audit = HERE / 'artifacts/news_target_audit_20260915/blind.jsonl'
    articles = HERE / 'artifacts/news_impact_v2/articles.jsonl'
    if not audit.exists() or not articles.exists():
        return set(), {}
    indexes = {row['article'] for row in rows(audit)}
    ids = {row['record_id'] for i, row in enumerate(rows(articles)) if i in indexes}
    assert len(ids) == len(indexes), 'Prior article indexes did not resolve uniquely'
    return ids, {str(p.relative_to(HERE)): sha(p) for p in (audit, articles)}


def prepare(out):
    from target_news_features import clean_target_article
    out.mkdir(parents=True, exist_ok=False)
    artifacts = HERE / 'artifacts'
    source = artifacts / 'news_quality_source_20260915/training_candidates.jsonl'
    quality = artifacts / 'news_quality_dataset_20260915_v2'
    prior = [
        artifacts / 'news_quality_dataset_20260915/human_review.json',
        quality / 'human_review.json',
        artifacts / 'current_event_review_20260915/ai_review.json',
        artifacts / 'current_event_holdout_review_20260915/packet.json',
        artifacts / 'current_event_holdout_review_20260915/ai_review.json',
        artifacts / 'news_target_audit_20260915/blind.jsonl',
    ]
    excluded = set()
    prior_hashes = {}
    for path in prior:
        if not path.exists():
            continue
        prior_hashes[str(path.relative_to(HERE))] = sha(path)
        values = list(rows(path)) if path.suffix == '.jsonl' else json.loads(path.read_text(encoding='utf8'))
        excluded.update(all_record_ids(values))
    indexed_ids, indexed_hashes = indexed_prior_ids()
    excluded.update(indexed_ids)
    prior_hashes.update(indexed_hashes)
    companies_path = HERE.parent / 'ner/data/companies.csv'
    with companies_path.open(encoding='utf-8-sig', newline='') as stream:
        names = {r['ticker']: {'name_official': r['name_official'], 'name_en': r['name_en']} for r in csv.DictReader(stream)}
    membership = {}
    for status, name in [('accepted', 'training_candidates_clean.jsonl'), ('rejected', 'rejected_candidates.jsonl')]:
        for row in rows(quality / name):
            rid = row['record_id']
            assert rid not in membership, rid
            membership[rid] = (status, row['region'], str(row.get('year') or row['published_date'][:4]))
    grouped = collections.defaultdict(list)
    for rid, (status, region, year) in membership.items():
        if rid not in excluded and region in ('domestic', 'overseas') and '2017' <= year <= '2022':
            grouped[(region, year, status)].append(rid)
    selected = {}
    strata = []
    for region in ('domestic', 'overseas'):
        for year in map(str, range(2017, 2023)):
            for status in ('accepted', 'rejected'):
                key = (region, year, status)
                group = sorted(grouped[key], key=lambda rid: hashlib.sha256((SEED + rid).encode()).hexdigest())
                take = group[:10]
                strata.append(dict(region=region, year=year, automatic_membership=status, eligible_after_prior_exclusion=len(group), target=10, selected=len(take)))
                for rid in take:
                    selected[rid] = dict(region=region, year=year, automatic_membership=status)
    packet = []
    for raw in rows(source):
        rid = raw['record_id']
        if rid not in selected:
            continue
        clean, _, _ = clean_target_article(raw)
        source_companies = [dict(ticker=c['ticker'], **names.get(c['ticker'], {'name_official': '', 'name_en': ''})) for c in raw.get('companies', [])]
        packet.append(dict(record_id=rid, region=raw['region'], year=str(raw['published_date'])[:4], published_date=raw['published_date'],
                           source_dataset=raw.get('source_dataset'), title=raw['title'], original_body=raw['body'],
                           clean_title=clean['title'], clean_body=clean['body'], source_companies=source_companies))
    assert len(packet) == len(selected)
    packet.sort(key=lambda r: hashlib.sha256(('blind-order:' + SEED + r['record_id']).encode()).hexdigest())
    for i, row in enumerate(packet, 1):
        row['review_id'] = i
        selected[row['record_id']]['review_id'] = i
    dump(out / 'blind_packet.json', packet)
    dump(out / 'sealed_selection.json', {'strata': strata, 'rows': selected})
    for start in range(0, len(packet), 10):
        dump(out / f'batch_{start // 10 + 1:02d}.json', packet[start:start + 10])
    inputs = [source, quality / 'training_candidates_clean.jsonl', quality / 'rejected_candidates.jsonl', companies_path]
    dump(out / 'protocol.json', dict(
        schema_version=1, review_date='2026-09-16', purpose='Fresh diagnostic sample of automatic accepted and rejected export candidates; no filter tuning',
        source_hashes={str(p.relative_to(HERE.parent.parent)): sha(p) for p in inputs}, previous_review_hashes=prior_hashes,
        previous_review_ids_excluded=len(excluded), selection_seed=SEED,
        selection='Lowest SHA256(seed + record_id), 10 articles per region x year x automatic membership; independent hash shuffle before review',
        target_articles=240, selected_articles=len(packet), unavailable_strata=[s for s in strata if s['selected'] < s['target']],
        original_full_text_preserved=True, rejected_full_text_restored_from_source=True, company_names='Current company dictionary, not certified historical identity',
        withheld=['automatic accepted/rejected membership', 'automatic rejection reason', 'automatic event evidence', 'separate price/outcome/model-prediction datasets', 'metadata payload labels'],
        body_cleaning='Existing clean_target_article applied unchanged to original source; cleaning flags and quarantine decisions hidden',
        review_type='AI-assisted semantic assessment, not human gold', human_review_completed=0,
        decision_unit='Article: supported if at least one source-linked company has a supported current financial event; company details retained',
        definitions={
            'supported': 'Article explicitly reports a current financial/corporate event or current quantified result/forecast of a source-linked company; no price outcome used',
            'unsupported': 'Text does not establish such an event; only background, commentary, named data provider, recommendation/feed, or wrong company',
            'uncertain': 'Entity, recency, event specificity, or available reviewed text does not allow a reliable decision',
        },
        inference_limits=['Balanced diagnostic sample is not source-population precision or recall', 'AI semantic assessment is fallible and not independent human annotation',
                          'No historical corporate identity or first-publication authenticity certification', 'No model performance or accuracy claim'],
        packet_sha256=sha(out / 'blind_packet.json'), code_sha256=sha(Path(__file__)),
    ))
    print(json.dumps({'selected': len(packet), 'prior_ids_excluded': len(excluded), 'unavailable_strata': [s for s in strata if s['selected'] < 10]}, ensure_ascii=False))


def show(out, start, end, compact=False):
    packet = json.loads((out / 'blind_packet.json').read_text(encoding='utf8'))
    for row in packet:
        if not start <= row['review_id'] <= end:
            continue
        print(json.dumps({k: v for k, v in row.items() if k not in ('original_body', 'clean_body', 'clean_title')}, ensure_ascii=False))
        if compact and row['clean_body'] != row['original_body']:
            import difflib
            print('CLEAN FULL TEXT\n' + row['clean_title'] + '\n' + row['clean_body'])
            changes = []
            for tag, i, j, _, _ in difflib.SequenceMatcher(None, row['original_body'], row['clean_body'], autojunk=False).get_opcodes():
                if tag in ('delete', 'replace'):
                    part = row['original_body'][i:j]
                    changes.append(dict(original_start=i, original_end=j, excerpt=part[:1200], omitted_characters=max(0, len(part)-1200)))
            print('ORIGINAL CHANGED REGIONS (bounded excerpts)\n' + json.dumps(changes, ensure_ascii=False))
            continue
        print('ORIGINAL FULL TEXT\n' + row['original_body'])
        if row['clean_body'] != row['original_body'] or row['clean_title'] != row['title']:
            print('CLEAN FULL TEXT\n' + row['clean_title'] + '\n' + row['clean_body'])
        else:
            print('CLEAN TEXT IDENTICAL TO ORIGINAL')


def validate(out):
    """Check all judgments and immutable sources without opening membership."""
    packet = json.loads((out / 'blind_packet.json').read_text(encoding='utf8'))
    protocol = json.loads((out / 'protocol.json').read_text(encoding='utf8'))
    assert sha(out / 'blind_packet.json') == protocol['packet_sha256']
    for relative_path, expected in protocol['source_hashes'].items():
        assert sha(HERE.parent.parent / relative_path) == expected, relative_path
    for relative_path, expected in protocol['previous_review_hashes'].items():
        assert sha(HERE / relative_path) == expected, relative_path
    reviewed = []
    for path in sorted(out.glob('verdicts_*.json')):
        value = json.loads(path.read_text(encoding='utf8'))
        reviewed.extend(value['reviews'] if isinstance(value, dict) else value)
    lookup = {r['review_id']: r for r in packet}
    assert len(reviewed) == len(packet), (len(reviewed), len(packet))
    assert len({r['review_id'] for r in reviewed}) == len(packet)
    for row in reviewed:
        source = lookup[row['review_id']]
        row['record_id'] = source['record_id']
        assert row['verdict'] in ('supported', 'unsupported', 'uncertain')
        assert row['entity_correctness'] in ('correct', 'incorrect', 'mixed', 'uncertain')
        assert row['feed_contamination'] in ('none', 'present', 'uncertain')
        assert row['historical_background'] in ('none', 'present', 'only', 'uncertain')
        assert row['quote'] and row['quote'] in source['title'] + '\n' + source['original_body'] + '\n' + source['clean_body'], (row['review_id'], row['quote'])
        assert row['reason'] and row['reviewed_text_scope']
        assert set(row.get('supported_tickers', [])) <= {c['ticker'] for c in source['source_companies']}, row['review_id']
        row['review_type'] = 'AI-assisted semantic assessment; not human gold'
    reviewed.sort(key=lambda r: r['review_id'])
    return packet, protocol, reviewed


def freeze(out):
    packet, protocol, reviewed = validate(out)
    previous_ids, prior_hashes = indexed_prior_ids()
    overlap = previous_ids & {r['record_id'] for r in packet}
    assert not overlap, 'Sample overlaps old target audit'
    freeze_path = out / 'blind_verdict_freeze.json'
    assert not freeze_path.exists(), 'Do not overwrite a frozen review'
    dump(freeze_path, dict(frozen_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        reviewed_articles=len(reviewed), automatic_membership_seen=False,
        verdict_files={p.name: sha(p) for p in sorted(out.glob('verdicts_*.json'))},
        packet_sha256=protocol['packet_sha256'], sealed_selection_sha256=sha(out / 'sealed_selection.json'),
        final_review_code_sha256=sha(Path(__file__)),
        indexed_prior_review_audit=dict(resolved_articles=len(previous_ids), selected_overlap=len(overlap), source_hashes=prior_hashes,
            note='Original protocol excluded record_id-based prior reviews. Additional numeric-index audit resolved before unmasking; selected overlap zero.'),
        note='All semantic verdicts fixed before opening automatic membership; no filter or model changes informed by this sample.'))
    print(json.dumps({'frozen_articles': len(reviewed), 'prior_indexed_overlap': len(overlap)}))


def finalize(out):
    packet, protocol, reviewed = validate(out)
    frozen = json.loads((out / 'blind_verdict_freeze.json').read_text(encoding='utf8'))
    assert frozen['verdict_files'] == {p.name: sha(p) for p in sorted(out.glob('verdicts_*.json'))}, 'Verdicts changed after freeze'
    assert frozen['packet_sha256'] == sha(out / 'blind_packet.json')
    assert frozen['sealed_selection_sha256'] == sha(out / 'sealed_selection.json')
    assert frozen['final_review_code_sha256'] == sha(Path(__file__))
    dump(out / 'ai_review.json', dict(review_date='2026-09-16', human_gold=False, human_review_completed=0,
        separate_price_outcome_or_prediction_dataset_seen=False,
        source_article_may_contain_embedded_quote_widgets_or_publisher_predictions=True,
        embedded_feed_used_as_event_evidence=False, automatic_membership_seen_during_review=False, for_rule_tuning=False,
        packet_sha256=protocol['packet_sha256'], reviews=reviewed))
    selection = json.loads((out / 'sealed_selection.json').read_text(encoding='utf8'))
    counts = collections.defaultdict(collections.Counter)
    for row in reviewed:
        meta = selection['rows'][row['record_id']]
        counts[(meta['region'], meta['year'], meta['automatic_membership'])][row['verdict']] += 1
    strata = [dict(region=k[0], year=k[1], automatic_membership=k[2], n=sum(v.values()), **{s: v[s] for s in ('supported', 'unsupported', 'uncertain')}) for k, v in sorted(counts.items())]
    characteristics = {field: dict(collections.Counter(r[field] for r in reviewed)) for field in ('entity_correctness', 'feed_contamination', 'historical_background', 'reviewed_text_scope')}
    result = dict(reviewed_articles=len(reviewed), human_review_completed=0, strata=strata, sample_characteristics=characteristics,
                  unavailable_strata=protocol['unavailable_strata'], pooled_population_precision=None, population_recall=None,
                  caveat='Equal-sized diagnostic strata; unweighted pooled ratios are not population precision. Rejected sample support is not overall recall.',
                  freeze_sha256=sha(out / 'blind_verdict_freeze.json'), code_sha256=sha(Path(__file__)),
                  ai_review_sha256=sha(out / 'ai_review.json'))
    dump(out / 'summary.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--stage', choices=('prepare', 'show', 'validate', 'freeze', 'finalize'), required=True)
    parser.add_argument('--start', type=int, default=1)
    parser.add_argument('--end', type=int, default=10)
    parser.add_argument('--compact', action='store_true', help='Full clean text plus bounded original changed-region excerpts; preserve full originals in packet')
    args = parser.parse_args()
    {'prepare': lambda: prepare(args.out), 'show': lambda: show(args.out, args.start, args.end, args.compact),
     'validate': lambda: print(json.dumps({'validated_articles': len(validate(args.out)[2])})),
     'freeze': lambda: freeze(args.out), 'finalize': lambda: finalize(args.out)}[args.stage]()
