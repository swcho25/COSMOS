"""Train-only current-event expansion with frozen original validation labels.

Rules propose event subjects; they are not human annotations or causal labels.
Keep all legacy pair rows and original thresholds/PCA, and evaluate the same
4743 hygiene-eligible direct val rows; 70 abstentions count as wrong on all 4813.
Rebuild annual graphs from accepted training mentions and prior-year same-market
prices only. Unverified legacy relation sentences, ownership and sector are omitted.
Compare clean old training, augmented training and augmented zero-text training,
three seeds each, same clean graphs and validation. Never fit on test or promote.
"""
import argparse
import collections
import itertools
import json
import time
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pandas as pd
import torch
from scipy.stats import norm
from current_event_gate import evaluate_article
from news_quality import financial_evidence
from target_news_features import clean_target_article,TargetEncoder
from news_impact_data import Labels,prices,availability,graph_arrays,node_features,mention_array
from train_target_news import fit_target
from verify_news_predictive_value import sha,dump,holm
from audit_news_impact import hac_mean_se

HERE=Path(__file__).resolve().parent
SOURCE=HERE/'artifacts/news_impact_v2'
PARENT=HERE/'artifacts/news_target_experiment_20260915'
QUALITY=HERE/'artifacts/news_quality_dataset_20260915_v2'
MODES=('clean_old','expanded','no_content')


def rows(path):
    with path.open(encoding='utf8') as f:
        for line in f:yield json.loads(line)


def save_rows(path,values):
    with path.open('x',encoding='utf8') as f:
        for r in values:f.write(json.dumps(r,ensure_ascii=False)+'\n')


def clean_graph(cutoff,docs,meta,price):
    """Only prior available training news and prior-year price correlations."""
    snapshot=pd.Timestamp(cutoff+'T12:00:00+00:00');pairs=collections.defaultdict(list);edges=[]
    for r in docs:
        if r['split']!='train' or pd.Timestamp(r['available_at'])>=snapshot:continue
        for a,b in itertools.combinations(sorted({c['ticker'] for c in r['companies']}),2):
            pairs[a,b].append(dict(id=r['record_id'],date=r['published_date'],available_at=r['available_at'],text=r['title']))
    neighbors=collections.defaultdict(list)
    for (a,b),ev in pairs.items():
        if len(ev)>=3:
            w=len(ev)/(len(ev)+5);neighbors[a].append((w,b,ev));neighbors[b].append((w,a,ev))
    for a,ns in neighbors.items():
        for w,b,ev in sorted(ns,key=lambda x:(-x[0],x[1]))[:4]:
            edges.append(dict(src=a,dst=b,type='co_mention',weight=w,sign=1.,reverse=False,evidence=ev[:3]))
    since=str(int(cutoff[:4])-1)+'-01-01'
    historical=np.log(price.loc[(price.index>=since)&(price.index<cutoff)]).diff()
    for _,group in meta.groupby('market'):
        corr=historical.reindex(columns=group.ticker).corr(min_periods=60)
        for a in corr:
            s=corr[a].drop(index=a).dropna()
            for b in s.abs().sort_values(ascending=False).head(4).index:
                if abs(s[b])>=.05:
                    edges.append(dict(src=a,dst=b,type='correlation',weight=float(abs(s[b])),sign=float(np.sign(s[b])),reverse=False,
                        evidence=[dict(id=f'prices:{since}:{cutoff}:{a}:{b}',date=since,text='Prior-year same-market adjusted returns; at least 60 overlapping observations.')]))
    return dict(as_of=cutoff,available_at=snapshot.isoformat(),industry_available=False,edges=edges)


