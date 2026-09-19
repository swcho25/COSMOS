"""Independent CPU checkpoint/row/statistics checks for balanced validation runs.

Uses exact frozen projected encoder outputs. Raw context encoder replay was already
verified in verify_news_context; this check covers the newly trained GATs, all seed
ensemble rows, and independently rebuilt per-session HAC/Holm statistics.
"""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from scipy.stats import norm
from news_impact_model import NewsGAT
from news_impact_data import graph_arrays,node_features,mention_array
from verify_news_predictive_value import sha,dump

HERE=Path(__file__).resolve().parent


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--experiment',type=Path,required=True);args=ap.parse_args()
    out=args.experiment.resolve();snapshot=out/'training_inputs';torch.set_num_threads(4)
    protocol=json.loads((out/'protocol.json').read_text(encoding='utf8'))
    completion=json.loads((out/'completion.json').read_text(encoding='utf8'));assert completion['validation_only']
    for path,digest in protocol['source_hashes'].items():assert sha(Path(path))==digest,path
    for name,digest in protocol['code_hashes'].items():assert sha(HERE/name)==digest,name
    docs=[json.loads(l) for l in (snapshot/'articles.jsonl').open(encoding='utf8')]
    pairs=pd.read_parquet(snapshot/'pairs.parquet');indices=np.flatnonzero(pairs.split.eq('val').to_numpy())
    val=pairs.iloc[indices];ids=[]
    for region in ('domestic','overseas'):
        eligible=[i for i,d in enumerate(docs) if d['split']=='val' and d['region']==region]
        ids += [eligible[0],eligible[len(eligible)//2],eligible[-1]]
    meta=pd.read_json(snapshot/'companies.json',dtype={'ticker':str});tickers=meta.ticker.tolist()
    graphs=json.loads((snapshot/'graphs.json').read_text(encoding='utf8'))
    outputs={};max_error=0.
    for mode in protocol['modes']:
        folder=out/mode
        text=np.load(snapshot/('context_text.npy' if mode=='balanced_context' else 'original_text.npy'))
        sent=np.load(snapshot/('context_sentiment.npy' if mode=='balanced_context' else 'sentiment.npy'))
        if mode=='balanced_no_content':text=np.zeros_like(text);sent=np.zeros_like(sent)
        with np.load(folder/'validation_predictions.npz') as saved:
            np.testing.assert_array_equal(saved['pair_indices'],indices)
            expected_p,expected_m=saved['probabilities'],saved['magnitude']
        outputs[mode]=(expected_p,expected_m);seed_p=[];seed_m=[]
        for seed in range(3):
            with np.load(folder/f'validation_seed{seed}.npz') as saved:
                np.testing.assert_array_equal(saved['pair_indices'],indices)
                sp,sm=saved['probabilities'],saved['magnitude'];seed_p.append(sp);seed_m.append(sm)
            checkpoint=torch.load(folder/f'gat_seed{seed}.pt',map_location='cpu',weights_only=True)
            model=NewsGAT(**checkpoint['config']).eval();model.load_state_dict(checkpoint['state_dict'])
            assert checkpoint['loss_policy']=='direct_indirect_equal_groups'
            with torch.inference_mode():
                for i in ids:
                    d=docs[i];g=graphs[d['graph']];e,a=graph_arrays(g,tickers)
                    inputs=[torch.from_numpy(x) for x in (text[i:i+1],sent[i:i+1],mention_array(d['companies'],tickers)[None],node_features(meta,g),e,a)]
                    logits,m=model(*inputs);p=logits.softmax(-1)[0].numpy();m=m[0].numpy()
                    rows=np.flatnonzero(val.article.to_numpy()==i);ni=np.array([tickers.index(t) for t in val.iloc[rows].ticker])
                    for actual,expected in ((p[ni],sp[rows]),(m[ni],sm[rows])):
                        np.testing.assert_allclose(actual,expected,atol=2e-5,rtol=1e-4)
                        max_error=max(max_error,float(abs(actual-expected).max()))
        np.testing.assert_array_equal(np.mean(seed_p,axis=0),expected_p)
        np.testing.assert_array_equal(np.mean(seed_m,axis=0),expected_m)
    source=HERE/'artifacts/news_impact_v2';evidence=HERE/'artifacts/news_impact_v2_evidence'
    paths={'original':source/'predictions_stage3.npz','old_no_content':evidence/'no_content/predictions.npz',
        'majority':source/'predictions_majority.npz','stage2':source/'predictions_stage2.npz',
        'market_prior':evidence/'predictions_market_prior.npz','ticker_prior':evidence/'predictions_ticker_prior.npz'}
    for name,path in paths.items():
        with np.load(path) as saved:outputs[name]=(saved['probabilities'][indices],saved['magnitude'][indices])
    y=val[['y_1d','y_3d']].to_numpy(dtype='float32')
    with np.load(snapshot/'label_config.npz') as saved:threshold=saved['thresholds']
    actual=np.where(y>threshold,2,np.where(y< -threshold,0,1))
    comp=pd.read_csv(out/'validation_comparisons.csv');errors=[]
    for r in comp.itertuples():
        mask=val.market.eq(r.market).to_numpy()
        if r.scope!='all':mask &= val.is_direct.to_numpy()==(r.scope=='direct')
        h={1:0,3:1}[r.horizon]
        diff=(outputs[r.candidate][0][mask,h].argmax(-1)==actual[mask,h]).astype(float)-(outputs[r.baseline][0][mask,h].argmax(-1)==actual[mask,h])
        series=pd.Series(diff,index=val.loc[mask,'anchor_date']).groupby(level=0).mean().sort_index().to_numpy()
        delta=series.mean();z=series-delta;n=len(z)
        variance=np.dot(z,z)/n
        for lag in range(1,min(r.lag,n-1)+1):variance += 2*(1-lag/(r.lag+1))*np.dot(z[lag:],z[:-lag])/n
        se=np.sqrt(max(variance,0)/n)
        p=float(2*norm.sf(abs(delta/se))) if se>0 else float(delta==0)
        np.testing.assert_allclose([delta,se,p],[r.delta,r.se,r.p],atol=1e-12,rtol=1e-9)
        errors.append(max(abs(delta-r.delta),abs(se-r.se),abs(p-r.p)))
    for lag in (5,20):
        subset=comp[comp.lag.eq(lag)].sort_values('p');running=0.
        for j,r in enumerate(subset.itertuples()):
            running=max(running,min(1,r.p*(len(subset)-j)))
            assert abs(running-r.p_holm_168)<1e-10
    result=dict(validation_only=True,source_unchanged=True,articles=ids,checkpoints=9,ensemble_rows_per_mode=len(indices),
        max_cpu_prediction_error=max_error,statistical_comparisons=len(comp),max_statistical_error=max(errors))
    dump(out/'reload_verification.json',result);print(json.dumps(result),flush=True)


if __name__=='__main__':main()
