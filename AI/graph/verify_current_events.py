"""Independent preservation, label arithmetic, graph and CPU replay checks.

--data-only runs before training. Full verification additionally replays six new
target encoder inputs and all nine CPU GAT checkpoints, recomputes session/HAC
metrics and retains all original validation rows in the target denominator.
This verifies execution and isolation, not human annotation quality or identity.
"""
import argparse
import collections
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from scipy.stats import norm

from news_impact_data import availability, close_times, graph_arrays, node_features
from news_quality import text_digest
from target_news_features import clean_target_article, TargetEncoder
from target_news_model import TargetNewsGAT
from verify_news_predictive_value import sha, dump

HERE = Path(__file__).resolve().parent


def rows(path):
    with path.open(encoding='utf8') as f:
        return [json.loads(line) for line in f]


def read_and_check(out):
    protocol = json.loads((out/'protocol.json').read_text(encoding='utf8'))
    prepared = json.loads((out/'prepared.json').read_text(encoding='utf8'))
    source, parent = Path(protocol['source']), Path(protocol['parent'])
    for path, digest in protocol['source_hashes'].items():
        assert sha(Path(path)) == digest, path
    for name, digest in protocol['code_hashes'].items():
        assert sha(HERE/name) == digest, name
    for name, digest in prepared['artifact_hashes'].items():
        assert sha(out/name) == digest, name
    old_docs, docs = rows(source/'articles.jsonl'), rows(out/'articles.jsonl')
    old_pairs, pairs = pd.read_parquet(source/'pairs.parquet'), pd.read_parquet(out/'pairs.parquet')
    pd.testing.assert_frame_equal(old_pairs, pairs.iloc[:len(old_pairs)].reset_index(drop=True), check_exact=True)
    assert len(old_docs) == prepared['legacy_articles']
    assert len(old_pairs) == prepared['legacy_pairs']
    assert len(docs)-len(old_docs) == prepared['added_articles']
    assert len(pairs)-len(old_pairs) == prepared['added_pairs']
    for i, old in enumerate(old_docs):
        for k in ('record_id','published_date','available_at','region','graph'):
            assert docs[i][k] == old[k], (i,k)
        if old['split']=='test':
            assert docs[i]['split']=='excluded'
    assert sha(out/'label_config.npz') == sha(source/'label_config.npz')
    assert sha(out/'companies.json') == sha(source/'companies.json')
    cfg=np.load(out/'label_config.npz');threshold,scale=cfg['thresholds'],cfg['scales']
    meta=pd.read_json(out/'companies.json',dtype={'ticker':str});tickers=meta.ticker.tolist()
    node=pairs.ticker.map({t:i for i,t in enumerate(tickers)}).to_numpy();article=pairs.article.to_numpy()
    mask=np.load(out/'direct_mask.npy');mapping=np.load(out/'feature_mapping.npy');mentions=np.load(out/'mentions.npy')
    assert mask.shape == mapping.shape == (len(docs),len(tickers))
    assert mentions.shape == (*mask.shape,5)
    original_mask=np.load(parent/'direct_mask.npy')
    val_articles=np.array([r['split']=='val' for r in old_docs])
    np.testing.assert_array_equal(mask[:len(old_docs)][val_articles],original_mask[val_articles])
    for name,new in [('feature_mapping',mapping),('mentions',mentions)]:
        np.testing.assert_array_equal(new[:len(old_docs)][val_articles],np.load(parent/(name+'.npy'))[val_articles])
    indices=np.flatnonzero(pairs.split.eq('val').to_numpy() & pairs.is_direct.to_numpy() & mask[article,node])
    np.testing.assert_array_equal(indices,np.load(out/'pair_indices.npy'))
    old_full=np.flatnonzero(old_pairs.split.eq('val').to_numpy() & old_pairs.is_direct.to_numpy())
    assert len(indices)==4743 and len(old_full)==4813

    # Independent price reading and scalar endpoint arithmetic. No Labels.label().
    raw=pd.read_parquet(HERE/'data/prices_daily.parquet')
    raw['date']=pd.to_datetime(raw.trading_at).dt.tz_localize(None).dt.normalize()
    price=raw.pivot(index='date',columns='ticker',values='adj_close').sort_index()
    market_by_ticker=meta.set_index('ticker').market.to_dict();panels={}
    for market,group in meta.groupby('market'):
        p=price.reindex(columns=group.ticker).dropna(how='all')
        assert (p.where(p.notna(),1)>0).all().all()
        f=pd.read_csv(source/f'index_{market}.csv')
        index=pd.Series(f.Close.to_numpy(),index=pd.to_datetime(f.Date.str[:10])).reindex(p.index)
        panels[market]=(p,index,close_times(p.index,market))
    held_ids={r['record_id'] for r in old_docs if r['split'] in ('val','test')}
    held_hashes=set()
    for r in old_docs:
        if r['split'] in ('val','test'):
            held_hashes.add(text_digest(r['body']))
            held_hashes.add(text_digest(clean_target_article(r)[0]['body']))
    new_ids=set();new_hashes=set()
    for r in docs[len(old_docs):]:
        assert r['split']=='train' and '2017-01-01'<=r['published_date']<='2022-12-31'
        assert pd.Timestamp(r['available_at'])==availability(r['published_date'],r['region'])
        assert r['record_id'] not in held_ids|new_ids
        digest=text_digest(r['body']);assert digest not in held_hashes|new_hashes
        new_ids.add(r['record_id']);new_hashes.add(digest)
    max_label_error=0.;new_pairs=pairs.iloc[len(old_pairs):]
    for r in new_pairs.itertuples():
        d=docs[r.article];p,index,closes=panels[r.market];available=pd.Timestamp(d['available_at'])
        assert r.split=='train' and r.is_direct and r.path_length==0 and r.news_id==d['record_id']
        assert market_by_ticker[r.ticker]==r.market
        assert r.ticker in {m['ticker'] for m in d['companies']}
        assert mask[r.article,node[r.Index]]
        finite=np.flatnonzero(np.isfinite(p[r.ticker].to_numpy()) & (p[r.ticker].to_numpy()>0))
        assert len(finite) and closes[finite[0]]<=available
        anchor=int(np.searchsorted(closes.asi8,available.value,side='right'))
        assert r.anchor_date==p.index[anchor].strftime('%Y-%m-%d')
        assert r.label_end==p.index[anchor+3].strftime('%Y-%m-%d') and r.label_end<='2022-12-31'
        stock=p[r.ticker].iloc[anchor:anchor+4].to_numpy();market=index.iloc[anchor:anchor+4].to_numpy()
        assert np.isfinite(stock).all() and np.isfinite(market).all() and min(stock)>0 and min(market)>0
        expected=np.log(stock[1:]/stock[:-1])-np.log(market[1:]/market[:-1])
        actual=np.array([r.y_1d,r.y_3d]);calculated=np.array([expected[0],expected.sum()])
        np.testing.assert_allclose(actual,calculated,rtol=1e-9,atol=1e-12)
        max_label_error=max(max_label_error,float(abs(actual-calculated).max()))

    # Reconstruct all co-mention counts; old relation/ownership/industry edges forbidden.
    graphs=json.loads((out/'graphs.json').read_text(encoding='utf8'));graph_checks=[]
    for key,g in graphs.items():
        assert key in {str(y) for y in range(2017,2024)} and not g['industry_available']
        cut=pd.Timestamp(g['available_at']);co=collections.Counter();evidence={}
        for d in docs:
            if d['split']!='train' or pd.Timestamp(d['available_at'])>=cut:
                continue
            for a,b in itertools.combinations(sorted({m['ticker'] for m in d['companies']}),2):
                co[a,b]+=1;evidence[d['record_id']]=d
        neighbors=collections.defaultdict(list)
        for (a,b),n in co.items():
            if n>=3:
                w=n/(n+5);neighbors[a].append((w,b));neighbors[b].append((w,a))
        expected_co={(a,b):w for a,ns in neighbors.items() for w,b in sorted(ns,key=lambda z:(-z[0],z[1]))[:4]}
        actual_co={};since=f'{int(key)-1}-01-01'
        prior=np.log(price.loc[(price.index>=since)&(price.index<g['as_of'])]).diff()
        actual_corr={};seen=set()
        for e in g['edges']:
            a,b=e['src'],e['dst'];kind=e['type']
            assert a in tickers and b in tickers and a!=b and kind in ('co_mention','correlation')
            assert (a,b,kind) not in seen;seen.add((a,b,kind))
            assert not e['reverse']
            if kind=='co_mention':
                actual_co[a,b]=e['weight'];assert e['sign']==1
                for ev in e['evidence']:
                    d=evidence[ev['id']]
                    assert ev['available_at']==d['available_at'] and pd.Timestamp(ev['available_at'])<cut
                    assert ev['date']==d['published_date'] and {a,b}<={m['ticker'] for m in d['companies']}
            else:
                assert market_by_ticker[a]==market_by_ticker[b]
                window=prior[[a,b]].dropna();assert len(window)>=60
                corr=float(np.corrcoef(window[a],window[b])[0,1])
                np.testing.assert_allclose([e['weight'],e['sign']],[abs(corr),np.sign(corr)],atol=1e-12)
                assert abs(corr)>=.05
                assert e['evidence'][0]['date']==since
                actual_corr[a,b]=e['weight']
        assert actual_co.keys()==expected_co.keys()
        for pair,w in actual_co.items():assert abs(w-expected_co[pair])<1e-15
        expected_corr={}
        for _,group in meta.groupby('market'):
            c=prior.reindex(columns=group.ticker).corr(min_periods=60)
            for a in c:
                s=c[a].drop(index=a).dropna()
                for b in s.abs().sort_values(ascending=False).head(4).index:
                    if abs(s[b])>=.05:expected_corr[a,b]=abs(s[b])
        assert actual_corr.keys()==expected_corr.keys()
        graph_checks.append(dict(graph=key,co_mention=len(actual_co),correlation=len(actual_corr)))

    text=np.load(out/'target_text.npy');sent=np.load(out/'target_sentiment.npy')
    old_text=np.load(parent/'target_text.npy');old_sent=np.load(parent/'target_sentiment.npy');offset=len(old_text)
    np.testing.assert_array_equal(text[:offset],old_text);np.testing.assert_array_equal(sent[:offset],old_sent)
    entries=json.loads((out/'new_feature_entries.json').read_text(encoding='utf8'))
    embedding=np.load(out/'new_target_embedding.npy');new_sent=np.load(out/'new_target_sentiment.npy')
    assert len(entries)==len(new_pairs) and len(embedding)==len(entries)+1
    for j,e in enumerate(entries):
        assert e['article']>=len(old_docs) and mapping[e['article'],tickers.index(e['ticker'])]==offset+j
        assert mask[e['article'],tickers.index(e['ticker'])]
    with np.load(parent/'target_projection.npz') as projection:
        expected=(embedding[1:]@projection['components'].T-projection['mean']@projection['components'].T).astype('float32')
    np.testing.assert_allclose(text[offset:],expected,atol=1e-7,rtol=1e-5)
    np.testing.assert_array_equal(sent[offset:],new_sent[1:])
    assert mapping.max()<len(text) and mapping.min()>=0
    for d in docs:
        if d['split'] in ('train','val'):
            assert pd.Timestamp(graphs[d['graph']]['available_at'])<pd.Timestamp(d['available_at'])
    result=dict(source_unchanged=True,legacy_articles=len(old_docs),legacy_pairs=len(old_pairs),
        added_articles=len(docs)-len(old_docs),added_pairs=len(new_pairs),
        full_new_label_recalculations=len(new_pairs)*2,max_label_error=max_label_error,
        original_direct_val_pairs=len(old_full),retained_val_pairs=len(indices),
        holdout_id_body_overlap=0,graph_checks=graph_checks,frozen_thresholds=threshold.tolist(),
        legal_identity_certified=False,human_review_claim=False)
    state=dict(protocol=protocol,prepared=prepared,source=source,parent=parent,docs=docs,pairs=pairs,
        meta=meta,tickers=tickers,node=node,article=article,mask=mask,mapping=mapping,mentions=mentions,
        indices=indices,graphs=graphs,text=text,sent=sent,entries=entries,embedding=embedding,new_sent=new_sent,
        threshold=threshold,scale=scale)
    return result,state


