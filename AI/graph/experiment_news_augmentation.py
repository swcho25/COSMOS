"""Retain broad old training and add frozen event candidates, three GAT arms.

The previous event-only experiment retained only 409 old articles and obtained
38.18%, below the original 39.61%. Test whether hard filtering discarded useful
training coverage. Restore all prior hygiene-eligible train inputs, subject only
to observed-before-news price history, then optionally add the same 3493 articles.
Freeze the previously rebuilt graphs, PCA, labels, event selection and val rows.
All arms share those graphs; this isolates extra supervised examples conditional
on that graph. No test predictions, relabeling, validation filtering or promotion.
"""
import argparse
import collections
import json
import shutil
import time
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from scipy.stats import norm
from experiment_current_events import load_data as load_event_data,rows,save_rows
from target_news_features import clean_target_article
from news_impact_data import prices,close_times
from train_target_news import fit_target
from audit_news_impact import hac_mean_se
from verify_news_predictive_value import sha,dump,holm

HERE=Path(__file__).resolve().parent
SOURCE=HERE/'artifacts/news_impact_v2'
PARENT=HERE/'artifacts/news_target_experiment_20260915'
EVENT=HERE/'artifacts/news_current_events_20260915_v2'
MODES=('old_full','augmented','no_content')
BASELINES=('old_full','no_content','original','ticker_prior','majority')
BASELINE_PATHS={'original':SOURCE/'predictions_stage3.npz','ticker_prior':HERE/'artifacts/news_impact_v2_evidence/predictions_ticker_prior.npz','majority':SOURCE/'predictions_majority.npz'}


