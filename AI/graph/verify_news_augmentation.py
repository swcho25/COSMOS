"""Independent checks for retaining old news while adding current-event examples.

The event source is rechecked using its existing scalar-label and graph verifier.
This verifier then checks the new preparation against the original, parent feature
cache and event source, including every restored old training mask and time guard.
Full verification reloads nine CPU checkpoints, checks seed averaging/selection,
and independently recomputes equal-session metrics and HAC/Holm comparisons.
No test prediction or encoder download is performed. Passing is evidence of
execution/isolation, not certified historical identity or unseen-period accuracy.
"""
import argparse
import collections
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from scipy.stats import norm

from news_impact_data import close_times, graph_arrays, node_features
from target_news_features import clean_target_article
from target_news_model import TargetNewsGAT
from verify_current_events import read_and_check as verify_event_data
from verify_news_predictive_value import dump, sha

HERE = Path(__file__).resolve().parent
MODES = ('old_full', 'augmented', 'no_content')


def rows(path):
    with path.open(encoding='utf8') as stream:
        return [json.loads(line) for line in stream]


def first_observed_closes(meta):
    """Read actual positive adjusted prices; do not reuse preparation's guard."""
    raw = pd.read_parquet(HERE/'data/prices_daily.parquet')
    raw['date'] = pd.to_datetime(raw.trading_at).dt.tz_localize(None).dt.normalize()
    price = raw.pivot(index='date', columns='ticker', values='adj_close').sort_index()
    first = {}
    for market, group in meta.groupby('market'):
        panel = price.reindex(columns=group.ticker).dropna(how='all')
        closes = close_times(panel.index, market)
        for ticker in panel:
            values = panel[ticker].to_numpy()
            valid = np.flatnonzero(np.isfinite(values) & (values > 0))
            if len(valid):
                first[ticker] = closes[valid[0]]
    return first