def prepare(out):
    out.mkdir(parents=True,exist_ok=False);start=time.monotonic();torch.set_num_threads(4)
    assert torch.cuda.is_available()
    files=[SOURCE/n for n in ('articles.jsonl','pairs.parquet','companies.json','aliases.csv','label_config.npz','index_KOSPI.csv','index_NASDAQ.csv')]
    files += [PARENT/n for n in ('feature_entries.json','feature_mapping.npy','direct_mask.npy','mentions.npy','target_text.npy','target_sentiment.npy','target_projection.npz','feature_manifest.json','protocol.json')]
    files += [PARENT/'training_inputs/pretrained.json',QUALITY/'training_candidates_clean.jsonl',QUALITY/'verification.json',HERE/'data/prices_daily.parquet']
    code=('experiment_current_events.py','current_event_gate.py','news_quality.py','news_impact_data.py','target_news_features.py','news_content.py','train_target_news.py','target_news_model.py','news_impact_model.py','train_news_impact.py')
    protocol=dict(source=str(SOURCE),parent=str(PARENT),quality=str(QUALITY),source_hashes={str(p):sha(p) for p in files},code_hashes={n:sha(HERE/n) for n in code},
        modes=MODES,seeds=[0,1,2],label_contract='original delayed 1/3-session adjusted log excess return; original train thresholds/scales frozen',
        graph_contract='annual clean training co-mention + prior-year same-market price correlation only; common graphs for all arms',
        model_input='cached target-specific text, frozen train PCA64, financial sentiment, market/mention/company ID; no source generated labels',
        time_contract='original conservative date-only available_at; no unverified source timestamp upgrade',
        history_guard='at least one valid target adjusted closing price at/before article available_at; observation boundary, not legal-identity verification',
        selection='input-only subject/action/recency rules; not human gold; no current-event filtering of val',
        objective='>=0.65 accuracy over all 4813 original direct val pairs, 70 hygiene abstentions wrong, equal sessions/four market-horizon cells',
        settings=dict(lr=.0008,weight_decay=.001,batch_size=24,epochs=30,patience=6,hidden=64,dropout=.15),
        evaluation='reused validation exploratory only; same 4743 retained pairs, no new test predictions',automatic_model_replacement=False)
    dump(out/'protocol.json',protocol)
    docs=list(rows(SOURCE/'articles.jsonl'));legacy_count=len(docs);pairs=pd.read_parquet(SOURCE/'pairs.parquet')
    meta=pd.read_json(SOURCE/'companies.json',dtype={'ticker':str});tickers=meta.ticker.tolist();node={t:i for i,t in enumerate(tickers)}
    p=prices();bench={}
    for market in ('KOSPI','NASDAQ'):
        f=pd.read_csv(SOURCE/f'index_{market}.csv');bench[market]=pd.Series(f.Close.to_numpy(),index=pd.to_datetime(f.Date.str[:10]))
    labels=Labels(p,meta,bench);first_close={}
    for market,(panel,_,closes) in labels.panels.items():
        for ticker in panel:
            values=panel[ticker].to_numpy();finite=np.flatnonzero(np.isfinite(values)&(values>0))
            if len(finite):first_close[ticker]=closes[finite[0]]
    mapping=np.load(PARENT/'feature_mapping.npy');mask=np.load(PARENT/'direct_mask.npy');mentions=np.load(PARENT/'mentions.npy')
    entries=json.loads((PARENT/'feature_entries.json').read_text());by_article=collections.defaultdict(list)
    for e in entries:by_article[e['article']]=e['hits']
    gate_rows=[];graph_docs=[];training_counts=collections.Counter()
    for i,d in enumerate(docs):
        if d['split']=='test':docs[i]=dict(d,split='excluded');continue
        clean=clean_target_article(d)[0];docs[i]=clean
        if d['split']=='train':
            hits=by_article.get(i,[])
            candidate=dict(clean,literal_mentions=hits,event_evidence=financial_evidence(clean['title'],clean['body'],hits))
            decision=evaluate_article(candidate);accepted=set(decision['accepted_tickers'])
            accepted={t for t in accepted if t in first_close and first_close[t]<=pd.Timestamp(d['available_at'])}
            keep=np.array([t in accepted for t in tickers]);mask[i]&=keep;mapping[i,~keep]=0;mentions[i,~keep]=0
            ms=[m for m in d['companies'] if m['ticker'] in accepted and mask[i,node[m['ticker']]]]
            clean['companies']=ms
            if ms:graph_docs.append(clean);training_counts['clean_old_articles']+=1;training_counts['clean_old_pairs']+=int(mask[i].sum())
            else:clean['split']='excluded'
            gate_rows.append(dict(record_id=d['record_id'],article=i,origin='legacy_train',accepted_with_history=sorted(accepted),**decision))
        elif not mask[i].any():clean['split']='excluded'
    additions=[];new_entries=[];new_pairs=[];new_mapping=[];new_masks=[];new_mentions=[]
    offset=len(np.load(PARENT/'target_text.npy'));new_gate_counts=collections.Counter()
    for k,r in enumerate(rows(QUALITY/'training_candidates_clean.jsonl')):
        if r['already_in_legacy_train']:continue
        decision=evaluate_article(r);accepted=set(decision['accepted_tickers']);new_gate_counts['examined']+=1
        accepted={t for t in accepted if t in first_close and first_close[t]<=pd.Timestamp(r['available_at'])}
        label_rows={t:labels.label(t,pd.Timestamp(r['available_at']),'train') for t in accepted};accepted={t for t in accepted if label_rows[t] is not None}
        gate_rows.append(dict(record_id=r['record_id'],origin='new_train',accepted_with_history_and_label=sorted(accepted),**decision))
        if not accepted:new_gate_counts['abstained_or_no_label']+=1;continue
        # Keep only clean public text/provenance needed for replay, never arbitrary source payload labels.
        d={key:r[key] for key in ('record_id','title','body','region','published_date','available_at','publication_time_audit')}
        d.update(split='train',graph=r['published_date'][:4],companies=[m for m in r['companies'] if m['ticker'] in accepted],current_event_decision=decision)
        i=legacy_count+len(additions);mp=np.zeros(len(tickers),dtype='int64');mk=np.zeros(len(tickers),dtype=bool)
        hits=[h for h in r['literal_mentions'] if h['ticker'] in accepted]
        for ticker in sorted(accepted):
            mp[node[ticker]]=offset+len(new_entries);mk[node[ticker]]=True;new_entries.append(dict(article=i,ticker=ticker,hits=hits))
            new_pairs.append(dict(article=i,news_id=r['record_id'],ticker=ticker,split='train',graph=d['graph'],is_direct=True,path_length=0,rule_weight=1.,rule_sign=1.,rule_source=ticker,**label_rows[ticker]))
        additions.append(d);new_mapping.append(mp);new_masks.append(mk);new_mentions.append(mention_array(d['companies'],tickers));graph_docs.append(d)
        if len(additions)%1000==0:print('new current-event articles',len(additions),flush=True)
    assert additions and new_entries,'No usable current-event expansion'
    docs.extend(additions);pairs=pd.concat([pairs,pd.DataFrame(new_pairs)],ignore_index=True)
    mapping=np.concatenate([mapping,np.stack(new_mapping)]);mask=np.concatenate([mask,np.stack(new_masks)]);mentions=np.concatenate([mentions,np.stack(new_mentions)])
    # Legacy test nodes may retain cached zero/nonzero arrays, but are never batched.
    graphs={str(y):clean_graph(f'{y}-01-01',graph_docs,meta,p) for y in range(2017,2024)}
    save_rows(out/'articles.jsonl',docs);save_rows(out/'event_decisions.jsonl',gate_rows);pairs.to_parquet(out/'pairs.parquet',index=False)
    dump(out/'graphs.json',graphs);dump(out/'new_feature_entries.json',new_entries)
    for name,value in [('feature_mapping',mapping),('direct_mask',mask),('mentions',mentions)]:np.save(out/(name+'.npy'),value)
    for name in ('companies.json','label_config.npz','aliases.csv'):(out/name).write_bytes((SOURCE/name).read_bytes())
    names=meta.set_index('ticker').name_official.to_dict();encoder=TargetEncoder(PARENT/'training_inputs',device='cuda')
    embedding,audits=encoder.encode_entries('embedding',docs,new_entries,names);encoder.release()
    sentiment=np.zeros((len(new_entries)+1,3),dtype='float32')
    for role in ('sentiment_ko','sentiment_en'):
        values,audit=encoder.encode_entries(role,docs,new_entries,names);sentiment+=values;audits.extend(audit);encoder.release()
    dump(out/'new_feature_audit.json',audits);np.save(out/'new_target_embedding.npy',embedding);np.save(out/'new_target_sentiment.npy',sentiment)
    with np.load(PARENT/'target_projection.npz') as projection:text=(embedding[1:]@projection['components'].T-projection['mean']@projection['components'].T).astype('float32')
    np.save(out/'target_text.npy',np.concatenate([np.load(PARENT/'target_text.npy'),text]));np.save(out/'target_sentiment.npy',np.concatenate([np.load(PARENT/'target_sentiment.npy'),sentiment[1:]]))
    indices=np.flatnonzero(pairs.split.eq('val').to_numpy() & pairs.is_direct.to_numpy() & mask[pairs.article.to_numpy(),pairs.ticker.map(node).to_numpy()])
    assert len(indices)==4743;np.save(out/'pair_indices.npy',indices)
    for path,digest in protocol['source_hashes'].items():assert sha(Path(path))==digest,path
    done=dict(legacy_articles=legacy_count,legacy_pairs=len(pairs)-len(new_pairs),added_articles=len(additions),added_pairs=len(new_pairs),**training_counts,new_gate_counts=dict(new_gate_counts),validation_pairs=len(indices),
        graph_edges={k:len(g['edges']) for k,g in graphs.items()},seconds=time.monotonic()-start,source_unchanged=True,trained=False,
        artifact_hashes={f.name:sha(f) for f in out.iterdir() if f.is_file()})
    dump(out/'prepared.json',done);print(json.dumps({k:v for k,v in done.items() if k!='artifact_hashes'}),flush=True)