def prepare(out):
    out.mkdir(parents=True,exist_ok=False);start=time.monotonic()
    # Previous completed preparation/verification stays an immutable dependency.
    ep=json.loads((EVENT/'prepared.json').read_text());verify=json.loads((EVENT/'reload_verification.json').read_text())
    assert verify['source_unchanged'] and verify['added_articles']==3493
    for name,digest in ep['artifact_hashes'].items():assert sha(EVENT/name)==digest,name
    copies=('pairs.parquet','graphs.json','companies.json','label_config.npz','target_text.npy','target_sentiment.npy','pair_indices.npy')
    files=[EVENT/n for n in copies+('prepared.json','protocol.json','reload_verification.json','articles.jsonl','feature_mapping.npy','direct_mask.npy','mentions.npy')]
    files += [PARENT/n for n in ('feature_mapping.npy','direct_mask.npy','mentions.npy','target_projection.npz','feature_manifest.json')]
    files += [SOURCE/'articles.jsonl',SOURCE/'pairs.parquet',HERE/'data/prices_daily.parquet']+list(BASELINE_PATHS.values())
    code=('experiment_news_augmentation.py','experiment_current_events.py','train_target_news.py','target_news_model.py','news_impact_model.py','train_news_impact.py','target_news_features.py','news_content.py','news_impact_data.py')
    protocol=dict(source=str(SOURCE),parent=str(PARENT),event_source=str(EVENT),source_hashes={str(p):sha(p) for p in files},code_hashes={n:sha(HERE/n) for n in code},
        modes=MODES,seeds=[0,1,2],contrasts=[['augmented',b] for b in BASELINES],correction_family=20,hac_lags=[5,20],
        hypothesis='Event-only filtering removed nearly all old train articles; preserve broad hygiene coverage and add frozen event examples.',
        old_training_policy='restore original target-context hygiene inputs; exclude all mapped train nodes lacking a positive finite price close at/before article available_at, including nodes without labels',
        graph_policy='byte-identical eventv2 annual clean graphs shared by all arms; no rebuild from restored broad articles',
        label_policy='unchanged original pairs prefix, original available_at, frozen thresholds/scales/PCA; same eventv2 added labels',
        goal='>=0.65 over original 4813 direct val pairs, 70 hygiene abstentions wrong; equal sessions/four cells',
        no_event_filter_on_validation=True,validation_only=True,reused_validation=True,automatic_model_replacement=False,
        settings=dict(epochs=30,patience=6,batch_size=24,lr=.0008,weight_decay=.001,hidden=64,dropout=.15),
        limitations='Event labels are weak semantic selections; timestamps and historical legal identity uncertified; fresh future evaluation still required')
    dump(out/'protocol.json',protocol)
    for name in copies:shutil.copyfile(EVENT/name,out/name)
    docs=list(rows(EVENT/'articles.jsonl'));original=list(rows(SOURCE/'articles.jsonl'));n=len(original)
    mapping=np.load(EVENT/'feature_mapping.npy');mask=np.load(EVENT/'direct_mask.npy');mentions=np.load(EVENT/'mentions.npy')
    old_mapping=np.load(PARENT/'feature_mapping.npy');old_mask=np.load(PARENT/'direct_mask.npy');old_mentions=np.load(PARENT/'mentions.npy')
    meta=pd.read_json(SOURCE/'companies.json',dtype={'ticker':str});tickers=meta.ticker.tolist();node={t:i for i,t in enumerate(tickers)}
    price=prices();first={}
    for market,group in meta.groupby('market'):
        panel=price.reindex(columns=group.ticker).dropna(how='all');closes=close_times(panel.index,market)
        for t in panel:
            v=panel[t].to_numpy();ids=np.flatnonzero(np.isfinite(v)&(v>0))
            if len(ids):first[t]=closes[ids[0]]
    audit=[];counts=collections.Counter()
    for i,old in enumerate(original):
        if old['split']!='train':continue
        d=clean_target_article(old)[0];mapping[i]=old_mapping[i];mask[i]=old_mask[i];mentions[i]=old_mentions[i]
        for j in np.flatnonzero(mapping[i]>0):
            t=tickers[j];before=t in first and first[t]<=pd.Timestamp(old['available_at'])
            had_label=bool(mask[i,j])
            audit.append(dict(article=i,record_id=old['record_id'],ticker=t,market=meta.iloc[j]['market'],available_at=old['available_at'],first_observed_close=first[t].isoformat() if t in first else None,eligible=bool(before),had_label=had_label))
            if not before:
                mask[i,j]=False;mapping[i,j]=0;mentions[i,j]=0
                counts['history_guard_excluded_inputs']+=1;counts['history_guard_excluded_pairs']+=int(had_label)
        # Valid text inputs may exist without an eligible price target. Preserve
        # their graph messages exactly as the original hygiene experiment did.
        d['companies']=[m for m in d['companies'] if mapping[i,node[m['ticker']]]>0]
        if not mask[i].any():d['split']='excluded'
        else:counts['old_train_articles']+=1;counts['old_train_pairs']+=int(mask[i].sum());counts['old_'+d['region']]+=1
        docs[i]=d
    save_rows(out/'articles.jsonl',docs);pd.DataFrame(audit).to_parquet(out/'old_training_eligibility.parquet',index=False)
    for name,x in [('feature_mapping',mapping),('direct_mask',mask),('mentions',mentions)]:np.save(out/(name+'.npy'),x)
    for path,digest in protocol['source_hashes'].items():assert sha(Path(path))==digest,path
    counts=dict(counts);counts.setdefault('history_guard_excluded_pairs',0);counts.setdefault('history_guard_excluded_inputs',0)
    result=dict(legacy_articles=n,legacy_pairs=ep['legacy_pairs'],added_articles=ep['added_articles'],added_pairs=ep['added_pairs'],**counts,
        total_train_articles=counts['old_train_articles']+ep['added_articles'],total_train_pairs=counts['old_train_pairs']+ep['added_pairs'],
        validation_pairs=4743,original_direct_pairs=4813,source_unchanged=True,seconds=time.monotonic()-start,
        artifact_hashes={p.name:sha(p) for p in out.iterdir() if p.is_file()})
    dump(out/'prepared.json',result);print(json.dumps({k:v for k,v in result.items() if k!='artifact_hashes'}),flush=True)


def load_data(out,mode):
    d=load_event_data(out,'expanded')
    if mode=='old_full':
        n=json.loads((out/'prepared.json').read_text())['legacy_articles']
        d.docs=[dict(r,split='excluded') if i>=n else r for i,r in enumerate(d.docs)]
    return d


