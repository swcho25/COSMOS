"""Fixed follow-up: add completed-session price context, retain the original goal.

Prepare CPU features while the previous fixed experiment finishes. Then train
news+price and price-only GATs, three seeds each, on identical direct samples.
No label changes, no new test predictions. Original 4813-pair accuracy includes
input-quality abstentions as wrong. Reused 2023 validation is exploratory.
"""
import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pandas as pd
import torch
from scipy.stats import norm
from market_context_features import build_context,FEATURE_NAMES
from train_market_context_news import fit_market
from train_news_impact import targets
from news_impact_data import node_features,graph_arrays
from audit_news_impact import hac_mean_se
from verify_news_predictive_value import sha,dump,holm

HERE=Path(__file__).resolve().parent
MODES=('news_price','price_only')
BASELINES=('price_only','target','pooled','no_content','original','majority','ticker_prior','stage2')


def load_data(parent):
    snapshot=parent/'training_inputs';d=SimpleNamespace()
    d.docs=[json.loads(l) for l in (snapshot/'articles.jsonl').open(encoding='utf8')]
    eligible=set(json.loads((parent/'eligible_articles.json').read_text()))
    d.docs=[r if i in eligible else dict(r,split='excluded') for i,r in enumerate(d.docs)]
    d.meta=pd.read_json(snapshot/'companies.json',dtype={'ticker':str});d.tickers=d.meta.ticker.tolist()
    d.pairs=pd.read_parquet(snapshot/'pairs.parquet');d.article_idx=d.pairs.article.to_numpy()
    d.node_idx=d.pairs.ticker.map({t:i for i,t in enumerate(d.tickers)}).to_numpy()
    d.graphs=json.loads((snapshot/'graphs.json').read_text());d.static={k:node_features(d.meta,g) for k,g in d.graphs.items()}
    d.graph_arrays={k:graph_arrays(g,d.tickers) for k,g in d.graphs.items()}
    d.train=d.pairs.split.eq('train').to_numpy();d.val=d.pairs.split.eq('val').to_numpy()
    d.y=d.pairs[['y_1d','y_3d']].to_numpy(dtype='float32');d.cls,d.mag,d.thresholds,d.scales=targets(d.y,d.train)
    with np.load(snapshot/'label_config.npz') as c:
        np.testing.assert_array_equal(d.thresholds,c['thresholds']);np.testing.assert_array_equal(d.scales,c['scales'])
    d.mask=np.load(parent/'direct_mask.npy');d.mentions=np.load(parent/'mentions.npy')
    d.dense_cls=np.zeros((*d.mask.shape,2),dtype='int64');d.dense_mag=np.zeros((*d.mask.shape,2),dtype='float32')
    d.dense_cls[d.article_idx,d.node_idx]=d.cls;d.dense_mag[d.article_idx,d.node_idx]=d.mag
    indices=np.flatnonzero(d.val & d.pairs.is_direct.to_numpy() & d.mask[d.article_idx,d.node_idx])
    return d,indices


