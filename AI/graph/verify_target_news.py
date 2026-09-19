"""CPU replay, ensemble/row checks, independent HAC and coverage-aware goal score.

Replay frozen encoders on six validation target pairs, and all nine GAT checkpoints.
The 65% goal counts rejected input-quality pairs as unanswered on the original
direct population, so filtering cannot manufacture attainment.
"""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from scipy.stats import norm
from target_news_features import clean_target_article,TargetEncoder
from target_news_model import TargetNewsGAT
from news_impact_data import graph_arrays,node_features
from verify_news_predictive_value import sha,dump

HERE=Path(__file__).resolve().parent


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--experiment',type=Path,required=True);args=ap.parse_args();out=args.experiment.resolve()
    snapshot=out/'training_inputs';torch.set_num_threads(4)
    protocol=json.loads((out/'protocol.json').read_text(encoding='utf8'));completion=json.loads((out/'completion.json').read_text(encoding='utf8'))
    assert completion['validation_only']
    for path,digest in protocol['source_hashes'].items():assert sha(Path(path))==digest,path
    for name,digest in protocol['code_hashes'].items():assert sha(HERE/name)==digest,name
    manifest=json.loads((out/'feature_manifest.json').read_text(encoding='utf8'))
    for name,digest in manifest['hashes'].items():assert sha(out/name)==digest,name
    assert sha(out/'feature_entries.json')==manifest['entries_sha']
    assert sha(out/'feature_audit.json')==manifest['audit_sha']
    feature_audit=pd.read_json(out/'feature_audit.json')
    input_checks=[]
    for role,group in feature_audit.groupby('role'):
        limit=256 if role=='embedding' else 128
        assert group.tokens.max()<=limit
        assert not group.feature_id.duplicated().any()
        input_checks.append(dict(role=role,rows=len(group),max_tokens=int(group.tokens.max()),literal_target_coverage=float(group.covered.mean())))
    pd.DataFrame(input_checks).to_csv(out/'feature_coverage.csv',index=False)
    entries=json.loads((out/'feature_entries.json').read_text(encoding='utf8'))
    docs=[json.loads(l) for l in (snapshot/'articles.jsonl').open(encoding='utf8')]
    pairs=pd.read_parquet(snapshot/'pairs.parquet');meta=pd.read_json(snapshot/'companies.json',dtype={'ticker':str});tickers=meta.ticker.tolist();names=meta.set_index('ticker').name_official.to_dict()
    graphs=json.loads((snapshot/'graphs.json').read_text(encoding='utf8'))
    mapping=np.load(out/'feature_mapping.npy');mentions=np.load(out/'mentions.npy');mask=np.load(out/'direct_mask.npy')
    node=pairs.ticker.map({t:i for i,t in enumerate(tickers)}).to_numpy();article=pairs.article.to_numpy()
    indices=np.flatnonzero(pairs.split.eq('val').to_numpy() & pairs.is_direct.to_numpy() & mask[article,node]);val=pairs.iloc[indices]
    assert len(indices)==completion['validation_pairs']
    selected=[]
    for market in ('KOSPI','NASDAQ'):
        rows=np.flatnonzero(val.market.eq(market).to_numpy());selected += [int(rows[0]),int(rows[len(rows)//2]),int(rows[-1])]
    ids=[int(article[indices[j]]) for j in selected];fids=[int(mapping[article[indices[j]],node[indices[j]]]) for j in selected]
    clean=[clean_target_article(docs[i])[0] for i in ids];sample_entries=[dict(entries[fid-1],article=j) for j,fid in enumerate(fids)]
    enc=TargetEncoder(snapshot,device='cpu');embedding,_=enc.encode_entries('embedding',clean,sample_entries,names);enc.release()
    sentiment=np.zeros((len(selected)+1,3),dtype='float32')
    for role in ('sentiment_ko','sentiment_en'):
        v,_=enc.encode_entries(role,clean,sample_entries,names);sentiment+=v;enc.release()
    stored_emb=np.load(out/'target_embedding.npy');stored_sent=np.load(out/'target_sentiment.npy')
    np.testing.assert_allclose(embedding[1:],stored_emb[fids],atol=2e-5,rtol=1e-4)
    np.testing.assert_allclose(sentiment[1:],stored_sent[fids],atol=2e-5,rtol=1e-4)
    text=np.load(out/'target_text.npy');pooled=np.load(out/'pooled_text.npy');pooled_sent=np.load(out/'pooled_sentiment.npy')
    with np.load(out/'target_projection.npz') as projection:
        projected=(stored_emb@projection['components'].T-projection['mean']@projection['components'].T).astype('float32');projected[0]=0
    np.testing.assert_allclose(projected,text,atol=1e-7,rtol=1e-5)
    for i in np.flatnonzero((mapping>0).any(1)):
        fi=mapping[i];fi=fi[fi>0]
        np.testing.assert_allclose(pooled[fi],np.repeat(text[fi].mean(0)[None],len(fi),axis=0),atol=1e-7)
        np.testing.assert_allclose(pooled_sent[fi],np.repeat(stored_sent[fi].mean(0)[None],len(fi),axis=0),atol=1e-7)
    outputs={};max_error=0.
    for mode in protocol['modes']:
        folder=out/mode;xx,ss=(text,stored_sent) if mode=='target' else ((pooled,pooled_sent) if mode=='pooled' else (np.zeros_like(text),np.zeros_like(stored_sent)))
        with np.load(folder/'validation_predictions.npz') as a:
            np.testing.assert_array_equal(a['pair_indices'],indices);p,m=a['probabilities'],a['magnitude']
        outputs[mode]=(p,m);ps=[];ms=[]
        for seed in range(3):
            with np.load(folder/f'validation_seed{seed}.npz') as a:
                np.testing.assert_array_equal(a['pair_indices'],indices);sp,sm=a['probabilities'],a['magnitude'];ps.append(sp);ms.append(sm)
            ckpt=torch.load(folder/f'gat_seed{seed}.pt',map_location='cpu',weights_only=True);assert ckpt['loss']=='direct_only'
            model=TargetNewsGAT(**ckpt['config']).eval();model.load_state_dict(ckpt['state_dict'])
            with torch.inference_mode():
                for i in sorted(set(ids)):
                    g=graphs[docs[i]['graph']];e,a=graph_arrays(g,tickers);fi=mapping[i:i+1]
                    inp=[torch.from_numpy(x) for x in (xx[fi],ss[fi],mentions[i:i+1],node_features(meta,g),e,a)]
                    logits,pred=model(*inp);pp=logits.softmax(-1)[0].numpy();mm=pred[0].numpy();rows=np.flatnonzero(val.article.to_numpy()==i);ni=node[indices[rows]]
                    for actual,expected in ((pp[ni],sp[rows]),(mm[ni],sm[rows])):
                        np.testing.assert_allclose(actual,expected,atol=2e-5,rtol=1e-4);max_error=max(max_error,float(abs(actual-expected).max()))
        np.testing.assert_array_equal(np.mean(ps,axis=0),p);np.testing.assert_array_equal(np.mean(ms,axis=0),m)
    source=HERE/'artifacts/news_impact_v2';evidence=HERE/'artifacts/news_impact_v2_evidence';balanced=HERE/'artifacts/news_balanced_experiment_20260915'
    for name,path in [('original',source/'predictions_stage3.npz'),('majority',source/'predictions_majority.npz'),('stage2',source/'predictions_stage2.npz'),('ticker_prior',evidence/'predictions_ticker_prior.npz')]:
        with np.load(path) as a:outputs[name]=(a['probabilities'][indices],a['magnitude'][indices])
    for name in ('balanced_original','balanced_no_content'):
        with np.load(balanced/name/'validation_predictions.npz') as a:
            positions=np.searchsorted(a['pair_indices'],indices);np.testing.assert_array_equal(a['pair_indices'][positions],indices)
            outputs[name]=(a['probabilities'][positions],a['magnitude'][positions])
    with np.load(snapshot/'label_config.npz') as a:threshold=a['thresholds']
    y=val[['y_1d','y_3d']].to_numpy(dtype='float32');labels=np.where(y>threshold,2,np.where(y< -threshold,0,1))
    comp=pd.read_csv(out/'validation_comparisons.csv');errors=[]
    for r in comp.itertuples():
        sel=val.market.eq(r.market).to_numpy();h={1:0,3:1}[r.horizon]
        diff=(outputs['target'][0][sel,h].argmax(-1)==labels[sel,h]).astype(float)-(outputs[r.baseline][0][sel,h].argmax(-1)==labels[sel,h])
        daily=pd.Series(diff,index=val.loc[sel,'anchor_date']).groupby(level=0).mean().sort_index().to_numpy()
        delta=daily.mean();z=daily-delta;n=len(z);variance=z@z/n
        for k in range(1,min(r.lag,n-1)+1):variance+=2*(1-k/(r.lag+1))*(z[k:]@z[:-k])/n
        se=np.sqrt(max(variance,0)/n);p=float(2*norm.sf(abs(delta/se))) if se else float(delta==0)
        np.testing.assert_allclose([delta,se,p],[r.delta,r.se,r.p],atol=1e-12,rtol=1e-9);errors.append(max(abs(delta-r.delta),abs(se-r.se),abs(p-r.p)))
    for lag in (5,20):
        sub=comp[comp.lag.eq(lag)].sort_values('p');running=0.
        for j,r in enumerate(sub.itertuples()):running=max(running,min(1,r.p*(len(sub)-j)));assert abs(running-r.p_holm_32)<1e-10
    full_ids=np.flatnonzero(pairs.split.eq('val').to_numpy() & pairs.is_direct.to_numpy());full=pairs.iloc[full_ids]
    covered=np.isin(full_ids,indices);positions=np.searchsorted(full_ids,indices);goal_rows=[]
    full_y=full[['y_1d','y_3d']].to_numpy(dtype='float32');full_labels=np.where(full_y>threshold,2,np.where(full_y< -threshold,0,1))
    for mode in list(protocol['modes'])+['original']:
        if mode=='original':
            with np.load(source/'predictions_stage3.npz') as a:correct=(a['probabilities'][full_ids].argmax(-1)==full_labels).astype(float)
        else:
            correct=np.zeros((len(full_ids),2),dtype=float);correct[positions]=outputs[mode][0].argmax(-1)==labels
        for market in ('KOSPI','NASDAQ'):
            smask=full.market.eq(market).to_numpy()
            for h in range(2):
                acc=pd.Series(correct[smask,h],index=full.loc[smask,'anchor_date']).groupby(level=0).mean().mean()
                goal_rows.append(dict(model=mode,market=market,horizon=(1,3)[h],all_original_direct_session_accuracy=float(acc),coverage=1. if mode=='original' else float(covered[smask].mean())))
    goal=pd.DataFrame(goal_rows);goal.to_csv(out/'coverage_adjusted_metrics.csv',index=False)
    goal_macro=goal.groupby('model').all_original_direct_session_accuracy.mean().to_dict()
    result=dict(validation_only=True,source_unchanged=True,checkpoints=9,replayed_target_pairs=len(selected),
        raw_encoder_max_error=max(float(abs(embedding[1:]-stored_emb[fids]).max()),float(abs(sentiment[1:]-stored_sent[fids]).max())),
        max_cpu_prediction_error=max_error,statistical_comparisons=len(comp),max_statistical_error=max(errors),
        original_direct_pairs=len(full_ids),retained_pairs=len(indices),coverage=float(covered.mean()),
        original_population_accuracy=goal_macro,user_target=.65,user_target_met=goal_macro['target']>=.65 and completion['target_accuracy']>=.65,
        goal_protocol_sha=sha(out/'goal_metric_protocol.json'))
    dump(out/'reload_verification.json',result);print(json.dumps(result),flush=True)


if __name__=='__main__':main()
