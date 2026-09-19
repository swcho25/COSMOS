"""Independent price arithmetic, all cutoff boundaries, CPU replay and statistics.

Six target pairs exercise raw price construction; every cutoff and saved prediction
row is checked. This does not turn reused validation into an independent test.
"""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from scipy.stats import norm
from experiment_market_context import load_data,get_outputs,MODES,HERE
from target_news_model import TargetNewsGAT
from news_impact_data import close_times
from verify_news_predictive_value import sha,dump


def main(out):
    torch.set_num_threads(4);protocol=json.loads((out/'protocol.json').read_text());parent=Path(protocol['parent'])
    prepared=json.loads((out/'prepared.json').read_text());done=json.loads((out/'completion.json').read_text());assert done['validation_only']
    for path,digest in (protocol['source_hashes']|json.loads((out/'baseline_hashes.json').read_text())).items():assert sha(Path(path))==digest,path
    for name,digest in protocol['code_hashes'].items():assert sha(HERE/name)==digest,name
    for name,digest in prepared['hashes'].items():assert sha(out/name)==digest,name
    d,indices=load_data(parent);np.testing.assert_array_equal(indices,np.load(out/'pair_indices.npy'))
    val=d.pairs.iloc[indices];outputs=get_outputs(parent,out,indices);context=np.load(out/'market_context.npy',mmap_mode='r')
    mapping=np.load(parent/'feature_mapping.npy');text=np.load(parent/'target_text.npy');sent=np.load(parent/'target_sentiment.npy')
    cutoffs=pd.read_parquet(out/'price_cutoffs.parquet');prices=pd.read_parquet(HERE/'data/prices_daily.parquet')
    selected=[];panels={};cutoff_count=0
    for market in ('KOSPI','NASDAQ'):
        p=prices[prices.market.eq(market)].pivot(index='trading_at',columns='ticker',values='adj_close').sort_index();p.index=pd.to_datetime(p.index)
        closes=close_times(p.index,market);f=pd.read_csv(HERE/f'artifacts/news_impact_v2/index_{market}.csv')
        benchmark=pd.Series(f.Close.to_numpy(),index=pd.to_datetime(f.Date.str[:10])).reindex(p.index);panels[market]=(p,closes,benchmark)
        c=cutoffs[cutoffs.market.eq(market)];available=pd.DatetimeIndex([d.docs[i]['available_at'] for i in c.article]);stored=pd.DatetimeIndex(c.close_time)
        pos=closes.searchsorted(available-pd.Timedelta(minutes=15),side='right')-1
        np.testing.assert_array_equal(stored.asi8,closes[pos].asi8);assert (stored+pd.Timedelta(minutes=15)<=available).all();cutoff_count+=len(c)
        rows=np.flatnonzero(val.market.eq(market).to_numpy());selected.extend([int(rows[0]),int(rows[len(rows)//2]),int(rows[-1])])
    with np.load(out/'normalization.npz') as a:mean=a['mean'];std=a['std']
    price_errors=[];ids=[]
    for j in selected:
        r=val.iloc[j];i=int(r.article);ni=int(d.node_idx[indices[j]]);ids.append(i);p,closes,b=panels[r.market]
        available=pd.Timestamp(d.docs[i]['available_at']);end=int(closes.searchsorted(available-pd.Timedelta(minutes=15),side='right'))-1
        stock=p[r.ticker];sr=[np.log(stock.iloc[end]/stock.iloc[end-h]) for h in (1,5,20)];mr=[np.log(b.iloc[end]/b.iloc[end-h]) for h in (1,5,20)]
        trailing=stock.iloc[end-20:end+1].to_numpy();bt=b.iloc[end-20:end+1].to_numpy();window=stock.iloc[end-59:end+1].to_numpy()
        sv=np.std(np.log(trailing[1:]/trailing[:-1]),ddof=0);mv=np.std(np.log(bt[1:]/bt[:-1]),ddof=0)
        draw=np.log(stock.iloc[end]/np.max(window)) if len(window)==60 and np.isfinite(window).all() else np.nan
        raw=np.array(sr+mr+[a-b for a,b in zip(sr,mr)]+[sv,mv,draw],dtype='float32');valid=np.isfinite(raw)
        expected=np.concatenate([np.where(valid,(raw-mean)/std,0).clip(-8,8),valid.astype('float32'),[min((available-closes[end]).total_seconds()/86400,30)/7]])
        np.testing.assert_allclose(expected,context[i,ni],atol=2e-5,rtol=1e-4);price_errors.append(float(abs(expected-context[i,ni]).max()))
    cpu_error=0.;replayed=0
    for mode in MODES:
        x,s=(text,sent) if mode=='news_price' else (np.zeros_like(text),np.zeros_like(sent));ps=[];ms=[]
        for seed in range(3):
            ckpt=torch.load(out/mode/f'gat_seed{seed}.pt',map_location='cpu',weights_only=True);assert ckpt['past_context_dim']==25
            model=TargetNewsGAT(**ckpt['config']).eval();model.load_state_dict(ckpt['state_dict'])
            with np.load(out/mode/f'validation_seed{seed}.npz') as a:
                np.testing.assert_array_equal(a['pair_indices'],indices);sp,sm=a['probabilities'],a['magnitude'];ps.append(sp);ms.append(sm)
            with torch.inference_mode():
                for i in sorted(set(ids)):
                    key=d.docs[i]['graph'];e,a=d.graph_arrays[key];fi=mapping[i:i+1];static=np.concatenate([d.static[key][None],context[i:i+1]],axis=-1)
                    logits,m=model(*[torch.from_numpy(v) for v in (x[fi],s[fi],d.mentions[i:i+1],static,e,a)])
                    rows=np.flatnonzero(val.article.to_numpy()==i);ni=d.node_idx[indices[rows]];replayed+=len(rows)
                    for actual,expected in ((logits.softmax(-1)[0].numpy()[ni],sp[rows]),(m[0].numpy()[ni],sm[rows])):
                        np.testing.assert_allclose(actual,expected,atol=2e-5,rtol=1e-4);cpu_error=max(cpu_error,float(abs(actual-expected).max()))
        np.testing.assert_array_equal(np.mean(ps,axis=0),outputs[mode][0]);np.testing.assert_array_equal(np.mean(ms,axis=0),outputs[mode][1])
    # Independent bincount daily reduction and Newey-West arithmetic.
    comp=pd.read_csv(out/'validation_comparisons.csv');stat_error=0.
    for r in comp.itertuples():
        sel=val.market.eq(r.market).to_numpy();h={1:0,3:1}[r.horizon];actual=d.cls[indices][sel,h]
        diff=(outputs['news_price'][0][sel,h].argmax(-1)==actual).astype(float)-(outputs[r.baseline][0][sel,h].argmax(-1)==actual)
        codes,_=pd.factorize(val.loc[sel,'anchor_date'],sort=True);daily=np.bincount(codes,weights=diff)/np.bincount(codes)
        delta=daily.mean();z=daily-delta;n=len(z);variance=z@z/n
        for k in range(1,min(r.lag,n-1)+1):variance+=2*(1-k/(r.lag+1))*(z[k:]@z[:-k])/n
        se=np.sqrt(max(variance,0)/n);p=float(2*norm.sf(abs(delta/se))) if se else float(delta==0)
        np.testing.assert_allclose([delta,se,p],[r.delta,r.se,r.p],atol=1e-12,rtol=1e-9);stat_error=max(stat_error,abs(p-r.p))
    for lag in (5,20):
        sub=comp[comp.lag.eq(lag)].sort_values('p');running=0.
        for j,r in enumerate(sub.itertuples()):running=max(running,min(1,r.p*(len(sub)-j)));assert abs(running-r.p_holm_32)<1e-10
    full_ids=np.flatnonzero(d.val & d.pairs.is_direct.to_numpy());full=d.pairs.iloc[full_ids];pos=np.searchsorted(full_ids,indices);goal={}
    for mode in MODES:
        correct=np.zeros((len(full_ids),2));correct[pos]=outputs[mode][0].argmax(-1)==d.cls[indices];scores=[]
        for market in ('KOSPI','NASDAQ'):
            sel=full.market.eq(market).to_numpy();codes,_=pd.factorize(full.loc[sel,'anchor_date'],sort=True)
            for h in range(2):scores.append(float((np.bincount(codes,weights=correct[sel,h])/np.bincount(codes)).mean()))
        goal[mode]=float(np.mean(scores));np.testing.assert_allclose(goal[mode],done['original_population_accuracy'][mode],atol=1e-12)
    result=dict(source_unchanged=True,validation_only=True,cutoffs_checked=cutoff_count,raw_price_pairs=len(selected),max_price_feature_error=max(price_errors),
        checkpoints=6,replayed_predictions=replayed,max_cpu_prediction_error=cpu_error,statistical_comparisons=len(comp),max_statistical_error=stat_error,
        original_population_accuracy=goal,user_target_met=done['user_target_met'])
    dump(out/'reload_verification.json',result);print(json.dumps(result),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--experiment',type=Path,required=True);main(ap.parse_args().experiment.resolve())
