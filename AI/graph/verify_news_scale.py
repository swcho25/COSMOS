"""Independent data, frozen-feature and checkpoint checks for scale experiments.

The original full validation population (4,813 direct pairs) remains fixed; 70
hygiene misses are not silently removed. New training examples are nested by
an input-only deterministic order. Every mapped training company, including
unlabelled text-only inputs, needs an observed adjusted price before availability.
Recompute all new targets by endpoint price arithmetic, never Labels.label().
No source mutation, test prediction, label refit, deployment or model promotion.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import itertools
import json
from pathlib import Path

import exchange_calendars as xc
import numpy as np
import pandas as pd
import torch
from scipy.stats import norm

from news_quality import text_digest, financial_evidence
from target_news_features import clean_target_article, TargetEncoder
from news_impact_data import graph_arrays, node_features
from target_news_model import TargetNewsGAT

HERE = Path(__file__).resolve().parent
ORIGINAL = HERE / 'artifacts/news_impact_v2'
PARENT = HERE / 'artifacts/news_target_experiment_20260915'
GRAPH_SOURCE = HERE / 'artifacts/news_current_events_20260915_v2'
OLD_METADATA = HERE / 'artifacts/news_quality_source_20260915/legacy_metadata.jsonl'


def rows(path):
    with Path(path).open(encoding='utf8') as f:
        for line in f:
            yield json.loads(line)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def write_frozen_audit(path, value):
    """Preserve audits that become hashed training inputs on later verification."""
    path=Path(path)
    if path.exists():
        assert read(path)==value, f'Frozen audit differs; preserving original: {path}'
        return
    with path.open('x',encoding='utf8') as f:
        f.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def independent_order(pool):
    """Expected indices without calling the production interleaving helper."""
    counts = collections.Counter((r['region'], r['published_date'][:4]) for r in pool)
    seen = collections.Counter()
    ordered = sorted(range(len(pool)), key=lambda i: (pool[i]['region'], pool[i]['published_date'][:4],
                                                     pool[i]['sample_key'], pool[i]['record_id']))
    scores = []
    for i in ordered:
        key = (pool[i]['region'], pool[i]['published_date'][:4])
        scores.append(((seen[key] + .5) / counts[key], pool[i]['sample_key'], i))
        seen[key] += 1
    return [i for _, _, i in sorted(scores)]


def read_and_check(out):
    protocol, prepared = read(out/'pool_protocol.json'), read(out/'prepared.json')
    source=Path(protocol['source'])
    export_manifest=read(source/'manifest.json')
    assert export_manifest['complete'] is True
    assert export_manifest['labels_used'] is False and export_manifest['hdfs_read_only'] is True
    assert sha(source/'training_candidates.jsonl')==export_manifest['training_candidates_sha256']
    pool_done=read(out/'pool_completion.json')
    assert {'graphs.json','companies.json','aliases.csv'} <= set(prepared['artifact_hashes'])
    for name, digest in protocol['source_hashes'].items():
        assert sha(Path(name)) == digest, name
    for name, digest in protocol['code_hashes'].items():
        assert sha(HERE/name) == digest, name
    for name, digest in prepared['artifact_hashes'].items():
        assert sha(out/name) == digest, name
    assert not protocol['selection_outcomes_used'] and protocol['original_validation_unchanged']
    assert protocol['requested_sizes'] == [20000, 50000, 100000]
    assert not protocol['source_time_certified'] and not protocol['historical_identity_certified']
    assert protocol['human_review_completed'] == 0 and not protocol['automatic_model_replacement']
    assert sha(out/'eligible_pool.jsonl') == pool_done['eligible_pool_sha256']
    for name in ('companies.json', 'label_config.npz', 'aliases.csv'):
        assert sha(out/name) == sha(ORIGINAL/name)
    assert sha(out/'graphs.json') == sha(GRAPH_SOURCE/'graphs.json')

    docs = list(rows(out/'articles.jsonl'))
    pairs = pd.read_parquet(out/'pairs.parquet')
    old_docs = list(rows(ORIGINAL/'articles.jsonl'))
    old_pairs = pd.read_parquet(ORIGINAL/'pairs.parquet')
    meta = pd.read_json(out/'companies.json', dtype={'ticker': str})
    tickers = meta.ticker.tolist(); node = {t:i for i,t in enumerate(tickers)}
    markets = meta.set_index('ticker').market.to_dict()
    mapping = np.load(out/'feature_mapping.npy'); mask = np.load(out/'direct_mask.npy')
    mentions = np.load(out/'mentions.npy'); indices = np.load(out/'pair_indices.npy')
    assert mapping.shape == mask.shape == (len(docs), len(tickers))
    assert mentions.shape == (*mask.shape, 5)
    assert len({r['record_id'] for r in docs}) == len(docs)
    assert not pairs.duplicated(['article', 'ticker']).any()
    assert pairs.is_direct.all() and set(pairs.split) == {'train', 'val'}
    articles = pairs.article.to_numpy(); nodes = pairs.ticker.map(node).to_numpy()
    assert np.isfinite(nodes).all()

    old_ids = np.flatnonzero(old_pairs.split.eq('val') & old_pairs.is_direct)
    np.testing.assert_array_equal(np.load(out/'original_validation_indices.npy'), old_ids)
    assert len(old_ids) == 4813 and prepared['original_direct_pairs'] == 4813
    old_articles = sorted(set(old_pairs.iloc[old_ids].article)); n_val = len(old_articles)
    assert n_val == prepared['compact_validation_articles']
    remap = {a:j for j,a in enumerate(old_articles)}
    expected_val = old_pairs.iloc[old_ids].copy().reset_index(drop=True)
    expected_val['article'] = expected_val.article.map(remap)
    pd.testing.assert_frame_equal(pairs.iloc[:4813][old_pairs.columns].reset_index(drop=True), expected_val)
    np.testing.assert_array_equal(pairs.iloc[:4813].original_pair_index, old_ids)
    assert (pairs.iloc[4813:].original_pair_index == -1).all()
    old_mask = np.load(PARENT/'direct_mask.npy')
    np.testing.assert_array_equal(mask[:n_val], old_mask[old_articles])
    for name, value in (('feature_mapping', mapping), ('mentions', mentions)):
        np.testing.assert_array_equal(value[:n_val], np.load(PARENT/f'{name}.npy')[old_articles])
    for j, old_index in enumerate(old_articles):
        expected = clean_target_article(old_docs[old_index])[0]
        if not mask[j].any():
            expected['split'] = 'excluded'
        assert docs[j] == expected
    expected_indices = np.flatnonzero(pairs.split.eq('val').to_numpy() & mask[articles, nodes])
    np.testing.assert_array_equal(indices, expected_indices)
    assert len(indices) == 4743 and len(set(range(4813)) - set(indices)) == 70

    # Raw/clean body and canonical URL overlap is checked independently from IDs.
    hold_ids = {d['record_id'] for d in old_docs if d['split'] in ('val', 'test')}
    hold_bodies = set()
    for d in old_docs:
        if d['record_id'] in hold_ids:
            hold_bodies.update((text_digest(d.get('body')), text_digest(clean_target_article(d)[0].get('body'))))
    hold_bodies.discard(text_digest(''))
    hold_urls = {r['canonical_url'] for r in rows(OLD_METADATA)
                 if r['record_id'] in hold_ids and r.get('canonical_url')}
    pool_light, selected_pool = [], {}
    wanted = {d['record_id'] for d in docs[n_val:]}
    seen_ids, seen_bodies, seen_urls = set(), set(), set()
    pool_cohorts=collections.Counter()
    for r in rows(out/'eligible_pool.jsonl'):
        rid, url = r['record_id'], r.get('canonical_url')
        hashes = {r['raw_body_hash'], r['clean_body_hash']} - {text_digest('')}
        assert rid not in hold_ids and rid not in seen_ids
        assert not hashes & hold_bodies and not hashes & seen_bodies
        assert not url or (url not in hold_urls and url not in seen_urls)
        assert r['clean_body_hash'] == text_digest(r.get('body'))
        assert r['sample_key'] == hashlib.sha256(('scale-supervision-v1:'+rid).encode()).hexdigest()
        assert '2017-01-01' <= r['published_date'] <= '2022-12-31' and r['split'] == 'train'
        full=r['title']+'\n'+r['body'];company_tickers={c['ticker'] for c in r['companies']}
        assert {h['ticker'] for h in r['literal_mentions']}==company_tickers
        for hit in r['literal_mentions']:
            assert 0<=hit['start']<hit['end']<=len(full) and full[hit['start']:hit['end']].strip()
        # A company removed by the observed-price guard cannot be the sole
        # finance keyword witness for unrelated surviving company inputs.
        evidence=financial_evidence(r['title'],r['body'],r['literal_mentions'])
        assert evidence, f"No financial evidence among surviving inputs: {rid}"
        assert all(e['ticker'] in company_tickers and full[e['start']:e['end']]==e['quote'] for e in evidence)
        seen_ids.add(rid); seen_bodies.update(hashes)
        if url: seen_urls.add(url)
        pool_cohorts[r['region'],r['published_date'][:4]]+=1
        pool_light.append({k:r[k] for k in ('record_id', 'region', 'published_date', 'sample_key')})
        if rid in wanted: selected_pool[rid] = r
    assert len(pool_light) == prepared['eligible_pool_articles'] == pool_done['eligible_articles']
    assert pool_cohorts==collections.Counter({(r['region'],r['year']):r['articles'] for r in pool_done['cohorts']})
    expected_sizes = [n for n in (20000, 50000, 100000) if n <= len(pool_light)]
    if len(pool_light) < 100000 and len(pool_light) not in expected_sizes:
        expected_sizes.append(len(pool_light))
    assert prepared['sizes'] == expected_sizes and min(expected_sizes) == 20000
    maximum = min(100000, len(pool_light))
    order = independent_order(pool_light)[:maximum]
    assert [d['record_id'] for d in docs[n_val:]] == [pool_light[i]['record_id'] for i in order]
    assert len(docs)-n_val == prepared['maximum_training_articles'] == maximum
    for n in expected_sizes:
        assert dict(collections.Counter(d['region']+':'+d['published_date'][:4] for d in docs[n_val:n_val+n])) == prepared['size_cohorts'][str(n)]
    source_seen = set();raw_ids=set();raw_cohorts=collections.Counter();raw_count=0
    for raw in rows(source/'training_candidates.jsonl'):
        raw_count+=1
        rid = raw['record_id']
        assert rid not in raw_ids;raw_ids.add(rid)
        assert str(raw['year'])==str(raw['published_date'])[:4]
        assert raw['region'] in ('domestic','overseas') and '2017'<=str(raw['year'])<='2022'
        raw_cohorts[raw['region'],str(raw['year'])]+=1
        if rid not in wanted: continue
        assert rid not in source_seen; source_seen.add(rid)
        d = selected_pool[rid]
        clean, flags, quarantine = clean_target_article(raw)
        assert not quarantine and len(clean['body'].strip()) >= 200
        assert d['raw_body_hash'] == text_digest(raw.get('body'))
        assert d['title'] == clean['title'] and d['body'] == clean['body'] and d['quality_flags'] == flags
        assert d['canonical_url'] == raw.get('canonical_url')
        assert d['region'] == raw['region'] and d['published_date'] == str(raw['published_date'])[:10]
        assert d['source_dataset'] == raw.get('source_dataset')
        assert {c['ticker'] for c in d['companies']} <= {c['ticker'] for c in raw['companies']}
    assert source_seen == wanted
    assert raw_count==len(raw_ids)==export_manifest['training_candidates']==pool_done['raw_candidates']
    manifest_cohorts={}
    for r in export_manifest['eligible_candidates']:
        key=r['region'],str(r['year']);assert key not in manifest_cohorts
        assert 0<=r['selected_articles']<=r['eligible_articles']
        assert r['selected_articles']<=export_manifest['per_year_region']
        assert not r.get('conflicting_record_ids') and not r.get('invalid_record_ids')
        manifest_cohorts[key]=r['selected_articles']
    assert set(manifest_cohorts)=={(region,str(year)) for region in ('domestic','overseas') for year in range(2017,2023)}
    assert raw_cohorts==collections.Counter(manifest_cohorts)
    excluded_ids=set();exclusion_counts=collections.Counter()
    for r in rows(out/'pool_exclusions.jsonl'):
        assert r['record_id'] not in excluded_ids and r['record_id'] not in seen_ids
        excluded_ids.add(r['record_id'])
        exclusion_counts[r['region'],str(r['year']),r['reason']]+=1
    assert raw_ids==seen_ids|excluded_ids
    assert exclusion_counts==collections.Counter({(r['region'],str(r['year']),r['reason']):r['articles'] for r in pool_done['exclusions']})

    # Independent observed-price guard and endpoint arithmetic; no label helper.
    raw_price = pd.read_parquet(HERE/'data/prices_daily.parquet')
    raw_price['date'] = pd.to_datetime(raw_price.trading_at).dt.tz_localize(None).dt.normalize()
    panels, first_closes = {}, {}
    for market, group in raw_price.groupby('market'):
        panel = group.pivot(index='date', columns='ticker', values='adj_close').sort_index()
        f = pd.read_csv(ORIGINAL/f'index_{market}.csv')
        benchmark = pd.Series(f.Close.to_numpy(), index=pd.to_datetime(f.Date.str[:10])).reindex(panel.index).to_numpy()
        calendar = xc.get_calendar('XKRX' if market == 'KOSPI' else 'XNAS', start='2016-01-01', end='2026-12-31')
        closes = pd.DatetimeIndex(calendar.schedule['close'].reindex(panel.index))
        assert not closes.isna().any()
        panels[market] = panel, benchmark, closes
        for ticker in panel:
            good = np.flatnonzero(np.isfinite(panel[ticker].to_numpy()) & (panel[ticker].to_numpy() > 0))
            if len(good): first_closes[ticker] = closes[good[0]]
    entries = list(rows(out/'feature_entries.jsonl'))
    offset = len(np.load(PARENT/'target_text.npy', mmap_mode='r'))
    assert offset == prepared['feature_offset']
    assert len(entries) == prepared['feature_entries']
    seen_entry = set(); expected_mentions = np.zeros_like(mentions[n_val:])
    labelled = collections.defaultdict(set)
    for p in pairs.iloc[4813:].itertuples(): labelled[p.article].add(p.ticker)
    mapped_count = unmapped_label_count = 0
    for j, doc in enumerate(docs[n_val:], n_val):
        assert doc['supervised_rank'] == j-n_val and doc['split'] == 'train'
        source_record = selected_pool[doc['record_id']]
        expected_doc = {k:v for k,v in source_record.items() if k not in ('target_labels', 'literal_mentions')}
        expected_doc['supervised_rank'] = j-n_val
        assert doc == expected_doc
        zone = 'Asia/Seoul' if doc['region'] == 'domestic' else 'Etc/GMT+12'
        available = (pd.Timestamp(doc['published_date']).tz_localize(zone)+pd.Timedelta(days=1)).tz_convert('UTC')
        assert pd.Timestamp(doc['available_at']) == available
        company_tickers = {m['ticker'] for m in doc['companies']}
        assert len(company_tickers) == len(doc['companies']) and labelled[j] == set(source_record['target_labels'])
        assert {tickers[k] for k in np.flatnonzero(mapping[j])} == company_tickers
        assert {tickers[k] for k in np.flatnonzero(mask[j])} == labelled[j]
        assert labelled[j] <= company_tickers and labelled[j]
        for c in doc['companies']:
            ticker = c['ticker']; k = node[ticker]
            assert ticker in first_closes and first_closes[ticker] <= available
            mapped_count += 1; unmapped_label_count += ticker not in labelled[j]
            expected_mentions[j-n_val,k,0] = 1
            expected_mentions[j-n_val,k,1] = np.log1p(c['n_mentions'])/5
            expected_mentions[j-n_val,k,2+{'title':0,'lead':1,'body':2}[c['first_pos']]] = 1
    np.testing.assert_array_equal(mentions[n_val:], expected_mentions)
    for j, entry in enumerate(entries):
        article, ticker = entry['article'], entry['ticker']
        assert n_val <= article < len(docs)
        assert (article,ticker) not in seen_entry; seen_entry.add((article,ticker))
        assert mapping[article,node[ticker]] == offset+j
        assert entry['hits'] == selected_pool[docs[article]['record_id']]['literal_mentions']
        full = docs[article]['title']+'\n'+docs[article]['body']
        for hit in entry['hits']:
            assert 0 <= hit['start'] < hit['end'] <= len(full)
            assert full[hit['start']:hit['end']].strip()
        assert any(h['ticker']==ticker for h in entry['hits'])
    assert mapped_count == len(entries) == int(np.count_nonzero(mapping[n_val:]))
    assert mapping.min() >= 0 and mapping.max() < offset+len(entries)
    label_error = 0.
    for r in pairs.iloc[4813:].itertuples():
        doc=docs[r.article]; panel, benchmark, closes=panels[r.market]
        assert r.market == markets[r.ticker] and r.news_id == doc['record_id']
        assert r.graph == doc['graph'] and r.path_length == 0 and r.rule_weight == 1 and r.rule_sign == 1
        assert r.rule_source == r.ticker and mask[r.article,node[r.ticker]]
        i=int(np.searchsorted(closes.asi8,pd.Timestamp(doc['available_at']).value,side='right'))
        assert i+3 < len(panel)
        assert r.anchor_date == panel.index[i].strftime('%Y-%m-%d')
        assert r.label_end == panel.index[i+3].strftime('%Y-%m-%d') and r.label_end <= '2022-12-31'
        stock=panel[r.ticker].to_numpy()[i:i+4]; index=benchmark[i:i+4]
        assert np.isfinite(stock).all() and np.isfinite(index).all() and (stock>0).all() and (index>0).all()
        expected=np.array([np.log(stock[h]/stock[0])-np.log(index[h]/index[0]) for h in (1,3)])
        error=float(np.max(abs(expected-np.array([r.y_1d,r.y_3d])))); label_error=max(label_error,error)
    assert label_error < 1e-12
    graphs=read(out/'graphs.json'); edge_count=0
    for key,g in graphs.items():
        assert key in {str(y) for y in range(2017,2024)} and not g['industry_available']
        cutoff=pd.Timestamp(g['available_at'])
        for edge in g['edges']:
            edge_count+=1
            assert edge['src'] in node and edge['dst'] in node and edge['src']!=edge['dst']
            assert edge['type'] in ('co_mention','correlation') and not edge['reverse']
            for ev in edge['evidence']:
                assert ev['date'] < g['as_of']
                if 'available_at' in ev: assert pd.Timestamp(ev['available_at']) < cutoff
                if edge['type']=='correlation': assert ev['id'].split(':')[1:3] == [f'{int(key)-1}-01-01',g['as_of']]
    for doc in docs:
        assert pd.Timestamp(graphs[doc['graph']]['available_at']) < pd.Timestamp(doc['available_at'])
    result=dict(source_unchanged=True, original_validation_pairs=4813, retained_validation_pairs=4743,
                missing_pairs_counted_wrong=70, validation_identity_and_values_exact=True,
                all_training_targets_recomputed=2*(len(pairs)-4813), maximum_label_error=label_error,
                all_mapped_training_nodes_checked=mapped_count, unlabelled_mapped_nodes_checked=unmapped_label_count,
                eligible_pool_articles=len(pool_light), selected_training_articles=maximum, nested_sizes=expected_sizes,
                completed_remote_export_sha256_verified=True,raw_candidate_rows_verified=raw_count,
                surviving_finance_evidence_checked_articles=len(pool_light),pool_inclusion_exclusion_partition_complete=True,
                original_holdout_id_body_url_overlap=0, checked_graph_edges=edge_count,
                frozen_graphs_match_previously_verified_source=True, source_timestamp_semantics_verified=False,
                human_review_completed=0, no_test_predictions=True, verifier_sha256=sha(Path(__file__)),
                prepared_sha256=sha(out/'prepared.json'),code_hashes={'verify_news_scale.py':sha(Path(__file__))})
    state=dict(docs=docs,pairs=pairs,meta=meta,tickers=tickers,mapping=mapping,mask=mask,mentions=mentions,
               indices=indices,article=articles,node=nodes,n_val=n_val,entries=entries,offset=offset,graphs=graphs,
               prepared=prepared,original_validation_indices=old_ids)
    return result,state


def verify_features(out,s):
    stamp=read(out/'features_complete.json')
    for name,digest in stamp['artifact_hashes'].items(): assert sha(out/name)==digest
    assert stamp['parent_projection_sha256']==sha(PARENT/'target_projection.npz') and stamp['frozen_encoders']
    text=np.load(out/'target_text.npy');sent=np.load(out/'target_sentiment.npy');offset=s['offset']
    np.testing.assert_array_equal(text[:offset],np.load(PARENT/'target_text.npy'))
    np.testing.assert_array_equal(sent[:offset],np.load(PARENT/'target_sentiment.npy'))
    embedding=np.load(out/'new_embedding.npy');ko=np.load(out/'new_sentiment_ko.npy');en=np.load(out/'new_sentiment_en.npy')
    assert len(embedding)==len(ko)==len(en)==len(s['entries'])+1
    assert len(text)==len(sent)==offset+len(s['entries'])
    assert not embedding[0].any() and not ko[0].any() and not en[0].any()
    assert np.isfinite(text).all() and np.isfinite(sent).all()
    with np.load(PARENT/'target_projection.npz') as p:
        expected=((embedding[1:]-p['mean'])@p['components'].T).astype('float32')
    np.testing.assert_allclose(text[offset:],expected,rtol=1e-6,atol=1e-7)
    np.testing.assert_array_equal(sent[offset:],(ko+en)[1:])
    picks=[]
    for region in ('domestic','overseas'):
        candidates=[j for j,e in enumerate(s['entries']) if s['docs'][e['article']]['region']==region]
        assert candidates
        picks.extend(candidates[i] for i in np.linspace(0,len(candidates)-1,3,dtype=int))
    picks=sorted(set(picks));entries=[s['entries'][j] for j in picks]
    names=s['meta'].set_index('ticker').name_official.to_dict()
    encoder=TargetEncoder(PARENT/'training_inputs',device='cpu'); errors={}
    torch.set_num_threads(4)
    for role,saved in [('embedding',embedding),('sentiment_ko',ko),('sentiment_en',en)]:
        role_stamp=read(out/f'new_{role}_complete.json')
        assert sha(out/f'new_{role}.npy')==role_stamp['sha256']
        assert sha(out/f'{role}_audit.jsonl')==role_stamp['audit_sha256']
        values,_=encoder.encode_entries(role,s['docs'],entries,names,batch_size=6);encoder.release()
        expected=saved[np.array(picks)+1]
        error=float(np.max(abs(values[1:]-expected)));errors[role]=error
        np.testing.assert_allclose(values[1:],expected,rtol=2e-5,atol=3e-6)
    s.update(text=text,sent=sent)
    return dict(parent_features_exact=True,projection_all_rows_match=True,encoder_pairs=len(picks),
                cpu_encoder_max_errors=errors,common_frozen_pca=True)


def verify_models(out,s):
    """Selected checkpoints, seed means and early-stopping history on CPU."""
    protocol=read(out/'training_protocol.json')
    for name,digest in protocol['source_hashes'].items(): assert sha(Path(name))==digest
    for name,digest in protocol['code_hashes'].items(): assert sha(HERE/name)==digest
    y=s['pairs'][['y_1d','y_3d']].to_numpy(dtype='float32')
    with np.load(out/'label_config.npz') as cfg:
        actual=np.where(y>cfg['thresholds'],2,np.where(y < -cfg['thresholds'],0,1))
        target_magnitude=np.minimum(abs(y)/cfg['scales'],1).astype('float32')
    results=[]
    chosen=[]
    val=s['pairs'].iloc[s['indices']]
    for market in ('KOSPI','NASDAQ'):
        ids=sorted(val.loc[val.market.eq(market),'article'].unique())
        chosen.extend(ids[i] for i in np.linspace(0,len(ids)-1,3,dtype=int))
    chosen=sorted(set(chosen))
    for size in s['prepared']['sizes']:
        for mode in ('text','no_content'):
            arm=f'size_{size}_{mode}';folder=out/'models'/arm
            marker=read(folder/'fit_complete.json');attempt=folder/marker['attempt']
            assert attempt.parent==folder and attempt.is_dir()
            assert sha(out/'training_protocol.json')==marker['training_protocol_sha256']
            assert sha(attempt/'fit_complete.json')==marker['fit_complete_sha256']
            done=read(attempt/'fit_complete.json')
            assert done['arm']==arm and done['seeds']==3 and done['article_budget']==size
            assert done['train_pairs']==int(s['mask'][s['n_val']:s['n_val']+size].sum())
            for name,digest in done['artifact_hashes'].items(): assert sha(attempt/name)==digest
            history=pd.read_csv(attempt/'training_history.csv');selections=read(attempt/'selections.json')
            seed_p=[];seed_m=[];max_error=0.;max_loss_error=0.
            for seed in range(3):
                checkpoint=torch.load(attempt/f'gat_seed{seed}.pt',map_location='cpu',weights_only=False)
                p=np.load(attempt/f'validation_seed{seed}.npz')
                np.testing.assert_array_equal(p['pair_indices'],s['indices'])
                probabilities=p['probabilities'];magnitude=p['magnitude']
                assert probabilities.shape==(4743,2,3) and magnitude.shape==(4743,2)
                np.testing.assert_allclose(probabilities.sum(-1),1,atol=1e-6)
                assert np.isfinite(probabilities).all() and np.isfinite(magnitude).all()
                assert (probabilities>=0).all() and (probabilities<=1).all() and (magnitude>=0).all() and (magnitude<=1).all()
                h=history.loc[history.seed.eq(seed)].sort_values('epoch');best=float('inf');best_epoch=-1
                assert h.epoch.tolist()==list(range(len(h))) and 1<=len(h)<=30
                for row in h.itertuples():
                    if row.val_loss < best-1e-5: best,best_epoch=row.val_loss,row.epoch
                    if row.epoch-best_epoch>=6: assert row.epoch==h.epoch.iloc[-1]
                if len(h)<30: assert h.epoch.iloc[-1]-best_epoch>=6
                expected=dict(seed=seed,best_epoch=best_epoch,val_loss=float(best))
                assert checkpoint['selection']==selections[seed]
                assert checkpoint['selection']['seed']==seed and checkpoint['selection']['best_epoch']==best_epoch
                assert abs(checkpoint['selection']['val_loss']-best)<1e-12
                ce=-np.log(np.take_along_axis(probabilities.astype(float),actual[s['indices'],...,None],axis=-1)[...,0])
                error=abs(magnitude.astype(float)-target_magnitude[s['indices']])
                huber=np.where(error<1,.5*error**2,error-.5)
                loss=float(pd.Series((ce+huber).mean(axis=-1),index=s['article'][s['indices']]).groupby(level=0).mean().mean())
                max_loss_error=max(max_loss_error,abs(loss-best));assert abs(loss-best)<2e-6
                model=TargetNewsGAT(**checkpoint['config']);model.load_state_dict(checkpoint['state_dict']);model.eval()
                for article in chosen:
                    key=s['docs'][article]['graph'];fi=s['mapping'][article:article+1]
                    text=s['text'][fi];sent=s['sent'][fi]
                    if mode=='no_content': text=np.zeros_like(text);sent=np.zeros_like(sent)
                    ei,ea=graph_arrays(s['graphs'][key],s['tickers'])
                    inputs=[torch.as_tensor(x) for x in (text,sent,s['mentions'][article:article+1],node_features(s['meta'],s['graphs'][key]),ei,ea)]
                    with torch.inference_mode(): logits,mag=model(*inputs)
                    pred=logits.softmax(-1).numpy()[0];mag=mag.numpy()[0]
                    positions=np.flatnonzero(s['article'][s['indices']]==article);nodes=s['node'][s['indices'][positions]]
                    error=max(float(abs(pred[nodes]-probabilities[positions]).max()),float(abs(mag[nodes]-magnitude[positions]).max()))
                    max_error=max(max_error,error);assert error<3e-6
                seed_p.append(probabilities);seed_m.append(magnitude)
            ensemble=np.load(attempt/'validation_predictions.npz')
            np.testing.assert_array_equal(ensemble['pair_indices'],s['indices'])
            np.testing.assert_array_equal(ensemble['probabilities'],np.mean(seed_p,axis=0))
            np.testing.assert_array_equal(ensemble['magnitude'],np.mean(seed_m,axis=0))
            results.append(dict(arm=arm,checkpoints=3,articles_per_checkpoint=len(chosen),cpu_max_error=max_error,
                                ensemble_exact=True,loss_selector_history_matches=True,selected_loss_max_error=max_loss_error))
    return dict(models=results,training_protocol_read=True)


def independent_hac(x,lag):
    x=np.asarray(x,dtype=float);n=len(x);centered=x-np.mean(x)
    covariance=[float(np.sum(centered[k:]*centered[:n-k]))/n for k in range(min(lag,n-1)+1)]
    variance=covariance[0]+sum(2*(1-k/(lag+1))*covariance[k] for k in range(1,len(covariance)))
    return float(np.sqrt(max(0.,variance)/n))


def verify_statistics(out,s):
    protocol=read(out/'training_protocol.json');sizes=s['prepared']['sizes']
    arms=[f'size_{n}_{mode}' for n in sizes for mode in ('text','no_content')]
    baseline_names=('original','ticker_prior','majority')
    contrasts=[(f'scale_{a}_to_{b}',f'size_{b}_text',f'size_{a}_text') for a,b in itertools.combinations(sizes,2)]
    contrasts += [(f'news_content_{n}',f'size_{n}_text',f'size_{n}_no_content') for n in sizes]
    contrasts += [(f'size_{n}_text_vs_{b}',f'size_{n}_text',b) for n in sizes for b in baseline_names]
    assert protocol['arms']==arms and protocol['contrasts']==[list(x) for x in contrasts]
    family=4*len(contrasts)
    assert protocol['correction_family']==family and protocol['hac_lags']==[5,20]
    assert protocol['validation_only'] and protocol['reused_validation'] and not protocol['fresh_holdout']
    assert not protocol['automatic_model_replacement'] and protocol['user_target']==.65
    assert protocol['settings']==dict(epochs=30,patience=6,min_delta=1e-5,batch_size=24,lr=.0008,
                                     weight_decay=.001,hidden=64,dropout=.15,gradient_clip=2.)
    full=s['pairs'].iloc[:4813];indices=s['indices'];retained=full.iloc[indices]
    y=s['pairs'][['y_1d','y_3d']].to_numpy(dtype='float32')
    with np.load(out/'label_config.npz') as cfg:
        actual=np.where(y>cfg['thresholds'],2,np.where(y < -cfg['thresholds'],0,1))
        magnitude_target=np.minimum(abs(y)/cfg['scales'],1).astype('float32')
    predictions={};full_baselines={}
    for arm in arms:
        folder=out/'models'/arm;attempt=folder/read(folder/'fit_complete.json')['attempt']
        with np.load(attempt/'validation_predictions.npz') as f:
            np.testing.assert_array_equal(f['pair_indices'],indices)
            predictions[arm]=(f['probabilities'].copy(),f['magnitude'].copy())
    baseline_paths={'original':ORIGINAL/'predictions_stage3.npz','majority':ORIGINAL/'predictions_majority.npz',
                    'ticker_prior':HERE/'artifacts/news_impact_v2_evidence/predictions_ticker_prior.npz'}
    for name,path in baseline_paths.items():
        with np.load(path) as f:
            p=f['probabilities'][s['original_validation_indices']];m=f['magnitude'][s['original_validation_indices']]
        full_baselines[name]=p;predictions[name]=(p[indices],m[indices])
    saved_metrics=pd.read_csv(out/'validation_metrics.csv').set_index(['model','market','horizon'])
    saved_comparisons=pd.read_csv(out/'validation_comparisons.csv')
    assert len(saved_metrics)==len(predictions)*4 and len(saved_comparisons)==2*family
    daily={};metric_error=0.;comparison_error=0.;expected_daily={}
    for name,(p,m) in predictions.items():
        for market in ('KOSPI','NASDAQ'):
            full_mask=full.market.eq(market).to_numpy();keep=retained.market.eq(market).to_numpy()
            for h in range(2):
                horizon=(1,3)[h]
                correct=p[:,h].argmax(-1)==actual[indices,h]
                all_correct=np.zeros(4813,dtype=float)
                all_correct[indices]=correct
                if name in full_baselines:all_correct=(full_baselines[name][:,h].argmax(-1)==actual[:4813,h]).astype(float)
                df=pd.DataFrame(dict(day=full.loc[full_mask,'anchor_date'].to_numpy(),value=all_correct[full_mask]))
                grouped=df.groupby('day').value.agg(['mean','sum','size']).sort_index();daily[name,market,horizon]=grouped['mean']
                retained_daily=pd.Series(correct[keep].astype(float),index=retained.loc[keep,'anchor_date']).groupby(level=0).mean()
                err=abs(m[keep,h].astype(float)-magnitude_target[indices,h][keep]);truth=actual[indices,h][keep]
                predicted=p[keep,h].argmax(-1)
                expected=dict(n_full=int(full_mask.sum()),n_retained=int(keep.sum()),n_sessions=len(grouped),
                    full_accuracy=float(grouped['mean'].mean()),full_pair_accuracy=float(all_correct[full_mask].mean()),
                    retained_accuracy=float(retained_daily.mean()),retained_pair_accuracy=float(correct[keep].mean()),
                    retained_balanced_accuracy=float(np.mean([np.mean(predicted[truth==c]==c) for c in range(3)])),
                    retained_magnitude_mae=float(err.mean()),
                    retained_session_magnitude_mae=float(pd.Series(err,index=retained.loc[keep,'anchor_date']).groupby(level=0).mean().mean()))
                for lag in (5,20):
                    se=independent_hac(grouped['mean'],lag)
                    expected[f'full_accuracy_hac_se_{lag}']=se
                    expected[f'full_accuracy_ci95_low_{lag}']=expected['full_accuracy']-norm.ppf(.975)*se
                    expected[f'full_accuracy_ci95_high_{lag}']=expected['full_accuracy']+norm.ppf(.975)*se
                row=saved_metrics.loc[name,market,horizon]
                for field,value in expected.items():metric_error=max(metric_error,abs(float(row[field])-value))
                for day,g in grouped.iterrows():expected_daily[name,market,horizon,str(day)]=(float(g['mean']),int(g['size']),int(g['sum']))
    for r in rows(out/'daily_scores.jsonl'):
        key=(r['model'],r['market'],r['horizon'],r['anchor_date'])
        value=expected_daily.pop(key)
        assert abs(r['accuracy']-value[0])<1e-15 and (r['n_pairs'],r['n_correct'])==value[1:]
    assert not expected_daily
    p_values=[]
    for row in saved_comparisons.itertuples():
        assert (row.contrast,row.model,row.baseline) in contrasts
        a=daily[row.model,row.market,row.horizon];b=daily[row.baseline,row.market,row.horizon]
        assert a.index.equals(b.index) and row.n_sessions==len(a)
        difference=a.to_numpy()-b.to_numpy();delta=float(difference.mean());se=independent_hac(difference,row.lag)
        p=float(2*norm.sf(abs(delta/se))) if se>0 else float(delta==0)
        for got,value in ((row.delta,delta),(row.se,se),(row.p,p),(row.ci95_low,delta-norm.ppf(.975)*se),(row.ci95_high,delta+norm.ppf(.975)*se)):
            comparison_error=max(comparison_error,abs(got-value))
        assert row.holm_family_size==family and not row.ci95_adjusted
        p_values.append(p)
    for lag in (5,20):
        positions=np.flatnonzero(saved_comparisons.lag.eq(lag));p=np.array(p_values)[positions]
        order=np.argsort(p);adjusted=np.empty(len(p));running=0.
        for rank,i in enumerate(order):running=max(running,min(1.,(len(p)-rank)*p[i]));adjusted[i]=running
        comparison_error=max(comparison_error,float(np.max(abs(saved_comparisons.iloc[positions].p_holm.to_numpy()-adjusted))))
    assert metric_error<1e-12 and comparison_error<1e-12
    evaluation=read(out/'evaluation.json');completion=read(out/'completion.json')
    for field in ('original_population_accuracy','conditional_accuracy','retained_magnitude_mae','retained_session_magnitude_mae'):
        column={'original_population_accuracy':'full_accuracy','conditional_accuracy':'retained_accuracy',
                'retained_magnitude_mae':'retained_magnitude_mae','retained_session_magnitude_mae':'retained_session_magnitude_mae'}[field]
        expected=saved_metrics.groupby(level='model')[column].mean().to_dict()
        for model,value in expected.items():assert abs(evaluation[field][model]-value)<1e-12
        assert completion[field]==evaluation[field]
    assert evaluation['target_met_by_text_size']=={str(n):evaluation['original_population_accuracy'][f'size_{n}_text']>=.65 for n in sizes}
    manifest=read(out/'training_output_manifest.json')
    for name,digest in manifest.items():assert sha(out/name)==digest,name
    return dict(metrics_checked=len(saved_metrics),hac_comparisons_checked=len(saved_comparisons),holm_family_per_lag=family,
                metric_max_error=metric_error,comparison_max_error=comparison_error,daily_scores_exact=True,
                output_manifest_files=len(manifest),frozen_original_baseline_indices_verified=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True)
    stage=parser.add_mutually_exclusive_group();stage.add_argument('--data-only',action='store_true');stage.add_argument('--features-only',action='store_true')
    args=parser.parse_args();out=args.out.resolve();result,s=read_and_check(out)
    write_frozen_audit(out/'data_verification.json',result)
    if not args.data_only:
        result['features']=verify_features(out,s)
        result['data_verification_sha256']=sha(out/'data_verification.json')
        result['features_complete_sha256']=sha(out/'features_complete.json')
        write_frozen_audit(out/'features_verification.json',result)
        if not args.features_only:
            result['models']=verify_models(out,s)
            result['statistics']=verify_statistics(out,s)
            dump(out/'verification.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__': main()