def load_data(out,mode):
    d=SimpleNamespace();d.docs=list(rows(out/'articles.jsonl'));done=json.loads((out/'prepared.json').read_text())
    if mode=='clean_old':d.docs=[dict(r,split='excluded') if i>=done['legacy_articles'] else r for i,r in enumerate(d.docs)]
    d.pairs=pd.read_parquet(out/'pairs.parquet');d.meta=pd.read_json(out/'companies.json',dtype={'ticker':str});d.tickers=d.meta.ticker.tolist()
    d.article_idx=d.pairs.article.to_numpy();d.node_idx=d.pairs.ticker.map({t:i for i,t in enumerate(d.tickers)}).to_numpy()
    d.graphs=json.loads((out/'graphs.json').read_text());d.static={k:node_features(d.meta,g) for k,g in d.graphs.items()};d.graph_arrays={k:graph_arrays(g,d.tickers) for k,g in d.graphs.items()}
    d.mask=np.load(out/'direct_mask.npy');d.mentions=np.load(out/'mentions.npy');d.val=d.pairs.split.eq('val').to_numpy();d.train=d.pairs.split.eq('train').to_numpy()
    d.y=d.pairs[['y_1d','y_3d']].to_numpy(dtype='float32')
    with np.load(out/'label_config.npz') as cfg:d.thresholds=cfg['thresholds'];d.scales=cfg['scales']
    d.cls=np.where(d.y>d.thresholds,2,np.where(d.y< -d.thresholds,0,1)).astype('int64');d.mag=np.minimum(abs(d.y)/d.scales,1).astype('float32')
    d.dense_cls=np.zeros((*d.mask.shape,2),dtype='int64');d.dense_mag=np.zeros((*d.mask.shape,2),dtype='float32')
    d.dense_cls[d.article_idx,d.node_idx]=d.cls;d.dense_mag[d.article_idx,d.node_idx]=d.mag
    return d


