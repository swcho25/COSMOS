"""Nested supervised-data learning curve, without changing the price task.

Use one financial-keyword source cohort, the existing weak same-sentence quality
policy, literal entity checks and observed-before-news prices at EVERY size.
Do not use the much narrower current_event_gate or tune filters on review cases.
News counts refer to eligible unique articles, not raw HDFS rows or target pairs.
All sizes share frozen encoders, legacy train PCA, annual historical graphs and
2023 validation targets. Thus this measures additional supervised examples given
those common resources, not scaling every source of information in the model.
The already reused validation is exploratory; never describe it as a fresh test.
"""
import argparse
import collections
import gc
import hashlib
import json
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from experiment_current_events import rows, save_rows, load_data as load_event_data
from news_impact_data import Labels, availability, prices, mention_array
from news_quality import text_digest, financial_evidence, audit_timestamp
from target_news_features import clean_target_article, valid_target_hit, CompanyMatcher, TargetEncoder
from verify_news_predictive_value import sha, dump

HERE = Path(__file__).resolve().parent
ORIGINAL = HERE / 'artifacts/news_impact_v2'
FEATURES = HERE / 'artifacts/news_target_experiment_20260915'
GRAPHS = HERE / 'artifacts/news_current_events_20260915_v2'
OLD_METADATA = HERE / 'artifacts/news_quality_source_20260915/legacy_metadata.jsonl'
REQUESTED_SIZES = (20000, 50000, 100000)


def body_keys(raw, clean):
    return {text_digest(raw.get('body')), text_digest(clean.get('body'))} - {text_digest('')}


def labeler():
    meta = pd.read_json(ORIGINAL/'companies.json', dtype={'ticker': str})
    benchmarks = {}
    for market in ('KOSPI', 'NASDAQ'):
        f = pd.read_csv(ORIGINAL/f'index_{market}.csv')
        benchmarks[market] = pd.Series(f.Close.to_numpy(), index=pd.to_datetime(f.Date.str[:10]))
    labels = Labels(prices(), meta, benchmarks)
    first = {}
    for panel, _, closes in labels.panels.values():
        for ticker in panel:
            values = panel[ticker].to_numpy()
            valid = np.flatnonzero(np.isfinite(values) & (values > 0))
            if len(valid):
                first[ticker] = closes[valid[0]]
    return labels, first, meta


