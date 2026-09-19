"""Fixed target-specific GAT experiment after the 100-pair qualitative audit.

Train/val only. Keep 3-class delayed excess-return labels, thresholds and graph
snapshots. Input-only hygiene is identical for three freshly trained arms: target
contexts, article mean of exactly those contexts, zero content. Direct labels only.
The user target is >=.65 direct equal-session accuracy averaged across 4 cells;
never change the metric, discard difficult labels, or call reused val a fresh test.
Report all eligibility exclusions before fitting and compare every baseline on the
same retained original pair IDs. Legacy graph construction/metadata limitations
remain; this experiment does not certify that the historical corpus is clean.
"""
import argparse
import collections
import copy
import json
import time
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from sklearn.decomposition import PCA
from scipy.stats import norm
from train_news_impact import TrainingData
from train_target_news import fit_target
from target_news_features import clean_target_article,valid_target_hit,TargetEncoder,CompanyMatcher
from news_text_features import feature_manifest
from verify_news_predictive_value import sha,dump,holm,COPIES
from audit_news_impact import hac_mean_se

HERE=Path(__file__).resolve().parent
MODES=('target','pooled','no_content')
BASELINES=('pooled','no_content','original','balanced_original','balanced_no_content','majority','ticker_prior','stage2')


def evaluate(data,indices,outputs,out):
    rows=[];comparisons=[];pairs=data.pairs.iloc[indices];cls=data.cls[indices];mag=data.mag[indices]
    for market in ('KOSPI','NASDAQ'):
        mask=pairs.market.eq(market).to_numpy();codes,dates=pd.factorize(pairs.loc[mask,'anchor_date'],sort=True);counts=np.bincount(codes)
        def daily(x):return np.bincount(codes,weights=x)/counts
        for h,horizon in enumerate((1,3)):
            actual=cls[mask,h];correct={};base=dict(market=market,horizon=horizon,n_pairs=int(mask.sum()),n_sessions=len(dates))
            for name,(p,m) in outputs.items():
                assert p.shape==(len(indices),2,3) and m.shape==(len(indices),2)
                assert np.isfinite(p).all() and np.isfinite(m).all();np.testing.assert_allclose(p.sum(-1),1,atol=1e-5)
                pred=p[mask,h].argmax(-1);acc=daily(actual==pred);correct[name]=acc
                recalls=[float((pred[actual==c]==c).mean()) for c in range(3)]
                row=base|dict(model=name,session_accuracy=float(acc.mean()),pair_accuracy=float((actual==pred).mean()),
                    balanced_accuracy=float(np.mean(recalls)),session_magnitude_mae=float(daily(abs(m[mask,h]-mag[mask,h])).mean()))
                for c in range(3):
                    row[f'recall_{c}']=recalls[c];row[f'pred_share_{c}']=float((pred==c).mean());row[f'actual_share_{c}']=float((actual==c).mean())
                rows.append(row)
            for baseline in BASELINES:
                diff=correct['target']-correct[baseline];delta=float(diff.mean())
                for lag in (5,20):
                    se=float(hac_mean_se(diff,lag));p=float(2*norm.sf(abs(delta/se))) if se else float(delta==0)
                    comparisons.append(base|dict(baseline=baseline,lag=lag,delta=delta,se=se,p=p,ci95_unadjusted_low=delta-1.96*se,ci95_unadjusted_high=delta+1.96*se))
    report=pd.DataFrame(rows);comp=pd.DataFrame(comparisons)
    for lag in (5,20):
        mask=comp.lag.eq(lag);assert mask.sum()==32;comp.loc[mask,'p_holm_32']=holm(comp.loc[mask,'p'].to_numpy())
    report.to_csv(out/'validation_metrics.csv',index=False);comp.to_csv(out/'validation_comparisons.csv',index=False)
    macro=report.groupby('model')[['session_accuracy','balanced_accuracy','session_magnitude_mae']].mean();macro.to_csv(out/'validation_macro.csv')
    score=float(macro.loc['target','session_accuracy']);gains={b:score-float(macro.loc[b,'session_accuracy']) for b in BASELINES}
    return dict(validation_only=True,scope='eligible direct pairs only',target_accuracy=score,user_target=.65,user_target_met=score>=.65,
        direct_macro_gains=gains,descriptive_improvement=all(g>0 for g in gains.values()),automatic_model_replacement=False)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,required=True);args=ap.parse_args();out=args.out.resolve()
    source=HERE/'artifacts/news_impact_v2';evidence=HERE/'artifacts/news_impact_v2_evidence';balanced=HERE/'artifacts/news_balanced_experiment_20260915'
    for p in (source,evidence,balanced):
        if out==p or p in out.parents or out in p.parents:raise ValueError('Use a new sibling research folder')
    out.mkdir(parents=True,exist_ok=False);start=time.monotonic();torch.set_num_threads(4)
    assert torch.cuda.is_available()
    files=[p for p in source.iterdir() if p.is_file()]
    files += [balanced/m/'validation_predictions.npz' for m in ('balanced_original','balanced_no_content')]
    files += [evidence/'predictions_ticker_prior.npz']
    hashes={str(p):sha(p) for p in files}
    code_names=['experiment_target_news.py','target_news_features.py','target_news_model.py','train_target_news.py','train_news_impact.py',
        'news_impact_model.py','news_company_context.py','news_content.py','target_audit_reviews.txt']
    dump(out/'protocol.json',dict(source_hashes=hashes,code_hashes={n:sha(HERE/n) for n in code_names},
        user_target=.65,modes=MODES,seeds=[0,1,2],epochs=30,patience=6,batch_size=24,lr=.0008,weight_decay=.001,
        hidden=64,dropout=.15,pca=64,loss='direct-only CE + Huber, mean horizons, equal eligible article weighting',
        input_policy='verified target prefix plus body-centered literal context; pooled arm averages same target features',
        exclusions='known feed quarantine; missing or rejected literal mentions; no outcome-based filtering',
        evaluation='val only; same retained original pair IDs; 4 equal market/horizon cells of equal-session accuracy',
        baselines=BASELINES,inference='32 HAC contrasts per lag (5/20), Holm per lag; unadjusted CIs; exploratory reused validation',
        acceptance='>=.65 target macro; beat matched zero-content and pooled controls and legacy baselines; no auto promotion',
        no_test_features=True,no_test_predictions=True,graph_notice='Original past graph snapshots preserved, not re-audited/rebuilt from cleaned corpus'))
    snapshot=out/'training_inputs';snapshot.mkdir()
    for n in COPIES:(snapshot/n).write_bytes((source/n).read_bytes())
    (snapshot/'aliases.csv').write_bytes((source/'aliases.csv').read_bytes())
    spec=json.loads((snapshot/'pretrained.json').read_text(encoding='utf8'))
    for role in spec:spec[role]['path']=str((source/spec[role]['path']).resolve())
    dump(snapshot/'pretrained.json',spec);dump(snapshot/'feature_manifest.json',feature_manifest(snapshot))
    data=TrainingData(snapshot)
    with np.load(source/'label_config.npz') as cfg:
        np.testing.assert_array_equal(data.thresholds,cfg['thresholds']);np.testing.assert_array_equal(data.scales,cfg['scales'])
    np.savez(snapshot/'label_config.npz',thresholds=data.thresholds,scales=data.scales)
    matcher=CompanyMatcher.from_csv(source/'aliases.csv');names=data.meta.set_index('ticker').name_official.to_dict()
    mapping=np.zeros((len(data.docs),len(data.tickers)),dtype='int64');entries=[];quality=[];clean_docs=[]
    node={t:i for i,t in enumerate(data.tickers)}
    for i,original in enumerate(data.docs):
        if original['split'] not in ('train','val'):clean_docs.append(original);continue
        d,flags,quarantine=clean_target_article(original);clean_docs.append(d);full=d['title']+'\n'+d['body']
        original_direct={m['ticker'] for m in d['companies']}
        hits=[] if quarantine else [dict(ticker=m.ticker,start=m.start,end=m.end) for m in matcher.match(d['title'],d['body']).mentions
            if m.confidence>=1 and m.ticker in original_direct and valid_target_hit(full,m.ticker,m.start,m.end)]
        verified={h['ticker'] for h in hits}
        for ticker in sorted(original_direct):
            valid=ticker in verified
            quality.append(dict(article=i,ticker=ticker,split=d['split'],eligible=valid,flags=';'.join(flags),reason='verified' if valid else ('feed_quarantine' if quarantine else 'missing_or_rejected_literal_mention')))
            if valid:
                mapping[i,node[ticker]]=len(entries)+1;entries.append(dict(article=i,ticker=ticker,hits=hits))
    q=pd.DataFrame(quality);q.to_csv(out/'input_quality.csv',index=False)
    data.mentions=data.mentions.copy();data.mentions[mapping==0]=0
    data.mask=data.mask & (mapping>0)
    pair_eligible=data.mask[data.article_idx,data.node_idx] & data.pairs.is_direct.to_numpy()
    indices=np.flatnonzero(data.val & pair_eligible)
    exclusions=[]
    for split in ('train','val'):
        for market in ('KOSPI','NASDAQ'):
            mask=data.pairs.split.eq(split).to_numpy() & data.pairs.market.eq(market).to_numpy() & data.pairs.is_direct.to_numpy()
            exclusions.append(dict(split=split,market=market,before=int(mask.sum()),retained=int((mask & pair_eligible).sum()),excluded=int((mask & ~pair_eligible).sum())))
    pd.DataFrame(exclusions).to_csv(out/'eligibility.csv',index=False)
    for i,d in enumerate(clean_docs):
        if not data.mask[i].any() and d['split'] in ('train','val'):clean_docs[i]=dict(d,split='excluded')
    data.docs=clean_docs
    dump(out/'feature_entries.json',entries);np.save(out/'feature_mapping.npy',mapping);np.save(out/'direct_mask.npy',data.mask);np.save(out/'mentions.npy',data.mentions)
    dump(out/'eligible_articles.json',[i for i,d in enumerate(clean_docs) if d['split'] in ('train','val')])
    print('ELIGIBILITY '+json.dumps(exclusions),flush=True)
    enc=TargetEncoder(snapshot,device='cuda');embedding,audits=enc.encode_entries('embedding',clean_docs,entries,names);enc.release()
    sentiment=np.zeros((len(entries)+1,3),dtype='float32')
    for role in ('sentiment_ko','sentiment_en'):
        values,audit=enc.encode_entries(role,clean_docs,entries,names);sentiment+=values;audits.extend(audit);enc.release()
    dump(out/'feature_audit.json',audits)
    np.save(out/'target_embedding.npy',embedding);np.save(out/'target_sentiment.npy',sentiment)
    train_ids=[j+1 for j,e in enumerate(entries) if data.docs[e['article']]['split']=='train']
    pca=PCA(n_components=64,random_state=42);pca.fit(embedding[train_ids]);text=pca.transform(embedding).astype('float32');text[0]=0
    np.savez(out/'target_projection.npz',mean=pca.mean_,components=pca.components_)
    pooled_text=np.zeros_like(text);pooled_sent=np.zeros_like(sentiment)
    for i in np.flatnonzero((mapping>0).any(axis=1)):
        fi=mapping[i];fi=fi[fi>0];pooled_text[fi]=text[fi].mean(0);pooled_sent[fi]=sentiment[fi].mean(0)
    for n,v in [('target_text',text),('pooled_text',pooled_text),('pooled_sentiment',pooled_sent)]:np.save(out/(n+'.npy'),v)
    dump(out/'feature_manifest.json',dict(hashes={p.name:sha(p) for p in out.iterdir() if p.suffix in ('.npy','.npz')},
        audit_sha=sha(out/'feature_audit.json'),entries_sha=sha(out/'feature_entries.json'),pca_train_entries=len(train_ids),
        clean_source='target_news_features.clean_target_article; production raw source preserved'))
    output={};selections={}
    for mode in MODES:
        folder=out/mode;folder.mkdir();dump(folder/'MODEL_POLICY.json',dict(research_only=True,mode=mode,direct_only=True,validation_only=True))
        x,s=(text,sentiment) if mode=='target' else ((pooled_text,pooled_sent) if mode=='pooled' else (np.zeros_like(text),np.zeros_like(sentiment)))
        output[mode],selections[mode]=fit_target(data,mapping,x,s,folder,indices);dump(out/'selections.json',selections)
    for name,path in [('original',source/'predictions_stage3.npz'),('majority',source/'predictions_majority.npz'),('stage2',source/'predictions_stage2.npz'),('ticker_prior',evidence/'predictions_ticker_prior.npz')]:
        with np.load(path) as a:output[name]=(a['probabilities'][indices],a['magnitude'][indices])
    for name in ('balanced_original','balanced_no_content'):
        with np.load(balanced/name/'validation_predictions.npz') as a:
            saved_ids=a['pair_indices'];positions=np.searchsorted(saved_ids,indices);np.testing.assert_array_equal(saved_ids[positions],indices)
            output[name]=(a['probabilities'][positions],a['magnitude'][positions])
    verdict=evaluate(data,indices,output,out)
    for path,digest in hashes.items():assert sha(Path(path))==digest,path
    dump(out/'completion.json',verdict|dict(seconds=time.monotonic()-start,source_unchanged=True,validation_pairs=len(indices),feature_entries=len(entries)))
    print(json.dumps(verdict,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