def evaluate(data,indices,outputs,out):
    val=data.pairs.iloc[indices];cls=data.cls[indices];metrics=[];comparisons=[];adjusted=[]
    full_ids=np.flatnonzero(data.val & data.pairs.is_direct.to_numpy());full=data.pairs.iloc[full_ids];pos=np.searchsorted(full_ids,indices)
    baseline_full={}
    for name,p in BASELINE_PATHS.items():
        with np.load(p) as a:baseline_full[name]=a['probabilities'][full_ids]
    for market in ('KOSPI','NASDAQ'):
        m=val.market.eq(market).to_numpy();fm=full.market.eq(market).to_numpy()
        for h in range(2):
            correct={}
            for name,(p,mag) in outputs.items():
                assert np.isfinite(p).all() and np.isfinite(mag).all();np.testing.assert_allclose(p.sum(-1),1,atol=1e-5)
                pred=p[m,h].argmax(-1);actual=cls[m,h];c=(pred==actual).astype(float)
                daily=pd.Series(c,index=val.loc[m,'anchor_date']).groupby(level=0).mean().sort_index();correct[name]=daily
                metrics.append(dict(model=name,market=market,horizon=(1,3)[h],n_pairs=int(m.sum()),n_sessions=len(daily),session_accuracy=float(daily.mean()),pair_accuracy=float(c.mean()),balanced_accuracy=float(np.mean([(pred[actual==k]==k).mean() for k in range(3)]))))
                if name in MODES:
                    allc=np.zeros(len(full));allc[pos]=p[:,h].argmax(-1)==cls[:,h]
                else:allc=(baseline_full[name][:,h].argmax(-1)==data.cls[full_ids,h]).astype(float)
                score=pd.Series(allc[fm],index=full.loc[fm,'anchor_date']).groupby(level=0).mean().mean()
                adjusted.append(dict(model=name,market=market,horizon=(1,3)[h],original_population_accuracy=float(score)))
            for baseline in BASELINES:
                diff=(correct['augmented']-correct[baseline]).to_numpy();delta=float(diff.mean())
                for lag in (5,20):
                    se=hac_mean_se(diff,lag);p=float(2*norm.sf(abs(delta/se))) if se else float(delta==0)
                    comparisons.append(dict(model='augmented',baseline=baseline,market=market,horizon=(1,3)[h],lag=lag,delta=delta,se=se,p=p))
    report=pd.DataFrame(metrics);comp=pd.DataFrame(comparisons);adj=pd.DataFrame(adjusted)
    for lag in (5,20):
        m=comp.lag.eq(lag);assert m.sum()==20;comp.loc[m,'p_holm_20']=holm(comp.loc[m,'p'].to_numpy())
    report.to_csv(out/'validation_metrics.csv',index=False);comp.to_csv(out/'validation_comparisons.csv',index=False);adj.to_csv(out/'coverage_adjusted_metrics.csv',index=False)
    scores=adj.groupby('model').original_population_accuracy.mean().to_dict();conditional=report.groupby('model').session_accuracy.mean().to_dict()
    return dict(validation_only=True,original_direct_pairs=len(full),retained_pairs=len(val),original_population_accuracy=scores,conditional_accuracy=conditional,user_target=.65,user_target_met=scores['augmented']>=.65,automatic_model_replacement=False)


def train(out):
    start=time.monotonic();torch.set_num_threads(4);protocol=json.loads((out/'protocol.json').read_text());prepared=json.loads((out/'prepared.json').read_text())
    assert (out/'data_verification.json').exists(),'Run independent --data-only verification first'
    for path,digest in protocol['source_hashes'].items():assert sha(Path(path))==digest,path
    for name,digest in protocol['code_hashes'].items():assert sha(HERE/name)==digest,name
    for name,digest in prepared['artifact_hashes'].items():assert sha(out/name)==digest,name
    mapping=np.load(out/'feature_mapping.npy');text=np.load(out/'target_text.npy');sent=np.load(out/'target_sentiment.npy');indices=np.load(out/'pair_indices.npy');outputs={};selections={}
    for mode in MODES:
        folder=out/mode;folder.mkdir();dump(folder/'MODEL_POLICY.json',dict(research_only=True,validation_only=True,mode=mode,full_old_hygiene_training=True))
        data=load_data(out,mode);x,s=(np.zeros_like(text),np.zeros_like(sent)) if mode=='no_content' else (text,sent)
        outputs[mode],selections[mode]=fit_target(data,mapping,x,s,folder,indices);dump(out/'selections.json',selections);del data;torch.cuda.empty_cache()
    data=load_data(out,'augmented')
    for name,path in BASELINE_PATHS.items():
        with np.load(path) as a:outputs[name]=(a['probabilities'][indices],a['magnitude'][indices])
    result=evaluate(data,indices,outputs,out)
    for path,digest in protocol['source_hashes'].items():assert sha(Path(path))==digest,path
    dump(out/'completion.json',result|dict(training_seconds=time.monotonic()-start,source_unchanged=True));print(json.dumps(result),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,required=True);ap.add_argument('--stage',choices=('prepare','train'),required=True);a=ap.parse_args()
    (prepare if a.stage=='prepare' else train)(a.out.resolve())