def replay_and_metrics(out,state):
    s=state;protocol=s['protocol'];docs=s['docs'];pairs=s['pairs'];indices=s['indices'];val=pairs.iloc[indices]
    selected=[]
    for market in ('KOSPI','NASDAQ'):
        ids=np.flatnonzero(val.market.eq(market).to_numpy())
        selected.extend([int(ids[0]),int(ids[len(ids)//2]),int(ids[-1])])
    article_ids=sorted({int(val.iloc[j].article) for j in selected})
    outputs={};max_cpu=0.;checkpoints=0
    for mode in protocol['modes']:
        folder=out/mode;text,sent=(np.zeros_like(s['text']),np.zeros_like(s['sent'])) if mode=='no_content' else (s['text'],s['sent'])
        with np.load(folder/'validation_predictions.npz') as f:
            np.testing.assert_array_equal(f['pair_indices'],indices);prob,mag=f['probabilities'],f['magnitude']
        seed_probs=[];seed_mags=[]
        for seed in protocol['seeds']:
            with np.load(folder/f'validation_seed{seed}.npz') as f:
                np.testing.assert_array_equal(f['pair_indices'],indices);pp,mm=f['probabilities'],f['magnitude']
                assert np.isfinite(pp).all() and np.isfinite(mm).all()
                assert (pp>=0).all() and (pp<=1).all() and (mm>=0).all() and (mm<=1).all()
                np.testing.assert_allclose(pp.sum(-1),1,atol=1e-6);seed_probs.append(pp);seed_mags.append(mm)
            ckpt=torch.load(folder/f'gat_seed{seed}.pt',map_location='cpu',weights_only=True)
            assert ckpt['loss']=='direct_only'
            model=TargetNewsGAT(**ckpt['config']).eval();model.load_state_dict(ckpt['state_dict'])
            for i in article_ids:
                graph=s['graphs'][docs[i]['graph']];e,a=graph_arrays(graph,s['tickers']);fi=s['mapping'][i:i+1]
                inputs=[torch.from_numpy(x) for x in (text[fi],sent[fi],s['mentions'][i:i+1],node_features(s['meta'],graph),e,a)]
                with torch.inference_mode():
                    logits,pred=model(*inputs);p=logits.softmax(-1)[0].numpy();m=pred[0].numpy()
                vrows=np.flatnonzero(val.article.to_numpy()==i);ni=s['node'][indices[vrows]]
                for actual,expected in ((p[ni],pp[vrows]),(m[ni],mm[vrows])):
                    np.testing.assert_allclose(actual,expected,atol=2e-5,rtol=1e-4)
                    max_cpu=max(max_cpu,float(abs(actual-expected).max()))
            checkpoints+=1
        np.testing.assert_array_equal(np.mean(seed_probs,axis=0),prob)
        np.testing.assert_array_equal(np.mean(seed_mags,axis=0),mag)
        outputs[mode]=(prob,mag)
    baseline_hashes=json.loads((out/'baseline_hashes.json').read_text())
    for path,digest in baseline_hashes.items():assert sha(Path(path))==digest,path
    paths={'original':s['source']/'predictions_stage3.npz','majority':s['source']/'predictions_majority.npz',
        'ticker_prior':HERE/'artifacts/news_impact_v2_evidence/predictions_ticker_prior.npz'}
    full_ids=np.flatnonzero(pairs.split.eq('val').to_numpy() & pairs.is_direct.to_numpy());full=pairs.iloc[full_ids]
    y=pairs[['y_1d','y_3d']].to_numpy(dtype='float32');cls=np.where(y>s['threshold'],2,np.where(y< -s['threshold'],0,1))
    baseline_full={}
    for name,path in paths.items():
        with np.load(path) as f:
            outputs[name]=(f['probabilities'][indices],f['magnitude'][indices]);baseline_full[name]=f['probabilities'][full_ids]
    metrics=pd.read_csv(out/'validation_metrics.csv');metric_errors=[]
    for r in metrics.itertuples():
        sel=val.market.eq(r.market).to_numpy();h={1:0,3:1}[r.horizon];actual=cls[indices][sel,h]
        pred=outputs[r.model][0][sel,h].argmax(-1);correct=(pred==actual).astype(float)
        daily=pd.Series(correct,index=val.loc[sel,'anchor_date']).groupby(level=0).mean()
        values=[daily.mean(),correct.mean(),np.mean([(pred[actual==k]==k).mean() for k in range(3)])]
        saved=[r.session_accuracy,r.pair_accuracy,r.balanced_accuracy]
        np.testing.assert_allclose(values,saved,atol=1e-12);metric_errors.extend(abs(np.array(values)-saved).tolist())
        assert r.n_pairs==sel.sum() and r.n_sessions==len(daily)
    comparisons=pd.read_csv(out/'validation_comparisons.csv');stat_errors=[]
    for r in comparisons.itertuples():
        sel=val.market.eq(r.market).to_numpy();h={1:0,3:1}[r.horizon];actual=cls[indices][sel,h]
        a=(outputs['expanded'][0][sel,h].argmax(-1)==actual).astype(float)
        b=(outputs[r.baseline][0][sel,h].argmax(-1)==actual).astype(float)
        daily=pd.Series(a-b,index=val.loc[sel,'anchor_date']).groupby(level=0).mean().sort_index().to_numpy()
        delta=daily.mean();z=daily-delta;n=len(z);variance=float(z@z)/n
        for lag in range(1,min(r.lag,n-1)+1):
            variance+=2*(1-lag/(r.lag+1))*float(z[lag:]@z[:-lag])/n
        se=np.sqrt(max(variance,0)/n);p=float(2*norm.sf(abs(delta/se))) if se else float(delta==0)
        np.testing.assert_allclose([delta,se,p],[r.delta,r.se,r.p],atol=1e-12,rtol=1e-9)
        stat_errors.append(float(max(abs(delta-r.delta),abs(se-r.se),abs(p-r.p))))
    for lag,group in comparisons.groupby('lag'):
        assert len(group)==20
        ordered=group.sort_values('p');running=0.
        for j,r in enumerate(ordered.itertuples()):
            running=max(running,min(1,r.p*(len(ordered)-j)))
            assert abs(r.p_holm_20-running)<1e-10
    adjusted=pd.read_csv(out/'coverage_adjusted_metrics.csv');score_by_model=collections.defaultdict(list)
    pos=np.searchsorted(full_ids,indices)
    for r in adjusted.itertuples():
        h={1:0,3:1}[r.horizon];sel=full.market.eq(r.market).to_numpy()
        if r.model in protocol['modes']:
            correct=np.zeros(len(full));correct[pos]=outputs[r.model][0][:,h].argmax(-1)==cls[indices,h]
        else:correct=(baseline_full[r.model][:,h].argmax(-1)==cls[full_ids,h]).astype(float)
        score=pd.Series(correct[sel],index=full.loc[sel,'anchor_date']).groupby(level=0).mean().mean()
        assert abs(score-r.original_population_accuracy)<1e-12;score_by_model[r.model].append(float(score))
    scores={k:float(np.mean(v)) for k,v in score_by_model.items()}
    completion=json.loads((out/'completion.json').read_text())
    assert completion['validation_only'] and not completion['automatic_model_replacement']
    for k,v in scores.items():assert abs(v-completion['original_population_accuracy'][k])<1e-12
    assert completion['user_target_met']==(scores['expanded']>=.65)

    # Fresh CPU encoding of deterministic new train subjects, with exact stored spans.
    sample=[]
    for market in ('KOSPI','NASDAQ'):
        ix=[j for j,e in enumerate(s['entries']) if s['meta'].set_index('ticker').loc[e['ticker'],'market']==market]
        if ix:sample.extend([ix[0],ix[len(ix)//2],ix[-1]])
    selected_entries=[];sample_docs=[]
    for i,j in enumerate(sample):
        entry=s['entries'][j];sample_docs.append(docs[entry['article']]);selected_entries.append(dict(entry,article=i))
    encoder=TargetEncoder(s['parent']/'training_inputs',device='cpu');names=s['meta'].set_index('ticker').name_official.to_dict()
    emb,_=encoder.encode_entries('embedding',sample_docs,selected_entries,names);encoder.release()
    sent=np.zeros((len(sample)+1,3),dtype='float32')
    for role in ('sentiment_ko','sentiment_en'):
        value,_=encoder.encode_entries(role,sample_docs,selected_entries,names);sent+=value;encoder.release()
    np.testing.assert_allclose(emb[1:],s['embedding'][np.array(sample)+1],atol=2e-5,rtol=1e-4)
    np.testing.assert_allclose(sent[1:],s['new_sent'][np.array(sample)+1],atol=2e-5,rtol=1e-4)
    encoder_error=max(float(abs(emb[1:]-s['embedding'][np.array(sample)+1]).max()),float(abs(sent[1:]-s['new_sent'][np.array(sample)+1]).max()))
    return dict(checkpoints=checkpoints,replayed_val_articles=len(article_ids),max_cpu_prediction_error=max_cpu,
        raw_encoder_pairs=len(sample),raw_encoder_max_error=encoder_error,metric_rows=len(metrics),
        max_metric_error=max(metric_errors),statistical_comparisons=len(comparisons),max_statistical_error=max(stat_errors),
        original_population_accuracy=scores,user_target_met=scores['expanded']>=.65,validation_only=True)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--experiment',type=Path,required=True);ap.add_argument('--data-only',action='store_true')
    args=ap.parse_args();out=args.experiment.resolve();torch.set_num_threads(4)
    result,state=read_and_check(out)
    if not args.data_only:result.update(replay_and_metrics(out,state))
    result['verification_code_sha256']=sha(Path(__file__))
    dump(out/('data_verification.json' if args.data_only else 'reload_verification.json'),result)
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