def prepare(parent,out):
    out.mkdir(parents=True,exist_ok=False);start=time.monotonic();source=HERE/'artifacts/news_impact_v2'
    dependencies=[p for p in (parent/'training_inputs').iterdir() if p.is_file()]
    dependencies += [parent/n for n in ('protocol.json','feature_manifest.json','feature_entries.json','feature_mapping.npy','direct_mask.npy','mentions.npy','eligible_articles.json','target_text.npy','target_sentiment.npy')]
    dependencies += [HERE/'data/prices_daily.parquet']+[source/f'index_{m}.csv' for m in ('KOSPI','NASDAQ')]
    hashes={str(p):sha(p) for p in dependencies}
    code=['experiment_market_context.py','market_context_features.py','train_market_context_news.py','target_news_model.py','news_impact_model.py','train_news_impact.py','news_impact_data.py']
    dump(out/'protocol.json',dict(parent=str(parent),source_hashes=hashes,code_hashes={n:sha(HERE/n) for n in code},
        modes=MODES,seeds=[0,1,2],baselines=BASELINES,user_target=.65,
        goal='all 4813 original direct val pairs, equal sessions and four cells, rejected inputs count as unanswered/incorrect; retained score also >=.65',
        numeric_features=FEATURE_NAMES,context_dim=25,availability_buffer_minutes=15,
        normalization='train eligible articles/nodes only, explicit missing indicators, clip standardized features to [-8,8]',
        settings=dict(lr=.0008,weight_decay=.001,batch_size=24,epochs=30,patience=6,hidden=64,dropout=.15),
        rationale='Target context alone was 39.09% on reused val; investigate past market state while keeping labels and samples fixed',
        statistics='32 HAC contrasts per lag 5/20, Holm per lag; exploratory reused validation',
        limitations='Downloaded adjusted prices may be revised; legacy historical graphs preserved; not a real-time archive',
        validation_only=True,automatic_model_replacement=False))
    data,indices=load_data(parent);prices=pd.read_parquet(HERE/'data/prices_daily.parquet');benchmarks={}
    for market in ('KOSPI','NASDAQ'):
        f=pd.read_csv(source/f'index_{market}.csv');benchmarks[market]=pd.Series(f.Close.to_numpy(),index=pd.to_datetime(f.Date.str[:10]))
    context,scaler,cutoffs=build_context(data.docs,data.meta,prices,benchmarks)
    assert np.isfinite(context).all();np.save(out/'market_context.npy',context);np.savez(out/'normalization.npz',**scaler)
    pd.DataFrame(cutoffs).to_parquet(out/'price_cutoffs.parquet',index=False);np.save(out/'pair_indices.npy',indices)
    for path,digest in hashes.items():assert sha(Path(path))==digest,path
    dump(out/'prepared.json',dict(seconds=time.monotonic()-start,shape=list(context.shape),validation_pairs=len(indices),
        hashes={n:sha(out/n) for n in ('market_context.npy','normalization.npz','price_cutoffs.parquet','pair_indices.npy')}))
    print('MARKET CONTEXT PREPARED',context.shape,flush=True)


def get_outputs(parent,out,indices):
    result={};files=[]
    for mode in MODES:files.append((mode,out/mode/'validation_predictions.npz'))
    for mode in ('target','pooled','no_content'):files.append((mode,parent/mode/'validation_predictions.npz'))
    for name,p in files:
        with np.load(p) as a:
            np.testing.assert_array_equal(a['pair_indices'],indices);result[name]=(a['probabilities'],a['magnitude'])
    source=HERE/'artifacts/news_impact_v2'
    for name,path in [('original',source/'predictions_stage3.npz'),('majority',source/'predictions_majority.npz'),('stage2',source/'predictions_stage2.npz'),('ticker_prior',HERE/'artifacts/news_impact_v2_evidence/predictions_ticker_prior.npz')]:
        with np.load(path) as a:result[name]=(a['probabilities'][indices],a['magnitude'][indices])
    return result