def make_pool(source, out):
    """Stream full raw export; reject overlap before making target labels."""
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    source = source.resolve()
    assert (source/'manifest.json').is_file(), 'Require completed, hashed remote export'
    export_manifest = json.loads((source/'manifest.json').read_text(encoding='utf8'))
    assert export_manifest['complete'] is True, 'Remote export did not complete'
    assert sha(source/'training_candidates.jsonl') == export_manifest['training_candidates_sha256'], 'Transferred candidates differ from completed remote export'
    files = [source/'training_candidates.jsonl', source/'manifest.json', OLD_METADATA,
             ORIGINAL/'articles.jsonl', ORIGINAL/'pairs.parquet', ORIGINAL/'aliases.csv',
             ORIGINAL/'index_KOSPI.csv', ORIGINAL/'index_NASDAQ.csv', HERE/'data/prices_daily.parquet']
    files += [source/n for n in ('protocol.json', 'runtime.json', 'schema.json',
              'source_counts.json', 'recovery_plan.json', 'recovery_audit.json',
              'reuse_plan.json', 'reuse_audit.json') if (source/n).is_file()]
    files += [ORIGINAL/n for n in ('companies.json', 'label_config.npz')]
    files += [FEATURES/n for n in ('direct_mask.npy', 'feature_mapping.npy', 'mentions.npy',
              'target_text.npy', 'target_sentiment.npy', 'target_projection.npz', 'feature_manifest.json',
              'training_inputs/pretrained.json')]
    files += [GRAPHS/'graphs.json']
    specs = json.loads((FEATURES/'training_inputs/pretrained.json').read_text())
    for role in ('embedding', 'sentiment_ko', 'sentiment_en'):
        model_path = Path(specs[role]['path'])
        if not model_path.is_absolute():
            model_path = FEATURES/'training_inputs'/model_path
        files.extend(p for p in model_path.rglob('*') if p.is_file())
    protocol = dict(source=str(source), source_hashes={str(p): sha(p) for p in files},
                    code_hashes={n: sha(HERE/n) for n in (
                        'experiment_news_scale.py', 'news_quality.py', 'target_news_features.py',
                        'news_content.py', 'news_impact_data.py', 'news_text_features.py',
                        'news_company_context.py', '../ner/matcher.py')},
                    requested_sizes=list(REQUESTED_SIZES), source_years=[2017, 2022],
                    quality_policy='existing hygiene + literal entity + weak same-sentence finance evidence; no current_event_gate',
                    selection_outcomes_used=False, original_validation_unchanged=True,
                    historical_identity_certified=False, source_time_certified=False,
                    human_review_completed=0, automatic_model_replacement=False)
    dump(out/'pool_protocol.json', protocol)
    held_ids, held_bodies, held_urls = set(), set(), set()
    metadata = {r['record_id']: r for r in rows(OLD_METADATA)}
    for d in rows(ORIGINAL/'articles.jsonl'):
        if d['split'] in ('val', 'test'):
            held_ids.add(d['record_id'])
            held_bodies.update(body_keys(d, clean_target_article(d)[0]))
            url = metadata[d['record_id']].get('canonical_url')
            if url:
                held_urls.add(url)
    del metadata
    labels, first, meta = labeler()
    universe = set(meta.ticker)
    matcher = CompanyMatcher.from_csv(ORIGINAL/'aliases.csv')
    seen_ids, seen_bodies, seen_urls = set(), set(), set()
    counts, cohorts = collections.Counter(), collections.Counter()
    kept = total = 0
    with (out/'eligible_pool.jsonl').open('x', encoding='utf8') as good, (out/'pool_exclusions.jsonl').open('x', encoding='utf8') as bad:
        for total, raw in enumerate(rows(source/'training_candidates.jsonl'), 1):
            rid = raw['record_id']; region = raw['region']; date = str(raw['published_date'])[:10]
            assert region in ('domestic', 'overseas')
            clean, flags, quarantine = clean_target_article(raw)
            keys = body_keys(raw, clean); url = raw.get('canonical_url')
            reason = None
            if not ('2017-01-01' <= date <= '2022-12-31'):
                reason = 'outside_training_period'
            elif rid in held_ids or keys & held_bodies or (url and url in held_urls):
                reason = 'holdout_overlap'
            elif rid in seen_ids or keys & seen_bodies or (url and url in seen_urls):
                reason = 'candidate_duplicate'
            elif quarantine:
                reason = 'feed_quarantine'
            elif len(clean.get('body', '').strip()) < 200:
                reason = 'short_clean_body'
            hits, evidence, targets = [], [], {}
            public_time = availability(date, region)
            if reason is None:
                allowed = {m['ticker'] for m in raw['companies']} & universe
                full = clean['title'] + '\n' + clean['body']
                hits = [dict(ticker=m.ticker, start=m.start, end=m.end)
                        for m in matcher.match(clean['title'], clean['body']).mentions
                        if m.confidence >= 1 and m.ticker in allowed
                        and valid_target_hit(full, m.ticker, m.start, m.end)]
                if not hits:
                    reason = 'no_verified_company'
                else:
                    evidence = financial_evidence(clean['title'], clean['body'], hits)
                    if not evidence:
                        reason = 'no_same_sentence_event'
            if reason is None:
                # Apply history guard to ALL text inputs, even unlabelled nodes.
                eligible_tickers = {h['ticker'] for h in hits
                                    if h['ticker'] in first and first[h['ticker']] <= public_time}
                hits = [h for h in hits if h['ticker'] in eligible_tickers]
                # A removed pre-listing company cannot supply the only finance
                # evidence for unrelated surviving inputs. Reapply the same rule.
                evidence = financial_evidence(clean['title'], clean['body'], hits)
                if not evidence:
                    reason = 'no_finance_evidence_after_history_guard'
                else:
                    for ticker in sorted(eligible_tickers):
                        value = labels.label(ticker, public_time, 'train')
                        if value is not None:
                            targets[ticker] = value
                    if not targets:
                        reason = 'no_prior_price_or_valid_target'
            if reason:
                counts[(region, date[:4], reason)] += 1
                bad.write(json.dumps(dict(record_id=rid, region=region, year=date[:4], reason=reason), ensure_ascii=False)+'\n')
            else:
                seen_ids.add(rid); seen_bodies.update(keys)
                if url:
                    seen_urls.add(url)
                companies = []
                for ticker in sorted({h['ticker'] for h in hits}):
                    own = [h for h in hits if h['ticker'] == ticker]; pos = min(h['start'] for h in own)
                    companies.append(dict(ticker=ticker, n_mentions=len(own),
                        first_pos='title' if pos < len(clean['title']) else ('lead' if pos < len(clean['title'])+201 else 'body'),
                        confidence=1., method='scale_verified_dictionary'))
                record = dict(record_id=rid, title=clean['title'], body=clean['body'], region=region,
                    published_date=date, available_at=public_time.isoformat(), graph=date[:4], split='train',
                    companies=companies, literal_mentions=hits, target_labels=targets,
                    source_url=raw.get('url'), canonical_url=url, source_dataset=raw.get('source_dataset'),
                    publication_time_audit=audit_timestamp(raw), quality_flags=flags,
                    raw_body_hash=text_digest(raw.get('body')), clean_body_hash=text_digest(clean.get('body')),
                    sample_key=hashlib.sha256(('scale-supervision-v1:'+rid).encode()).hexdigest())
                good.write(json.dumps(record, ensure_ascii=False)+'\n')
                kept += 1; cohorts[(region, date[:4])] += 1
            if total % 5000 == 0:
                print(json.dumps(dict(stage='pool', examined=total, eligible=kept)), flush=True)
    assert total == export_manifest['training_candidates'], 'Export row count differs from manifest'
    for path, digest in protocol['source_hashes'].items():
        assert sha(Path(path)) == digest, path
    summary = dict(raw_candidates=total, eligible_articles=kept,
        cohorts=[dict(region=r, year=y, articles=n) for (r, y), n in sorted(cohorts.items())],
        exclusions=[dict(region=r, year=y, reason=z, articles=n) for (r, y, z), n in sorted(counts.items())],
        seconds=time.monotonic()-started, source_unchanged=True,
        eligible_pool_sha256=sha(out/'eligible_pool.jsonl'))
    dump(out/'pool_completion.json', summary)
    print(json.dumps(summary), flush=True)