def read_and_check(out):
    protocol = json.loads((out/'protocol.json').read_text(encoding='utf8'))
    prepared = json.loads((out/'prepared.json').read_text(encoding='utf8'))
    assert tuple(protocol['modes']) == MODES and protocol['seeds'] == [0, 1, 2]
    assert protocol['hac_lags'] == [5, 20] and protocol['correction_family'] == 20
    assert protocol['contrasts'] == [['augmented', b] for b in ('old_full','no_content','original','ticker_prior','majority')]
    source, parent, event_source = (Path(protocol[k]) for k in ('source', 'parent', 'event_source'))
    for path, digest in protocol['source_hashes'].items():
        assert sha(Path(path)) == digest, path
    for name, digest in protocol['code_hashes'].items():
        assert sha(HERE/name) == digest, name
    for name, digest in prepared['artifact_hashes'].items():
        assert sha(out/name) == digest, name

    # The graphs are deliberately fixed to this already prepared event source,
    # rather than reconstructed from the larger restored old training population.
    event_result, event = verify_event_data(event_source)
    assert source == event['source'] and parent == event['parent']
    old_docs = rows(source/'articles.jsonl')
    docs = rows(out/'articles.jsonl')
    old_pairs = pd.read_parquet(source/'pairs.parquet')
    pairs = pd.read_parquet(out/'pairs.parquet')
    pd.testing.assert_frame_equal(pairs, event['pairs'], check_exact=True)
    pd.testing.assert_frame_equal(old_pairs, pairs.iloc[:len(old_pairs)].reset_index(drop=True), check_exact=True)
    legacy = len(old_docs)
    assert len(docs) == len(event['docs'])
    assert docs[legacy:] == event['docs'][legacy:]
    assert prepared['legacy_articles'] == legacy and prepared['legacy_pairs'] == len(old_pairs)
    assert prepared['added_articles'] == len(docs)-legacy
    assert prepared['added_pairs'] == len(pairs)-len(old_pairs)
    for name in ('graphs.json', 'companies.json', 'label_config.npz', 'target_text.npy', 'target_sentiment.npy'):
        assert sha(out/name) == sha(event_source/name), name

    meta = event['meta']; tickers = meta.ticker.tolist()
    node = pairs.ticker.map({t:i for i,t in enumerate(tickers)}).to_numpy()
    article = pairs.article.to_numpy()
    mask = np.load(out/'direct_mask.npy')
    mapping = np.load(out/'feature_mapping.npy')
    mentions = np.load(out/'mentions.npy')
    assert mask.shape == mapping.shape == (len(docs), len(tickers))
    assert mentions.shape == (*mask.shape, 5) and mask.dtype == np.bool_
    for name, value in (('direct_mask', mask), ('feature_mapping', mapping), ('mentions', mentions)):
        np.testing.assert_array_equal(value[legacy:], np.load(event_source/(name+'.npy'))[legacy:])

    expected_mask = np.load(parent/'direct_mask.npy').copy()
    expected_mapping = np.load(parent/'feature_mapping.npy').copy()
    expected_mentions = np.load(parent/'mentions.npy').copy()
    first = first_observed_closes(meta)
    assert old_pairs.loc[old_pairs.split.eq('train'),'label_end'].le('2022-12-31').all()
    history_rejected = []; labelled_history_rejected = []; audit = {}
    for i, old in enumerate(old_docs):
        if old['split'] != 'train':
            continue
        available = pd.Timestamp(old['available_at'])
        for n in np.flatnonzero(expected_mapping[i] > 0):
            ticker = tickers[n]
            had_label = bool(expected_mask[i, n])
            before = ticker in first and first[ticker] <= available
            audit[i, ticker] = dict(record_id=old['record_id'], available_at=old['available_at'],
                eligible=before, had_label=had_label,
                first_observed_close=first[ticker].isoformat() if ticker in first else None)
            if not before:
                history_rejected.append((i, ticker))
                if had_label: labelled_history_rejected.append((i, ticker))
                expected_mask[i, n] = False
                expected_mapping[i, n] = 0
                expected_mentions[i, n] = 0
    np.testing.assert_array_equal(mask[:legacy], expected_mask)
    np.testing.assert_array_equal(mapping[:legacy], expected_mapping)
    np.testing.assert_array_equal(mentions[:legacy], expected_mentions)
    saved_audit = pd.read_parquet(out/'old_training_eligibility.parquet')
    assert len(saved_audit) == len(audit)
    assert not saved_audit.duplicated(['article', 'ticker']).any()
    for r in saved_audit.itertuples():
        expected = audit[r.article, r.ticker]
        for key, value in expected.items():
            assert getattr(r, key) == value, (r.article, r.ticker, key)

    for i, old in enumerate(old_docs):
        expected_split = old['split']
        if expected_split == 'test' or not expected_mask[i].any():
            expected_split = 'excluded'
        assert docs[i]['split'] == expected_split, (i, 'split')
        for key in ('record_id', 'published_date', 'available_at', 'region', 'graph'):
            assert docs[i][key] == old[key], (i, key)
        if old['split'] in ('train', 'val'):
            clean = clean_target_article(old)[0]
            assert docs[i]['title'] == clean['title'] and docs[i]['body'] == clean['body'], i
            if old['split'] == 'train':
                expected_companies=[m for m in old['companies'] if expected_mapping[i,tickers.index(m['ticker'])]>0]
                assert docs[i]['companies'] == expected_companies, (i, 'companies')
        else:
            assert docs[i]['title'] == old['title'] and docs[i]['body'] == old['body'], i

    indices = np.flatnonzero(pairs.split.eq('val').to_numpy() & pairs.is_direct.to_numpy() & mask[article, node])
    np.testing.assert_array_equal(indices, np.load(out/'pair_indices.npy'))
    np.testing.assert_array_equal(indices, event['indices'])
    full_ids = np.flatnonzero(old_pairs.split.eq('val').to_numpy() & old_pairs.is_direct.to_numpy())
    assert len(indices) == 4743 and len(full_ids) == 4813
    graphs = event['graphs']
    for d in docs:
        if d['split'] in ('train', 'val'):
            assert pd.Timestamp(graphs[d['graph']]['available_at']) < pd.Timestamp(d['available_at'])
    old_active = np.array([d['split'] == 'train' for d in docs[:legacy]])
    old_kept_pairs = int(mask[:legacy][old_active].sum())
    assert prepared['old_train_articles'] == int(old_active.sum())
    assert prepared['old_train_pairs'] == old_kept_pairs
    assert prepared['history_guard_excluded_inputs'] == len(history_rejected)
    assert prepared['history_guard_excluded_pairs'] == len(labelled_history_rejected)

    # Exercise the actual loader after computing the expected arrays independently.
    from experiment_news_augmentation import load_data
    arm_counts = {}
    for mode in MODES:
        loaded = load_data(out, mode)
        np.testing.assert_array_equal(loaded.mask, mask)
        np.testing.assert_array_equal(loaded.mentions, mentions)
        for i, actual in enumerate(loaded.docs):
            expected = 'excluded' if mode == 'old_full' and i >= legacy else docs[i]['split']
            assert actual['split'] == expected, (mode, i)
        active = np.array([d['split'] == 'train' for d in loaded.docs])
        arm_counts[mode] = dict(articles=int(active.sum()), pairs=int(loaded.mask[active].sum()))
        assert all(d['split'] != 'test' for d in loaded.docs)
        del loaded
    assert arm_counts['augmented'] == arm_counts['no_content']
    assert arm_counts['augmented']['articles']-arm_counts['old_full']['articles'] == prepared['added_articles']
    assert arm_counts['augmented']['pairs']-arm_counts['old_full']['pairs'] == prepared['added_pairs']
    assert prepared['total_train_articles'] == arm_counts['augmented']['articles']
    assert prepared['total_train_pairs'] == arm_counts['augmented']['pairs']
    result = dict(source_unchanged=True, legacy_articles=legacy, legacy_pairs=len(old_pairs),
        added_articles=len(docs)-legacy, added_pairs=len(pairs)-len(old_pairs),
        old_training_articles=int(old_active.sum()), old_training_pairs=old_kept_pairs,
        old_history_rejected_inputs=len(history_rejected), old_history_rejected_pairs=len(labelled_history_rejected),
        old_input_history_checks=len(audit), old_train_label_end_guard=True,
        original_direct_val_pairs=len(full_ids), retained_val_pairs=len(indices),
        arm_training_counts=arm_counts, frozen_event_source_verification=event_result,
        graphs_and_feature_cache_unchanged=True, original_pairs_prefix_exact=True,
        legal_identity_certified=False, human_review_claim=False, validation_only=True)
    state = dict(protocol=protocol, prepared=prepared, source=source, parent=parent,
        docs=docs, pairs=pairs, meta=meta, tickers=tickers, node=node, article=article,
        mask=mask, mapping=mapping, mentions=mentions, indices=indices, full_ids=full_ids,
        graphs=graphs, text=np.load(out/'target_text.npy'), sent=np.load(out/'target_sentiment.npy'),
        threshold=event['threshold'], scale=event['scale'])
    return result, state


