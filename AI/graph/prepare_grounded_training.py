"""Research-only, reproducible price-task inputs from the completed grounded KG.

The KG is a provisional relevance filter, not a price label. All primary splits
use the same exact cleaned target context and existing company-specific FinBERT
probabilities. Missing sentiment remains an all-zero missing feature. Labels,
class thresholds, magnitude scales, company universe and PCA are frozen legacy
resources. Calendar-day/naive timestamps become next-day 12 UTC; explicit time
zones are retained. Only a strictly earlier annual graph is usable.

The 2023 validation and 2024+ test periods have already been examined in this
project. These are exploratory comparisons, not untouched final-test claims.
Legacy 2023 examples are retained as a secondary cross-preprocessing comparison
and NEVER select checkpoints. No service model is replaced by this module.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import time
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ORIGINAL = HERE / 'artifacts/news_impact_v2'
FEATURES = HERE / 'artifacts/news_target_experiment_20260915'
BASE_GRAPHS = HERE / 'artifacts/news_current_events_20260915_v2/graphs.json'
BUNDLE = HERE / 'artifacts/news_impact_v2_bundle'
VERSION = 'grounded-price-research-inputs-v2'
UTC = dt.timezone.utc
ARMS = ('candidate', 'no_news', 'no_new_graph')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def dump(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    replace_retry(temporary, path)


def replace_retry(source, target):
    for attempt in range(8):
        try:
            Path(source).replace(target)
            return
        except PermissionError:
            if attempt == 7: raise
            time.sleep(min(.05*2**attempt, 1.))


def rows(path):
    with Path(path).open(encoding='utf8') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def write_row(stream, row):
    stream.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n')


def available_time(raw):
    require(isinstance(raw, str) and bool(raw.strip()), 'Missing publication date')
    value = dt.datetime.fromisoformat(raw.strip().replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = dt.datetime.combine(value.date() + dt.timedelta(days=1), dt.time(12), UTC)
        policy = 'calendar_day_finished_worldwide_utc_minus_12'
    else:
        value = value.astimezone(UTC)
        policy = 'timezone_aware_publication'
    require(2000 <= value.year <= 2100, 'Publication date outside supported years')
    return value.isoformat(), policy


def split_for(raw):
    day = raw[:10]
    if '2020-01-01' <= day <= '2022-12-31':
        return 'train'
    if '2023-01-01' <= day <= '2023-12-31':
        return 'val'
    if '2024-01-01' <= day <= '2026-09-11':
        return 'test'
    return None


def text_key(value):
    value = ' '.join((value or '').split())
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def url_key(value):
    if not value:
        return None
    parts = urlsplit(value.strip())
    # Do not guess which query parameters identify an article.
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ''))


def document_keys(row):
    return {x for x in (text_key(row.get('body')), text_key(row.get('cleaned_body'))) if x}


def validate_target(article, pair, sentiment):
    require(pair['article_id'] == article['article_id'], 'Pair/article mismatch')
    require(pair['materialization_decision'] == 'candidate_ready', 'Unready target')
    require(pair['cleaned_text_sha256'] == article['cleaned_text_sha256'], 'Cleaned content differs')
    context = pair['target_context']['text']
    require(isinstance(context, str) and context.strip(), 'Missing target context')
    quotes = []
    for evidence in pair['evidence']:
        field = evidence['source']
        require(field in ('title', 'body'), 'Unknown evidence field')
        require(article[field][evidence['start']:evidence['end']] == evidence['quote'], 'Original evidence mismatch')
        quote = evidence['cleaned_quote']
        require(article['cleaned_' + field][evidence['cleaned_start']:evidence['cleaned_end']] == quote, 'Cleaned evidence mismatch')
        quotes.append(quote)
    require(quotes and all(q in context for q in quotes), 'Target context lost evidence')
    # Historical context may be appended by the grounder; its input is retained,
    # and publication-specific resolution is bound in the immutable KG snapshot.
    if sentiment is None:
        return context, [0., 0., 0.], 'missing_record'
    require(sentiment['article_id'] == pair['article_id'] and sentiment['ticker'] == pair['ticker'], 'Sentiment identity mismatch')
    require(sentiment['input_sha256'] == pair['source_input_sha256'], 'Sentiment input differs')
    if sentiment['status'] != 'analyzed':
        require(sentiment['probabilities'] is None, 'Held sentiment unexpectedly has probabilities')
        return context, [0., 0., 0.], sentiment['status']
    values = [sentiment['probabilities'][k] for k in ('negative', 'neutral', 'positive')]
    require(all(isinstance(x, (int, float)) and math.isfinite(x) and 0 <= x <= 1 for x in values)
            and abs(sum(values) - 1) < 1e-5, 'Invalid sentiment probabilities')
    return context, values, 'analyzed'


def choose_graph(graphs, public_time):
    eligible = [(pd.Timestamp(g['available_at']), key) for key, g in graphs.items()
                if pd.Timestamp(g['available_at']) < pd.Timestamp(public_time)]
    require(bool(eligible), 'No strictly past graph')
    return max(eligible)[1]


def base_graphs():
    original = json.loads(BASE_GRAPHS.read_text())
    result = {}
    for year in range(2019, 2027):
        source_year = min(year, 2023)
        graph = json.loads(json.dumps(original[str(source_year)]))
        graph.update(as_of=f'{year}-01-01', available_at=f'{year}-01-01T12:00:00+00:00',
                     frozen_base_source_year=source_year, industry_available=False)
        result[str(year)] = graph
    return result


def source_contract(source):
    source = Path(source).resolve()
    manifest = json.loads((source/'manifest.json').read_text())
    wal = source/'state.sqlite3-wal'
    require(not wal.exists() or wal.stat().st_size == 0, 'Immutable source has uncheckpointed WAL')
    require(manifest.get('schema') == 'cosmos-grounded-candidate-kg-1' and manifest.get('artifact_complete') is True,
            'Require completed grounded candidate KG')
    require(sha(source/'state.sqlite3') == manifest['state_sqlite_sha256'], 'KG database differs from completion')
    require(sha(source/'protocol.json') == manifest['protocol_sha256'], 'KG protocol differs')
    return {'directory': str(source), 'manifest_sha256': sha(source/'manifest.json'),
            'state_sqlite_sha256': manifest['state_sqlite_sha256'], 'protocol_sha256': manifest['protocol_sha256']}


def input_assets():
    paths = [ORIGINAL/n for n in ('articles.jsonl', 'pairs.parquet', 'companies.json', 'label_config.npz', 'index_KOSPI.csv', 'index_NASDAQ.csv')]
    paths += [FEATURES/n for n in ('feature_mapping.npy', 'direct_mask.npy', 'mentions.npy', 'target_text.npy', 'target_sentiment.npy', 'target_projection.npz')]
    paths += [BASE_GRAPHS, HERE/'data/prices_daily.parquet']
    return {str(p.resolve()): sha(p) for p in paths}


def prepare(source, out):
    """CPU-only source verification, overlap exclusion, labels and sparse features."""
    from experiment_news_scale import labeler
    from news_impact_data import mention_array
    source, out = Path(source).resolve(), Path(out).resolve()
    require(not out.exists(), 'Choose a new output directory; completed inputs are immutable')
    started = time.monotonic(); binding = source_contract(source)
    out.mkdir(parents=True)
    protocol = {'version': VERSION, 'source': binding, 'assets': input_assets(),
                'code': {name: sha(HERE/name) for name in ('prepare_grounded_training.py', 'news_impact_data.py', 'experiment_news_scale.py')},
                'splits': {'train': ['2020-01-01', '2022-12-31'], 'val': ['2023-01-01', '2023-12-31'], 'test': ['2024-01-01', '2026-09-11']},
                'validation_previously_exposed': True, 'test_previously_exposed': True,
                'human_gold': False, 'production_approved': False, 'economic_relevance_validated': False,
                'label_definition': 'unchanged legacy next-close anchored market-adjusted 1/3-session residuals',
                'price_labels_used_for_news_selection': False, 'legacy_val_checkpoint_selection': False,
                'input_membership': 'every ready target in frozen company universe; observed-price history and future label availability affect supervision mask only',
                'unknown_timezone_policy': 'calendar_day_finished_worldwide_utc_minus_12',
                'feature_policy': 'exact_grounded_target_context_all_nonoverlapping_256_token_windows_token_weighted_mean_then_l2_then_frozen_legacy_train_pca64',
                'sentiment_policy': 'reuse_grounded_company_polarity_probabilities_or_zero_missing_not_neutral',
                'mention_policy': 'grounded_evidence_count_and_first_evidence_position_proxy_not_raw_name_occurrence_count',
                'legacy_val_feature_policy': 'byte-identical old target feature bank and mapping; secondary cross-preprocessing comparison',
                'common_resources': 'legacy train PCA/class thresholds plus frozen annual baseline graphs; not a pure 2020-only information budget'}
    dump(out/'protocol.json', protocol)
    con = sqlite3.connect((source/'state.sqlite3').as_uri()+'?mode=ro&immutable=1', uri=True)
    query = "SELECT a.payload FROM articles a WHERE EXISTS (SELECT 1 FROM pairs p WHERE p.article_id=a.article_id AND p.decision='candidate_ready') ORDER BY a.article_id"
    held_ids, held_text, held_urls = set(), set(), set()
    test_ids, test_text, test_urls = set(), set(), set()
    legacy_docs = list(rows(ORIGINAL/'articles.jsonl'))
    for item in legacy_docs:
        if item['split'] in ('val', 'test'):
            held_ids.add(item['record_id']); held_text.update(document_keys(item))
        if item['split'] == 'test':
            test_ids.add(item['record_id']); test_text.update(document_keys(item))
    metadata_path = HERE/'artifacts/news_quality_source_20260915/legacy_metadata.jsonl'
    for item in rows(metadata_path):
        if item['record_id'] in held_ids and item.get('canonical_url'):
            held_urls.add(url_key(item['canonical_url']))
        if item['record_id'] in test_ids and item.get('canonical_url'):
            test_urls.add(url_key(item['canonical_url']))
    protocol['assets'][str(metadata_path.resolve())] = sha(metadata_path)
    dump(out/'protocol.json', protocol)
    census = collections.Counter()
    for raw, in con.execute(query):
        item = json.loads(raw); split = split_for(item['published_at'])
        census[split or 'outside'] += 1
        if split in ('val', 'test'):
            held_ids.add(item['article_id']); held_text.update(document_keys(item))
            if item.get('source_url'): held_urls.add(url_key(item['source_url']))
        if split == 'test':
            test_ids.add(item['article_id']); test_text.update(document_keys(item))
            if item.get('source_url'): test_urls.add(url_key(item['source_url']))
    print(json.dumps({'stage': 'overlap_census', 'articles': dict(census)}), flush=True)
    labels, first, meta = labeler(); tickers = meta.ticker.tolist(); universe = set(tickers)
    base = base_graphs(); dump(out/'graphs_base.json', base)
    old_bank_size = len(np.load(FEATURES/'target_text.npy', mmap_mode='r'))
    docs, pairs, mappings = [], [], []
    exclusions, counts, time_counts, sentiment_counts = (collections.Counter() for _ in range(4))
    seen_text, seen_urls = set(), set(); feature_count = 0
    with (out/'new_feature_entries.jsonl').open('x', encoding='utf8', newline='\n') as entry_stream, (out/'exclusions.jsonl').open('x', encoding='utf8', newline='\n') as excluded:
        for examined, (raw,) in enumerate(con.execute(query), 1):
            article = json.loads(raw); rid = article['article_id']; split = split_for(article['published_at'])
            reason = None
            if split is None:
                reason = 'outside_primary_periods'
            try:
                public_time, time_policy = available_time(article['published_at'])
            except (TypeError, ValueError):
                reason = 'invalid_publication_time'
            keys, url = document_keys(article), url_key(article.get('source_url'))
            if reason is None and split == 'train':
                if rid in held_ids or keys & held_text or (url and url in held_urls):
                    reason = 'heldout_id_text_or_url_overlap'
                elif keys & seen_text or (url and url in seen_urls):
                    reason = 'duplicate_training_text_or_url'
            if reason is None and split == 'val' and (rid in test_ids or keys & test_text or (url and url in test_urls)):
                reason = 'test_id_text_or_url_overlap'
            accepted = []
            if reason is None:
                for raw_pair, raw_sent in con.execute("SELECT p.payload,s.payload FROM pairs p LEFT JOIN sentiments s USING(article_id,ticker) WHERE p.article_id=? AND p.decision='candidate_ready' ORDER BY p.ticker", (rid,)):
                    pair = json.loads(raw_pair); ticker = pair['ticker']
                    rejection = None
                    if ticker not in universe: rejection = 'company_outside_frozen_universe'
                    if rejection:
                        exclusions[split+':'+rejection] += 1
                        write_row(excluded, {'article_id': rid, 'ticker': ticker, 'split': split, 'reason': rejection}); continue
                    history_available = ticker in first and first[ticker] <= pd.Timestamp(public_time)
                    value = labels.label(ticker, pd.Timestamp(public_time), split) if history_available else None
                    if value is None:
                        label_reason = 'missing_future_label_or_split_boundary' if history_available else 'no_price_observed_before_news'
                        exclusions[split+':'+label_reason] += 1
                        write_row(excluded, {'article_id': rid, 'ticker': ticker, 'split': split, 'reason': label_reason,
                                             'scope': 'supervision_only', 'retained_as_input_if_article_has_another_label': True})
                    context, sentiment, sent_status = validate_target(article, pair, json.loads(raw_sent) if raw_sent else None)
                    accepted.append((pair, value, context, sentiment, sent_status))
                if not any(value is not None for _, value, _, _, _ in accepted): reason = 'no_price_eligible_ready_target'
            if reason:
                exclusions[(split or 'outside')+':'+reason] += 1
                write_row(excluded, {'article_id': rid, 'split': split, 'reason': reason}); continue
            if split == 'train':
                seen_text.update(keys)
                if url: seen_urls.add(url)
            article_index = len(docs); graph = choose_graph(base, public_time); companies = []; mapping = {}
            for pair, value, context, sentiment, sent_status in accepted:
                ticker = pair['ticker']; evidence = pair['evidence']; first_span = min(evidence, key=lambda e: (e['source'] != 'title', e['start']))
                position = 'title' if first_span['source'] == 'title' else ('lead' if first_span['cleaned_start'] < 200 else 'body')
                companies.append({'ticker': ticker, 'n_mentions': len(evidence), 'first_pos': position})
                feature_id = old_bank_size + feature_count; mapping[ticker] = feature_id
                write_row(entry_stream, {'feature_id': feature_id, 'article': article_index, 'article_id': rid, 'ticker': ticker,
                    'company_name': pair['company_name'], 'text': context, 'text_sha256': hashlib.sha256(context.encode()).hexdigest(),
                    'source_input_sha256': pair['source_input_sha256'], 'sentiment': sentiment, 'sentiment_status': sent_status,
                    'historical_resolution': pair['target_context'].get('historical_resolution')})
                feature_count += 1; sentiment_counts[split+':'+sent_status] += 1
                if value is not None:
                    pairs.append(dict(article=article_index, news_id=rid, ticker=ticker, split=split, graph=graph,
                                      is_direct=True, path_length=0, **value))
            docs.append(dict(record_id=rid, published_date=article['published_at'][:10], original_published_at=article['published_at'],
                             available_at=public_time, time_policy=time_policy, split=split, graph=graph, companies=companies))
            mappings.append(mapping); counts[split+'_articles'] += 1; counts[split+'_pairs'] += sum(value is not None for _, value, _, _, _ in accepted)
            counts[split+'_input_pairs'] += len(accepted); time_counts[split+':'+time_policy] += 1
            if examined % 5000 == 0: print(json.dumps({'stage': 'price_labels', 'examined': examined, 'counts': dict(counts)}), flush=True)
    con.close()
    # Preserve every original direct validation row, including old missing-feature
    # rows. Their unchanged mask is exposed to the evaluator for abstention-aware
    # denominator accounting, never silently removed.
    old_pairs = pd.read_parquet(ORIGINAL/'pairs.parquet'); legacy_index = {}
    for i, doc in enumerate(legacy_docs):
        if doc['split'] == 'val':
            legacy_index[i] = len(docs); docs.append(dict(doc, split='legacy_val')); mappings.append(None)
    legacy_pairs = old_pairs[old_pairs.split.eq('val') & old_pairs.is_direct].copy()
    legacy_pairs['article'] = legacy_pairs.article.map(legacy_index); legacy_pairs['split'] = 'legacy_val'
    final_pairs = pd.concat([pd.DataFrame(pairs), legacy_pairs], ignore_index=True)
    final_pairs.to_parquet(out/'pairs.parquet', index=False)
    with (out/'articles.jsonl').open('x', encoding='utf8', newline='\n') as stream:
        for doc in docs: write_row(stream, doc)
    node = {ticker: i for i, ticker in enumerate(tickers)}
    mapping = np.lib.format.open_memmap(out/'feature_mapping.npy', mode='w+', dtype='int64', shape=(len(docs), len(tickers)))
    mask = np.lib.format.open_memmap(out/'direct_mask.npy', mode='w+', dtype=bool, shape=mapping.shape)
    mentions = np.lib.format.open_memmap(out/'mentions.npy', mode='w+', dtype='float32', shape=(*mapping.shape, 5))
    mapping[:] = 0; mask[:] = False; mentions[:] = 0
    for i, targets in enumerate(mappings):
        if targets is None: continue
        for ticker, feature in targets.items(): mapping[i, node[ticker]] = feature
        mentions[i] = mention_array(docs[i]['companies'], tickers)
    for pair in pairs: mask[pair['article'],node[pair['ticker']]] = True
    old_mapping = np.load(FEATURES/'feature_mapping.npy', mmap_mode='r'); old_mask = np.load(FEATURES/'direct_mask.npy', mmap_mode='r'); old_mentions = np.load(FEATURES/'mentions.npy', mmap_mode='r')
    for old, new in legacy_index.items(): mapping[new] = old_mapping[old]; mask[new] = old_mask[old]; mentions[new] = old_mentions[old]
    mapping.flush(); mask.flush(); mentions.flush(); del mapping, mask, mentions
    for name in ('companies.json', 'label_config.npz'): shutil.copyfile(ORIGINAL/name, out/name)
    files = {p.name: sha(p) for p in out.iterdir() if p.is_file()}
    summary = dict(version=VERSION, complete=True, source_verified=True, source=binding, old_feature_bank_size=old_bank_size,
                   new_feature_entries=feature_count, counts=dict(counts), source_census=dict(census), exclusions=dict(exclusions),
                   legacy_val_pairs=len(legacy_pairs), sentiment_counts=dict(sentiment_counts), timestamp_counts=dict(time_counts),
                   source_quality_validated=False, trained=False, encoded=False, elapsed_seconds=time.monotonic()-started, files=files)
    dump(out/'prepared.json', summary); print(json.dumps({k:v for k,v in summary.items() if k not in ('files', 'source')}), flush=True)
    return summary


def verify_prepared(out):
    out = Path(out); prepared = json.loads((out/'prepared.json').read_text()); protocol = json.loads((out/'protocol.json').read_text())
    require(prepared['complete'] is True and prepared['version'] == VERSION, 'Incomplete input preparation')
    for name, digest in prepared['files'].items(): require(sha(out/name) == digest, 'Prepared input changed: '+name)
    for path, digest in protocol['assets'].items(): require(sha(path) == digest, 'Frozen input changed: '+path)
    for name, digest in protocol['code'].items(): require(sha(HERE/name) == digest, 'Preparation code changed: '+name)
    return prepared


def add_graphs(out, integrated):
    out, integrated = Path(out), Path(integrated)
    verify_prepared(out)
    require(not (out/'graphs_candidate.json').exists(), 'Temporal graphs already frozen')
    manifest = json.loads((integrated/'manifest.json').read_text())
    require(manifest.get('schema') == 'cosmos-integrated-relation-graph-1' and manifest.get('artifact_complete') is True, 'Incomplete integrated graph')
    spec = manifest['files']['temporal_evidence']; path = (integrated/spec['path']).resolve()
    require(path.is_relative_to(integrated.resolve()) and sha(path) == spec['sha256'], 'Temporal evidence differs')
    evidence = list(rows(path)); require(len(evidence) == spec['rows'], 'Temporal evidence count differs')
    graphs = json.loads((out/'graphs_base.json').read_text()); universe = set(pd.read_json(out/'companies.json', dtype={'ticker': str}).ticker)
    statistics = {}
    for key, graph in graphs.items():
        cutoff = pd.Timestamp(graph['available_at']); selected = []; seen = {(e['src'], e['dst'], e['type'], bool(e['reverse'])) for e in graph['edges']}
        for row in evidence:
            require(row['rel_type'] in ('PARTNER', 'COMPETE', 'SUPPLY', 'INVEST'), 'Unknown relation type')
            require(pd.Timestamp(row['available_at']).tz is not None, 'Naive temporal relation')
            if pd.Timestamp(row['available_at']) >= cutoff or row['src_ticker'] not in universe or row['dst_ticker'] not in universe: continue
            require(row['src_ticker'] != row['dst_ticker'], 'Self relation')
            for src, dst, reverse in ((row['src_ticker'], row['dst_ticker'], False), (row['dst_ticker'], row['src_ticker'], True)):
                identity = (src, dst, row['rel_type'], reverse)
                if identity in seen: continue
                seen.add(identity)
                selected.append(dict(src=src, dst=dst, type=row['rel_type'], reverse=reverse, weight=1., sign=1.,
                    evidence=[{'id': row['evidence_id'], 'date': row['available_at'], 'source_kind': row['source_kind']}],
                    relation_subtype=row.get('relation_subtype'), polarity_assumption='unsigned_typed_link_not_price_causal_sign'))
        graph['edges'].extend(selected); statistics[key] = {'base_edges': len(graph['edges'])-len(selected), 'added_edges': len(selected)}
    dump(out/'graphs_candidate.json', graphs)
    receipt = {'complete': True, 'integrated_manifest_sha256': sha(integrated/'manifest.json'), 'temporal_evidence_sha256': spec['sha256'],
               'candidate_sha256': sha(out/'graphs_candidate.json'), 'base_sha256': sha(out/'graphs_base.json'), 'snapshots': statistics,
               'edge_policy': 'unit_unsigned_typed_forward_and_reverse_messages; evidence_strictly_before_annual_snapshot',
               'historical_assertions_current_validity_certified': False, 'test_base_graph_policy': 'carry_frozen_2023_base_forward_for_2024_to_2026'}
    dump(out/'graphs_receipt.json', receipt); return receipt


def token_windows(tokenizer, text, max_length=256):
    ids = tokenizer.encode(text, add_special_tokens=False, truncation=False)
    require(bool(ids), 'Empty tokenized context')
    budget = max_length - tokenizer.num_special_tokens_to_add(pair=False)
    require(budget > 0, 'Invalid tokenizer special token budget')
    return [(ids[start:start+budget], len(ids[start:start+budget])) for start in range(0, len(ids), budget)]


def encode(out, *, device='cuda', batch_size=32, chunk_size=256):
    """No downloads, truncation or finetuning. Atomic per-chunk encoding resume."""
    import torch
    from transformers import AutoModel, AutoTokenizer
    out = Path(out); prepared = verify_prepared(out)
    require(batch_size > 0 and chunk_size > 0, 'Positive batch/chunk size required')
    if (out/'encoded.json').exists():
        finished = json.loads((out/'encoded.json').read_text())
        require(finished.get('complete') is True and sha(out/'encoding_protocol.json') == finished['encoding_protocol_sha256'], 'Encoding receipt differs')
        for name, digest in finished['files'].items(): require(sha(out/name) == digest, 'Encoded features differ: '+name)
        saved = json.loads((out/'encoding_protocol.json').read_text())
        require(saved['code_sha256'] == sha(Path(__file__)) and saved['prepared_sha256'] == sha(out/'prepared.json'), 'Completed encoding inputs changed')
        return finished
    model_dir = BUNDLE/'models/embedding'
    binding = {'prepared_sha256': sha(out/'prepared.json'), 'code_sha256': sha(Path(__file__)),
               'model_files': {str(p.relative_to(model_dir)): sha(p) for p in sorted(model_dir.rglob('*')) if p.is_file()},
               'model_spec': json.loads((BUNDLE/'pretrained.json').read_text())['embedding'], 'chunk_size': chunk_size,
               'pooling': 'exclude_special_tokens_masked_content_token_mean_across_nonoverlapping_windows_then_l2', 'max_length': 256}
    chunks = out/'embedding_chunks'; chunks.mkdir(exist_ok=True)
    if (out/'encoding_protocol.json').exists():
        require(json.loads((out/'encoding_protocol.json').read_text()) == binding, 'Encoding model/protocol changed')
    else: dump(out/'encoding_protocol.json', binding)
    entries = list(rows(out/'new_feature_entries.jsonl')); require(len(entries) == prepared['new_feature_entries'], 'Feature count differs')
    tokenizer = model = None
    for start in range(0, len(entries), chunk_size):
        end = min(start+chunk_size, len(entries)); target = chunks/f'{start:08d}_{end:08d}.npz'; receipt = target.with_suffix('.json')
        if receipt.exists():
            require(target.exists() and sha(target) == json.loads(receipt.read_text())['sha256'], 'Completed embedding chunk differs'); continue
        if (out/'STOP_ENCODING').exists():
            return {'status': 'paused_stop_file', 'next_entry': start}
        if model is None:
            torch.set_num_threads(4)
            tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False)
            model = AutoModel.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False).to(device).eval()
        started = time.monotonic(); flat = []; owners = []; weights = []; audits = []
        for local_index, entry in enumerate(entries[start:end]):
            require(hashlib.sha256(entry['text'].encode()).hexdigest() == entry['text_sha256'], 'Context hash differs')
            windows = token_windows(tokenizer, entry['text']); audits.append({'feature_id': entry['feature_id'], 'tokens': sum(n for _, n in windows), 'windows': len(windows), 'truncated': False})
            for ids, weight in windows:
                encoded = tokenizer.prepare_for_model(ids, add_special_tokens=True, truncation=False, return_special_tokens_mask=True)
                flat.append(encoded); owners.append(local_index); weights.append(weight)
        sums = np.zeros((end-start, model.config.hidden_size), dtype='float64'); totals = np.zeros(end-start, dtype='float64')
        with torch.inference_mode():
            for offset in range(0, len(flat), batch_size):
                batch = tokenizer.pad(flat[offset:offset+batch_size], padding=True, return_tensors='pt')
                special = batch.pop('special_tokens_mask').to(device)
                batch = {key: value.to(device) for key, value in batch.items()}
                hidden = model(**batch).last_hidden_state
                mask = (batch['attention_mask'] * (1-special)).unsqueeze(-1)
                means = ((hidden*mask).sum(1)/mask.sum(1).clamp(min=1)).cpu().numpy()
                for j, mean in enumerate(means):
                    owner, weight = owners[offset+j], weights[offset+j]; sums[owner] += mean*weight; totals[owner] += weight
        embedding = sums / totals[:, None]; embedding /= np.maximum(np.linalg.norm(embedding, axis=1, keepdims=True), 1e-12)
        temporary = target.with_suffix('.tmp')
        with temporary.open('wb') as stream:
            np.savez_compressed(stream, embedding=embedding.astype('float32'), feature_ids=np.asarray([x['feature_id'] for x in entries[start:end]])); stream.flush(); os.fsync(stream.fileno())
        replace_retry(temporary, target); dump(receipt, {'sha256': sha(target), 'start': start, 'end': end, 'audits': audits, 'seconds': time.monotonic()-started})
        print(json.dumps({'stage': 'embedding', 'completed': end, 'total': len(entries), 'seconds': time.monotonic()-started}), flush=True)
    old = prepared['old_feature_bank_size']; total = old+len(entries)
    texts = np.lib.format.open_memmap(out/'target_text.npy', mode='w+', dtype='float32', shape=(total, 64))
    sentiments = np.lib.format.open_memmap(out/'target_sentiment.npy', mode='w+', dtype='float32', shape=(total, 3))
    texts[:old] = np.load(FEATURES/'target_text.npy'); sentiments[:old] = np.load(FEATURES/'target_sentiment.npy')
    with np.load(FEATURES/'target_projection.npz') as projection:
        for start in range(0, len(entries), chunk_size):
            end = min(start+chunk_size, len(entries)); target = chunks/f'{start:08d}_{end:08d}.npz'
            with np.load(target) as chunk:
                require(np.array_equal(chunk['feature_ids'], np.arange(old+start, old+end)), 'Chunk feature identity differs')
                texts[old+start:old+end] = (chunk['embedding']-projection['mean']) @ projection['components'].T
            sentiments[old+start:old+end] = [entry['sentiment'] for entry in entries[start:end]]
    texts.flush(); sentiments.flush(); del texts, sentiments
    receipt = {'complete': True, 'features': total, 'new_features': len(entries), 'encoding_protocol_sha256': sha(out/'encoding_protocol.json'),
               'files': {name: sha(out/name) for name in ('target_text.npy', 'target_sentiment.npy')}, 'truncated_contexts': 0}
    dump(out/'encoded.json', receipt)
    return receipt


def load_data(out, arm='candidate'):
    """TrainingData-compatible object; primary val only selects the checkpoint."""
    from news_impact_data import graph_arrays, node_features
    out = Path(out); require(arm in ARMS, 'Unknown comparison arm')
    d = SimpleNamespace(); d.docs = list(rows(out/'articles.jsonl')); d.pairs = pd.read_parquet(out/'pairs.parquet')
    d.meta = pd.read_json(out/'companies.json', dtype={'ticker': str}); d.tickers = d.meta.ticker.tolist()
    d.article_idx = d.pairs.article.to_numpy(dtype='int64'); d.node_idx = d.pairs.ticker.map({t:i for i,t in enumerate(d.tickers)}).to_numpy(dtype='int64')
    d.graphs = json.loads((out/('graphs_base.json' if arm == 'no_new_graph' else 'graphs_candidate.json')).read_text())
    d.static = {k:node_features(d.meta,g) for k,g in d.graphs.items()}; d.graph_arrays = {k:graph_arrays(g,d.tickers) for k,g in d.graphs.items()}
    d.mask = np.load(out/'direct_mask.npy'); d.mentions = np.load(out/'mentions.npy')
    d.val = d.pairs.split.eq('val').to_numpy(); d.train = d.pairs.split.eq('train').to_numpy()
    d.y = d.pairs[['y_1d','y_3d']].to_numpy(dtype='float32')
    with np.load(out/'label_config.npz') as cfg: d.thresholds = cfg['thresholds']; d.scales = cfg['scales']
    d.cls = np.where(d.y>d.thresholds,2,np.where(d.y < -d.thresholds,0,1)).astype('int64'); d.mag = np.minimum(abs(d.y)/d.scales,1).astype('float32')
    d.dense_cls = np.zeros((*d.mask.shape,2),dtype='int64'); d.dense_mag = np.zeros((*d.mask.shape,2),dtype='float32')
    d.dense_cls[d.article_idx,d.node_idx] = d.cls; d.dense_mag[d.article_idx,d.node_idx] = d.mag
    return d


def main():
    parser = argparse.ArgumentParser(description=__doc__); commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare'); p.add_argument('--source', type=Path, required=True); p.add_argument('--out', type=Path, required=True)
    p = commands.add_parser('graphs'); p.add_argument('--out', type=Path, required=True); p.add_argument('--integrated', type=Path, required=True)
    p = commands.add_parser('encode'); p.add_argument('--out', type=Path, required=True); p.add_argument('--device', default='cuda'); p.add_argument('--batch-size', type=int, default=32); p.add_argument('--chunk-size', type=int, default=256)
    args = parser.parse_args()
    if args.command == 'prepare': result = prepare(args.source, args.out)
    elif args.command == 'graphs': result = add_graphs(args.out, args.integrated)
    else: result = encode(args.out, device=args.device, batch_size=args.batch_size, chunk_size=args.chunk_size)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