def evaluate(data,indices,outputs,out):
    metrics=[];contrasts=[];val=data.pairs.iloc[indices];full_ids=np.flatnonzero(data.val & data.pairs.is_direct.to_numpy());full=data.pairs.iloc[full_ids];positions=np.searchsorted(full_ids,indices)
    full_actual=data.cls[full_ids];adjusted=[]
    for market in ('KOSPI','NASDAQ'):
        sel=val.market.eq(market).to_numpy();allsel=full.market.eq(market).to_numpy()
        for h in range(2):
            correct={}
            for name,(p,m) in outputs.items():
                pred=p[sel,h].argmax(-1);actual=data.cls[indices][sel,h];c=(pred==actual).astype(float)
                daily=pd.Series(c,index=val.loc[sel,'anchor_date']).groupby(level=0).mean().sort_index();correct[name]=daily
                metrics.append(dict(model=name,market=market,horizon=(1,3)[h],n_pairs=int(sel.sum()),n_sessions=len(daily),session_accuracy=float(daily.mean()),pair_accuracy=float(c.mean()),balanced_accuracy=float(np.mean([(pred[actual==k]==k).mean() for k in range(3)]))))
                if name in MODES:
                    allc=np.zeros(len(full));allc[positions]=(p[:,h].argmax(-1)==data.cls[indices,h]);acc=pd.Series(allc[allsel],index=full.loc[allsel,'anchor_date']).groupby(level=0).mean().mean()
                else:
                    # Baseline source predictions cover all original pairs, including hygiene rejections.
                    paths={'original':SOURCE/'predictions_stage3.npz','majority':SOURCE/'predictions_majority.npz','ticker_prior':HERE/'artifacts/news_impact_v2_evidence/predictions_ticker_prior.npz'}
                    with np.load(paths[name]) as a:allc=(a['probabilities'][full_ids,h].argmax(-1)==full_actual[:,h]).astype(float)
                    acc=pd.Series(allc[allsel],index=full.loc[allsel,'anchor_date']).groupby(level=0).mean().mean()
                adjusted.append(dict(model=name,market=market,horizon=(1,3)[h],original_population_accuracy=float(acc)))
            for baseline in ('clean_old','no_content','original','majority','ticker_prior'):
                diff=(correct['expanded']-correct[baseline]).to_numpy();delta=float(diff.mean())
                for lag in (5,20):
                    se=hac_mean_se(diff,lag);p=float(2*norm.sf(abs(delta/se))) if se else float(delta==0)
                    contrasts.append(dict(baseline=baseline,market=market,horizon=(1,3)[h],lag=lag,delta=delta,se=se,p=p))
    report=pd.DataFrame(metrics);comp=pd.DataFrame(contrasts)
    for lag in (5,20):
        m=comp.lag.eq(lag);comp.loc[m,'p_holm_20']=holm(comp.loc[m,'p'].to_numpy())
    report.to_csv(out/'validation_metrics.csv',index=False);comp.to_csv(out/'validation_comparisons.csv',index=False);pd.DataFrame(adjusted).to_csv(out/'coverage_adjusted_metrics.csv',index=False)
    conditional=report.groupby('model').session_accuracy.mean().to_dict();scores=pd.DataFrame(adjusted).groupby('model').original_population_accuracy.mean().to_dict()
    return dict(validation_only=True,original_direct_pairs=len(full_ids),retained_pairs=len(indices),conditional_accuracy=conditional,original_population_accuracy=scores,user_target=.65,user_target_met=scores['expanded']>=.65,automatic_model_replacement=False)