def check_pool_inputs(out):
    protocol = json.loads((out/'pool_protocol.json').read_text())
    for path, digest in protocol['source_hashes'].items():
        assert sha(Path(path)) == digest, path
    for name, digest in protocol['code_hashes'].items():
        assert sha(HERE/name) == digest, name


def nested_order(records):
    """Fixed proportional interleaving: every smaller budget is a prefix."""
    groups = collections.defaultdict(list)
    for i, row in enumerate(records):
        groups[row['region'], row['published_date'][:4]].append(i)
    ranked = []
    for key, ids in sorted(groups.items()):
        ids.sort(key=lambda i: (records[i]['sample_key'], records[i]['record_id']))
        ranked.extend(((j+.5)/len(ids), records[i]['sample_key'], i) for j, i in enumerate(ids))
    return np.array([i for _, _, i in sorted(ranked)], dtype='int64')


def prepare(out):
    assert not (out/'prepared.json').exists()
    check_pool_inputs(out)
    pool_done = json.loads((out/'pool_completion.json').read_text())
    assert sha(out/'eligible_pool.jsonl') == pool_done['eligible_pool_sha256']
    pool = list(rows(out/'eligible_pool.jsonl'))
    if len(pool) < 20000:
        raise ValueError(f'Only {len(pool)} eligible articles; do not label a smaller run 20k')
    sizes = [n for n in REQUESTED_SIZES if n <= len(pool)]
    if len(pool) < max(REQUESTED_SIZES) and len(pool) not in sizes:
        sizes.append(len(pool))
    max_size = min(max(REQUESTED_SIZES), len(pool)); order = nested_order(pool)[:max_size]
    selected = [pool[i] for i in order]
    del pool
    meta = pd.read_json(ORIGINAL/'companies.json', dtype={'ticker': str})
    tickers = meta.ticker.tolist(); node = {t: i for i, t in enumerate(tickers)}
    original_docs = list(rows(ORIGINAL/'articles.jsonl'))
    original_pairs = pd.read_parquet(ORIGINAL/'pairs.parquet')
    old_mask = np.load(FEATURES/'direct_mask.npy'); old_map = np.load(FEATURES/'feature_mapping.npy')
    old_mentions = np.load(FEATURES/'mentions.npy')
    val_rows = np.flatnonzero(original_pairs.split.eq('val') & original_pairs.is_direct)
    assert len(val_rows) == 4813
    val_ids = sorted(set(original_pairs.iloc[val_rows].article)); remap = {a: j for j, a in enumerate(val_ids)}
    docs = [clean_target_article(original_docs[i])[0] for i in val_ids]
    mask = np.zeros((len(docs)+max_size, len(tickers)), dtype=bool)
    mapping = np.zeros(mask.shape, dtype='int64'); mentions = np.zeros((*mask.shape, 5), dtype='float32')
    mapping[:len(docs)] = old_map[val_ids]; mentions[:len(docs)] = old_mentions[val_ids]
    for j, i in enumerate(val_ids):
        mask[j] = old_mask[i]
        if not mask[j].any():
            docs[j]['split'] = 'excluded'
    pairs = original_pairs.iloc[val_rows].copy()
    pairs['original_pair_index'] = val_rows
    pairs['article'] = pairs.article.map(remap)
    # Compact dataset deliberately remaps article indices; original price targets
    # and validation identity are copied exactly and saved with their old indices.
    n_val = len(docs); entries = []; new_pairs = []
    offset = len(np.load(FEATURES/'target_text.npy', mmap_mode='r'))
    for ordinal, item in enumerate(selected):
        i = len(docs)
        targets = item.pop('target_labels'); hits = item.pop('literal_mentions')
        item['supervised_rank'] = ordinal
        for company in item['companies']:
            ticker = company['ticker']; j = node[ticker]
            mapping[i, j] = offset + len(entries)
            entries.append(dict(article=i, ticker=ticker, hits=hits))
            if ticker in targets:
                mask[i, j] = True
                new_pairs.append(dict(article=i, news_id=item['record_id'], ticker=ticker, split='train',
                    graph=item['graph'], is_direct=True, path_length=0, rule_weight=1., rule_sign=1.,
                    rule_source=ticker, original_pair_index=-1, **targets[ticker]))
        mentions[i] = mention_array(item['companies'], tickers)
        assert mask[i].any()
        docs.append(item)
    pairs = pd.concat([pairs, pd.DataFrame(new_pairs)], ignore_index=True)
    save_rows(out/'articles.jsonl', docs); pairs.to_parquet(out/'pairs.parquet', index=False)
    save_rows(out/'feature_entries.jsonl', entries)
    np.save(out/'feature_mapping.npy', mapping); np.save(out/'direct_mask.npy', mask); np.save(out/'mentions.npy', mentions)
    indices = np.flatnonzero(pairs.split.eq('val').to_numpy() & mask[pairs.article.to_numpy(), pairs.ticker.map(node).to_numpy()])
    assert len(indices) == 4743
    np.save(out/'pair_indices.npy', indices); np.save(out/'original_validation_indices.npy', val_rows)
    for name in ('companies.json', 'label_config.npz', 'aliases.csv'):
        shutil.copyfile(ORIGINAL/name, out/name)
    shutil.copyfile(GRAPHS/'graphs.json', out/'graphs.json')
    summary = dict(legacy_articles=0, compact_validation_articles=n_val, validation_pairs=4743,
        original_direct_pairs=4813, eligible_pool_articles=pool_done['eligible_articles'], sizes=sizes,
        requested_sizes=list(REQUESTED_SIZES), maximum_training_articles=max_size,
        maximum_training_pairs=len(new_pairs), feature_entries=len(entries), feature_offset=offset,
        selection='fixed proportional region/year interleaving of deterministic hash orders; nested prefixes',
        size_cohorts={str(n):dict(collections.Counter(d['region']+':'+d['published_date'][:4] for d in selected[:n])) for n in sizes},
        unverified_semantic_quality=True, human_review_completed=0,
        artifact_hashes={p.name: sha(p) for p in out.iterdir() if p.is_file() and p.suffix in ('.npy','.npz','.parquet','.jsonl','.json','.csv')})
    dump(out/'prepared.json', summary)
    check_pool_inputs(out)
    print(json.dumps({k:v for k,v in summary.items() if k not in ('artifact_hashes', 'size_cohorts')}), flush=True)