def evaluate(data,indices,output,out):
    val=data.pairs.iloc[indices];actual=data.cls[indices];rows=[];contrasts=[]
    for market in ('KOSPI','NASDAQ'):
        sel=val.market.eq(market).to_numpy();dates=val.loc[sel,'anchor_date']
        for h in range(2):
            correct={}
            for mode,(p,m) in output.items():
                np.testing.assert_allclose(p.sum(-1),1,atol=1e-5);assert np.isfinite(m).all()
                pred=p[sel,h].argmax(-1);c=(pred==actual[sel,h]);daily=pd.Series(c.astype(float),index=dates).groupby(level=0).mean().sort_index();correct[mode]=daily
                recalls=[float((pred[actual[sel,h]==k]==k).mean()) for k in range(3)]
                mae=pd.Series(abs(m[sel,h]-data.mag[indices][sel,h]),index=dates).groupby(level=0).mean().mean()
                rows.append(dict(model=mode,market=market,horizon=(1,3)[h],n_pairs=int(sel.sum()),n_sessions=len(daily),session_accuracy=float(daily.mean()),pair_accuracy=float(c.mean()),balanced_accuracy=float(np.mean(recalls)),session_magnitude_mae=float(mae)))
            for baseline in BASELINES:
                diff=(correct['news_price']-correct[baseline]).to_numpy();delta=float(diff.mean())
                for lag in (5,20):
                    se=hac_mean_se(diff,lag);p=float(2*norm.sf(abs(delta/se))) if se else float(delta==0)
                    contrasts.append(dict(baseline=baseline,market=market,horizon=(1,3)[h],lag=lag,delta=delta,se=se,p=p))
    metrics=pd.DataFrame(rows);comp=pd.DataFrame(contrasts)
    for lag in (5,20):
        mask=comp.lag.eq(lag);assert mask.sum()==32;comp.loc[mask,'p_holm_32']=holm(comp.loc[mask,'p'].to_numpy())
    metrics.to_csv(out/'validation_metrics.csv',index=False);comp.to_csv(out/'validation_comparisons.csv',index=False)
    macro=metrics.groupby('model').session_accuracy.mean();macro.to_csv(out/'validation_macro.csv')
    full_ids=np.flatnonzero(data.val & data.pairs.is_direct.to_numpy());full=data.pairs.iloc[full_ids];pos=np.searchsorted(full_ids,indices);goal_rows=[]
    for mode in MODES:
        correct=np.zeros((len(full_ids),2));correct[pos]=output[mode][0].argmax(-1)==actual
        for market in ('KOSPI','NASDAQ'):
            sel=full.market.eq(market).to_numpy()
            for h in range(2):
                acc=pd.Series(correct[sel,h],index=full.loc[sel,'anchor_date']).groupby(level=0).mean().mean()
                goal_rows.append(dict(model=mode,market=market,horizon=(1,3)[h],all_original_direct_session_accuracy=float(acc)))
    goal=pd.DataFrame(goal_rows);goal.to_csv(out/'coverage_adjusted_metrics.csv',index=False);score=goal.groupby('model').all_original_direct_session_accuracy.mean().to_dict()
    return dict(validation_only=True,original_direct_pairs=len(full_ids),retained_pairs=len(indices),coverage=len(indices)/len(full_ids),
        conditional_accuracy=macro.to_dict(),original_population_accuracy=score,user_target=.65,user_target_met=bool(score['news_price']>=.65 and macro['news_price']>=.65),automatic_model_replacement=False)


def train(parent,out):
    start=time.monotonic();protocol=json.loads((out/'protocol.json').read_text());prepared=json.loads((out/'prepared.json').read_text())
    for path,digest in protocol['source_hashes'].items():assert sha(Path(path))==digest,path
    for name,digest in protocol['code_hashes'].items():assert sha(HERE/name)==digest,name
    for name,digest in prepared['hashes'].items():assert sha(out/name)==digest,name
    assert (parent/'completion.json').exists(),'Previous fixed controls must finish first'
    baselines=[parent/m/'validation_predictions.npz' for m in ('target','pooled','no_content')]
    baselines += [HERE/'artifacts/news_impact_v2'/n for n in ('predictions_stage3.npz','predictions_majority.npz','predictions_stage2.npz')]+[HERE/'artifacts/news_impact_v2_evidence/predictions_ticker_prior.npz']
    baseline_hashes={str(p):sha(p) for p in baselines};dump(out/'baseline_hashes.json',baseline_hashes)
    assert torch.cuda.is_available();torch.set_num_threads(4);data,indices=load_data(parent)
    np.testing.assert_array_equal(indices,np.load(out/'pair_indices.npy'));context=np.load(out/'market_context.npy')
    mapping=np.load(parent/'feature_mapping.npy');text=np.load(parent/'target_text.npy');sent=np.load(parent/'target_sentiment.npy');selections={}
    for mode in MODES:
        folder=out/mode;folder.mkdir();dump(folder/'MODEL_POLICY.json',dict(research_only=True,validation_only=True,direct_only=True,past_price_context=True))
        x,s=(text,sent) if mode=='news_price' else (np.zeros_like(text),np.zeros_like(sent))
        _,selections[mode]=fit_market(data,mapping,x,s,context,folder,indices);dump(out/'selections.json',selections)
    verdict=evaluate(data,indices,get_outputs(parent,out,indices),out)
    for path,digest in (protocol['source_hashes']|baseline_hashes).items():assert sha(Path(path))==digest,path
    dump(out/'completion.json',verdict|dict(training_seconds=time.monotonic()-start,source_unchanged=True));print(json.dumps(verdict),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--parent',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);ap.add_argument('--stage',choices=['prepare','train'],required=True);args=ap.parse_args()
    parent=args.parent.resolve();out=args.out.resolve()
    if parent==out or parent in out.parents or out in parent.parents:raise ValueError('Use a new sibling experiment folder')
    (prepare if args.stage=='prepare' else train)(parent,out)