def train(out):
    start=time.monotonic();torch.set_num_threads(4);protocol=json.loads((out/'protocol.json').read_text());prepared=json.loads((out/'prepared.json').read_text())
    for path,digest in protocol['source_hashes'].items():assert sha(Path(path))==digest,path
    for name,digest in protocol['code_hashes'].items():assert sha(HERE/name)==digest,name
    for name,digest in prepared['artifact_hashes'].items():assert sha(out/name)==digest,name
    indices=np.load(out/'pair_indices.npy');mapping=np.load(out/'feature_mapping.npy');text=np.load(out/'target_text.npy');sent=np.load(out/'target_sentiment.npy');outputs={};selections={}
    for mode in MODES:
        data=load_data(out,mode);folder=out/mode;folder.mkdir();dump(folder/'MODEL_POLICY.json',dict(research_only=True,validation_only=True,current_event_training=True,mode=mode))
        x,s=(np.zeros_like(text),np.zeros_like(sent)) if mode=='no_content' else (text,sent)
        outputs[mode],selections[mode]=fit_target(data,mapping,x,s,folder,indices);dump(out/'selections.json',selections)
        del data;torch.cuda.empty_cache()
    data=load_data(out,'expanded')
    baseline_paths={'original':SOURCE/'predictions_stage3.npz','majority':SOURCE/'predictions_majority.npz','ticker_prior':HERE/'artifacts/news_impact_v2_evidence/predictions_ticker_prior.npz'}
    dump(out/'baseline_hashes.json',{str(p):sha(p) for p in baseline_paths.values()})
    for name,path in baseline_paths.items():
        with np.load(path) as a:outputs[name]=(a['probabilities'][indices],a['magnitude'][indices])
    verdict=evaluate(data,indices,outputs,out)
    for path,digest in protocol['source_hashes'].items():assert sha(Path(path))==digest,path
    dump(out/'completion.json',verdict|dict(training_seconds=time.monotonic()-start,source_unchanged=True));print(json.dumps(verdict),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,required=True);ap.add_argument('--stage',choices=('prepare','train'),required=True);args=ap.parse_args()
    (prepare if args.stage=='prepare' else train)(args.out.resolve())