def replay_and_metrics(out, state):
    s=state; protocol=s['protocol']; docs=s['docs']; pairs=s['pairs']; indices=s['indices']
    val=pairs.iloc[indices]; full_ids=s['full_ids']; full=pairs.iloc[full_ids]
    selected=[]
    for market in ('KOSPI', 'NASDAQ'):
        ids=np.flatnonzero(val.market.eq(market).to_numpy())
        selected.extend([int(ids[0]), int(ids[len(ids)//2]), int(ids[-1])])
    article_ids=sorted({int(val.iloc[j].article) for j in selected})
    outputs={}; max_cpu=0.; checkpoints=0
    for mode in MODES:
        folder=out/mode
        text,sent=(np.zeros_like(s['text']), np.zeros_like(s['sent'])) if mode=='no_content' else (s['text'], s['sent'])
        with np.load(folder/'validation_predictions.npz') as f:
            np.testing.assert_array_equal(f['pair_indices'], indices)
            prob,mag=f['probabilities'],f['magnitude']
        history=pd.read_csv(folder/'training_history.csv')
        assert set(history.seed)==set(protocol['seeds'])
        seed_probs=[]; seed_mags=[]
        for seed in protocol['seeds']:
            with np.load(folder/f'validation_seed{seed}.npz') as f:
                np.testing.assert_array_equal(f['pair_indices'], indices)
                pp,mm=f['probabilities'],f['magnitude']
                assert pp.shape==(len(indices),2,3) and mm.shape==(len(indices),2)
                assert np.isfinite(pp).all() and np.isfinite(mm).all()
                assert (pp>=0).all() and (pp<=1).all() and (mm>=0).all() and (mm<=1).all()
                np.testing.assert_allclose(pp.sum(-1),1,atol=1e-6)
                seed_probs.append(pp); seed_mags.append(mm)
            checkpoint=torch.load(folder/f'gat_seed{seed}.pt',map_location='cpu',weights_only=True)
            assert checkpoint['loss']=='direct_only' and checkpoint['mode']==mode
            epochs=history.loc[history.seed.eq(seed)].sort_values('epoch')
            assert epochs.epoch.tolist()==list(range(len(epochs))) and 1<=len(epochs)<=30
            assert np.isfinite(epochs[['train_loss','val_loss']]).all().all()
            best=float('inf'); best_epoch=-1
            for r in epochs.itertuples():
                if r.val_loss<best-1e-5: best,best_epoch=r.val_loss,int(r.epoch)
                elif r.epoch-best_epoch>=6: assert r.epoch==epochs.epoch.iloc[-1]
            assert checkpoint['selection']['best_epoch']==best_epoch
            assert abs(checkpoint['selection']['val_loss']-best)<1e-12
            model=TargetNewsGAT(**checkpoint['config']).eval(); model.load_state_dict(checkpoint['state_dict'])
            for i in article_ids:
                graph=s['graphs'][docs[i]['graph']]; edge,attr=graph_arrays(graph,s['tickers'])
                feature=s['mapping'][i:i+1]
                arrays=(text[feature],sent[feature],s['mentions'][i:i+1],node_features(s['meta'],graph),edge,attr)
                with torch.inference_mode():
                    logits,pred=model(*(torch.from_numpy(x) for x in arrays))
                    p=logits.softmax(-1)[0].numpy(); m=pred[0].numpy()
                selected_rows=np.flatnonzero(val.article.to_numpy()==i)
                nodes=s['node'][indices[selected_rows]]
                for actual,expected in ((p[nodes],pp[selected_rows]),(m[nodes],mm[selected_rows])):
                    np.testing.assert_allclose(actual,expected,atol=2e-5,rtol=1e-4)
                    max_cpu=max(max_cpu,float(abs(actual-expected).max()))
            checkpoints+=1
        np.testing.assert_array_equal(np.mean(seed_probs,axis=0),prob)
        np.testing.assert_array_equal(np.mean(seed_mags,axis=0),mag)
        outputs[mode]=(prob,mag)

    baseline_paths={'original':s['source']/'predictions_stage3.npz',
        'majority':s['source']/'predictions_majority.npz',
        'ticker_prior':HERE/'artifacts/news_impact_v2_evidence/predictions_ticker_prior.npz'}
    baseline_full={}
    for name,path in baseline_paths.items():
        assert sha(path)==protocol['source_hashes'][str(path)],path
        with np.load(path) as f:
            outputs[name]=(f['probabilities'][indices],f['magnitude'][indices])
            baseline_full[name]=f['probabilities'][full_ids]
    y=pairs[['y_1d','y_3d']].to_numpy(dtype='float32')
    cls=np.where(y>s['threshold'],2,np.where(y< -s['threshold'],0,1))
    metrics=pd.read_csv(out/'validation_metrics.csv'); metric_errors=[]
    assert len(metrics)==len(outputs)*4
    expected_keys={(name,market,h) for name in outputs for market in ('KOSPI','NASDAQ') for h in (1,3)}
    assert set(zip(metrics.model,metrics.market,metrics.horizon))==expected_keys
    for r in metrics.itertuples():
        sel=val.market.eq(r.market).to_numpy(); h={1:0,3:1}[r.horizon]; actual=cls[indices][sel,h]
        pred=outputs[r.model][0][sel,h].argmax(-1); correct=(pred==actual).astype(float)
        daily=pd.Series(correct,index=val.loc[sel,'anchor_date']).groupby(level=0).mean()
        values=[daily.mean(),correct.mean(),np.mean([(pred[actual==k]==k).mean() for k in range(3)])]
        saved=[r.session_accuracy,r.pair_accuracy,r.balanced_accuracy]
        np.testing.assert_allclose(values,saved,atol=1e-12)
        metric_errors.extend(abs(np.array(values)-saved).tolist())
        assert r.n_pairs==sel.sum() and r.n_sessions==len(daily)

    comparisons=pd.read_csv(out/'validation_comparisons.csv'); stat_errors=[]
    expected_baselines={'old_full','no_content','original','majority','ticker_prior'}
    assert len(comparisons)==40 and set(comparisons.baseline)==expected_baselines
    expected_comparisons={(baseline,market,h,lag) for baseline in expected_baselines
        for market in ('KOSPI','NASDAQ') for h in (1,3) for lag in (5,20)}
    assert set(zip(comparisons.baseline,comparisons.market,comparisons.horizon,comparisons.lag))==expected_comparisons
    assert comparisons.model.eq('augmented').all()
    for r in comparisons.itertuples():
        sel=val.market.eq(r.market).to_numpy(); h={1:0,3:1}[r.horizon]; actual=cls[indices][sel,h]
        a=(outputs['augmented'][0][sel,h].argmax(-1)==actual).astype(float)
        b=(outputs[r.baseline][0][sel,h].argmax(-1)==actual).astype(float)
        daily=pd.Series(a-b,index=val.loc[sel,'anchor_date']).groupby(level=0).mean().sort_index().to_numpy()
        delta=daily.mean(); z=daily-delta; n=len(z); variance=float(z@z)/n
        for lag in range(1,min(r.lag,n-1)+1):
            variance+=2*(1-lag/(r.lag+1))*float(z[lag:]@z[:-lag])/n
        se=np.sqrt(max(variance,0)/n); p=float(2*norm.sf(abs(delta/se))) if se else float(delta==0)
        np.testing.assert_allclose([delta,se,p],[r.delta,r.se,r.p],atol=1e-12,rtol=1e-9)
        stat_errors.append(float(max(abs(delta-r.delta),abs(se-r.se),abs(p-r.p))))
    for lag,group in comparisons.groupby('lag'):
        assert lag in (5,20) and len(group)==20
        ordered=group.sort_values('p'); running=0.
        for j,r in enumerate(ordered.itertuples()):
            running=max(running,min(1,r.p*(len(ordered)-j)))
            assert abs(r.p_holm_20-running)<1e-10

    adjusted=pd.read_csv(out/'coverage_adjusted_metrics.csv')
    assert len(adjusted)==len(expected_keys)
    assert set(zip(adjusted.model,adjusted.market,adjusted.horizon))==expected_keys
    scores_by_model=collections.defaultdict(list); positions=np.searchsorted(full_ids,indices)
    for r in adjusted.itertuples():
        h={1:0,3:1}[r.horizon]; sel=full.market.eq(r.market).to_numpy()
        if r.model in MODES:
            correct=np.zeros(len(full)); correct[positions]=outputs[r.model][0][:,h].argmax(-1)==cls[indices,h]
        else:
            correct=(baseline_full[r.model][:,h].argmax(-1)==cls[full_ids,h]).astype(float)
        score=pd.Series(correct[sel],index=full.loc[sel,'anchor_date']).groupby(level=0).mean().mean()
        assert abs(score-r.original_population_accuracy)<1e-12
        scores_by_model[r.model].append(float(score))
    scores={k:float(np.mean(v)) for k,v in scores_by_model.items()}
    completion=json.loads((out/'completion.json').read_text())
    assert completion['validation_only'] and not completion['automatic_model_replacement']
    assert completion['original_direct_pairs']==4813 and completion['retained_pairs']==4743
    for k,v in scores.items():
        assert abs(v-completion['original_population_accuracy'][k])<1e-12
        conditional=float(metrics.loc[metrics.model.eq(k),'session_accuracy'].mean())
        assert abs(conditional-completion['conditional_accuracy'][k])<1e-12
    assert completion['user_target_met']==(scores['augmented']>=.65)
    assert not list(out.rglob('*test*predictions*'))
    return dict(checkpoints=checkpoints,replayed_val_articles=len(article_ids),max_cpu_prediction_error=max_cpu,
        seed_ensemble_exact=True,early_stopping_selections_checked=checkpoints,
        metric_rows=len(metrics),max_metric_error=max(metric_errors),statistical_comparisons=len(comparisons),
        max_statistical_error=max(stat_errors),original_population_accuracy=scores,
        user_target_met=scores['augmented']>=.65,no_new_encoder_fit=True,no_new_test_predictions=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--experiment',type=Path,required=True)
    parser.add_argument('--data-only',action='store_true')
    args=parser.parse_args(); out=args.experiment.resolve(); torch.set_num_threads(4)
    result,state=read_and_check(out)
    if not args.data_only:
        result.update(replay_and_metrics(out,state))
    result['verification_code_sha256']=sha(Path(__file__))
    dump(out/('data_verification.json' if args.data_only else 'reload_verification.json'),result)
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