def features(out):
    assert not (out/'features_complete.json').exists()
    check_pool_inputs(out)
    torch.set_num_threads(4)
    docs = list(rows(out/'articles.jsonl')); entries = list(rows(out/'feature_entries.jsonl'))
    meta = pd.read_json(out/'companies.json', dtype={'ticker': str})
    names = meta.set_index('ticker').name_official.to_dict()
    encoder = TargetEncoder(FEATURES/'training_inputs', device='cuda')
    started = time.monotonic()
    # Cache role outputs individually; a failed later role does not repeat prior
    # encoding. Each role is accepted only with its matching completion/hash file.
    for role in ('embedding', 'sentiment_ko', 'sentiment_en'):
        dest = out/f'new_{role}.npy'; stamp = out/f'new_{role}_complete.json'
        if stamp.exists():
            assert sha(dest) == json.loads(stamp.read_text())['sha256']
            continue
        values, audits = encoder.encode_entries(role, docs, entries, names, batch_size=32)
        np.save(dest, values)
        save_rows(out/f'{role}_audit.jsonl', audits)
        dump(stamp, dict(sha256=sha(dest), rows=len(values), audit_sha256=sha(out/f'{role}_audit.jsonl')))
        encoder.release(); del values, audits; gc.collect()
    embedding = np.load(out/'new_embedding.npy')
    with np.load(FEATURES/'target_projection.npz') as projection:
        text = ((embedding[1:]-projection['mean']) @ projection['components'].T).astype('float32')
    sentiment = np.load(out/'new_sentiment_ko.npy') + np.load(out/'new_sentiment_en.npy')
    np.save(out/'target_text.npy', np.concatenate([np.load(FEATURES/'target_text.npy'), text]))
    np.save(out/'target_sentiment.npy', np.concatenate([np.load(FEATURES/'target_sentiment.npy'), sentiment[1:]]))
    check_pool_inputs(out)
    dump(out/'features_complete.json', dict(seconds=time.monotonic()-started,
        artifact_hashes={n:sha(out/n) for n in ('target_text.npy','target_sentiment.npy')},
        parent_projection_sha256=sha(FEATURES/'target_projection.npz'), frozen_encoders=True))


def load_data(out, size):
    data = load_event_data(out, 'expanded')
    data.docs = [dict(d, split='excluded') if d['split']=='train' and d['supervised_rank']>=size else d for d in data.docs]
    active = np.array([d['split']=='train' for d in data.docs])
    data.train = data.pairs.split.eq('train').to_numpy() & active[data.article_idx]
    assert int(active.sum()) == size
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--stage', choices=('pool','prepare','features','train','evaluate'), required=True)
    args = parser.parse_args(); out = args.out.resolve()
    if args.stage == 'pool':
        if args.source is None:
            parser.error('--source is required for pool')
        make_pool(args.source, out)
    elif args.stage == 'prepare':
        prepare(out)
    elif args.stage == 'features':
        features(out)
    else:
        from train_news_scale import train, evaluate
        (train if args.stage == 'train' else evaluate)(out)
