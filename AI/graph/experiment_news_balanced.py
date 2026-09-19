"""Fixed validation-only experiment: original/context/no-content, 3 seeds each.

Change only the per-article direct/indirect reduction and its checkpoint objective.
Reuse previously frozen context features. Seven comparators, three scopes, two
markets and two horizons for each of two candidates = 168 contrasts per HAC lag.
Validation is reused research data, not independent evidence of future accuracy.
No test forward pass, automatic promotion or deployment. Preserve source hashes.
"""
import argparse
import copy
import json
import time
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from scipy.stats import norm
from train_news_impact import TrainingData
from train_balanced_news_gat import fit_balanced
from verify_news_predictive_value import sha,dump,holm,COPIES
from audit_news_impact import hac_mean_se

HERE=Path(__file__).resolve().parent
MODES=('balanced_original','balanced_context','balanced_no_content')
BASELINES=('original','old_no_content','balanced_no_content','majority','market_prior','ticker_prior','stage2')


def load_inputs(snapshot,context):
    data=TrainingData(snapshot)
    with np.load(context/'text_projection.npz') as projection:
        # sklearn PCA.transform subtracts the projected mean after projection.
        embedding=np.load(context/'embeddings.npy')
        context_text=(embedding@projection['components'].T-projection['mean']@projection['components'].T).astype('float32')
    context_sentiment=np.load(context/'sentiment.npy')
    assert context_text.shape==data.text.shape
    assert context_sentiment.shape==data.sentiment.shape
    return data,context_text,context_sentiment


def evaluate(data,outputs,out):
    indices=np.flatnonzero(data.val);pairs=data.pairs.iloc[indices]
    cls,mag=data.cls[indices],data.mag[indices]
    rows=[];contrasts=[]
    for market in ('KOSPI','NASDAQ'):
        for scope in ('all','direct','indirect'):
            mask=pairs.market.eq(market).to_numpy()
            if scope!='all':mask &= pairs.is_direct.to_numpy()==(scope=='direct')
            codes,dates=pd.factorize(pairs.loc[mask,'anchor_date'],sort=True)
            counts=np.bincount(codes)
            def daily(x):return np.bincount(codes,weights=x)/counts
            for h,horizon in enumerate((1,3)):
                base=dict(market=market,scope=scope,horizon=horizon,n_pairs=int(mask.sum()),n_sessions=len(dates))
                correct={};actual=cls[mask,h]
                for name,(p,m) in outputs.items():
                    assert p.shape==(len(indices),2,3) and m.shape==(len(indices),2)
                    assert np.isfinite(p).all() and np.isfinite(m).all()
                    np.testing.assert_allclose(p.sum(-1),1,atol=1e-5)
                    pred=p[mask,h].argmax(-1);acc=daily(actual==pred);correct[name]=acc
                    recalls=[float((pred[actual==c]==c).mean()) for c in range(3)]
                    row=base|dict(model=name,session_accuracy=float(acc.mean()),pair_accuracy=float((actual==pred).mean()),
                        balanced_accuracy=float(np.mean(recalls)),session_magnitude_mae=float(daily(abs(m[mask,h]-mag[mask,h])).mean()))
                    for c in range(3):
                        row[f'recall_{c}']=recalls[c];row[f'pred_share_{c}']=float((pred==c).mean());row[f'actual_share_{c}']=float((actual==c).mean())
                    rows.append(row)
                for candidate in MODES[:2]:
                    for baseline in BASELINES:
                        diff=correct[candidate]-correct[baseline];delta=float(diff.mean())
                        for lag in (5,20):
                            se=float(hac_mean_se(diff,lag))
                            p=float(2*norm.sf(abs(delta/se))) if se>0 else float(delta==0)
                            contrasts.append(base|dict(candidate=candidate,baseline=baseline,lag=lag,delta=delta,se=se,p=p,
                                ci95_unadjusted_low=delta-1.96*se,ci95_unadjusted_high=delta+1.96*se))
    report=pd.DataFrame(rows);comp=pd.DataFrame(contrasts)
    for lag in (5,20):
        mask=comp.lag.eq(lag);assert mask.sum()==168
        comp.loc[mask,'p_holm_168']=holm(comp.loc[mask,'p'].to_numpy())
    report.to_csv(out/'validation_metrics.csv',index=False);comp.to_csv(out/'validation_comparisons.csv',index=False)
    macro=report.groupby(['scope','model'])[['session_accuracy','session_magnitude_mae','balanced_accuracy']].mean()
    macro.to_csv(out/'validation_macro.csv')
    verdict={}
    for candidate in MODES[:2]:
        gain={b:float(macro.loc[('direct',candidate),'session_accuracy']-macro.loc[('direct',b),'session_accuracy']) for b in BASELINES}
        indirect=float(macro.loc[('indirect',candidate),'session_accuracy']-macro.loc[('indirect','original'),'session_accuracy'])
        mae=float(macro.loc[('all',candidate),'session_magnitude_mae']-macro.loc[('all','original'),'session_magnitude_mae'])
        verdict[candidate]=dict(direct_macro_gains=gain,indirect_macro_delta=indirect,all_macro_mae_delta=mae,
            descriptive_acceptance=all(x>0 for x in gain.values()) and indirect>=0 and mae<=0)
    # Reproduce prior evaluator's baseline rows to check labels, order and aggregation.
    previous=pd.read_csv(out.parent/'news_context_experiment_20260915/validation_metrics.csv')
    errors=[]
    for name,old_name in (('original','original'),('old_no_content','no_content'),('previous_context','context')):
        current=report[report.model.eq(name)].set_index(['market','scope','horizon']).sort_index()
        prior=previous[previous.model.eq(old_name)].set_index(['market','scope','horizon']).sort_index()
        for metric in ('session_accuracy','pair_accuracy','balanced_accuracy','session_magnitude_mae'):
            error=float(abs(current[metric]-prior[metric]).max());assert error<1e-7,(name,metric,error);errors.append(error)
    return dict(validation_only=True,candidates=verdict,baseline_reproduction_max_error=max(errors),automatic_model_replacement=False)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,required=True);args=ap.parse_args()
    source=HERE/'artifacts/news_impact_v2';evidence=HERE/'artifacts/news_impact_v2_evidence'
    context=HERE/'artifacts/news_context_experiment_20260915';out=args.out.resolve()
    for p in (source,evidence,context):
        if out==p or p in out.parents or out in p.parents:raise ValueError('Use a new sibling output directory')
    out.mkdir(parents=True,exist_ok=False);start=time.monotonic();torch.set_num_threads(4)
    if not torch.cuda.is_available():raise RuntimeError('CUDA required')
    old=json.loads((evidence/'protocol.json').read_text(encoding='utf8'))
    for name,digest in old['source_hashes'].items():assert sha(source/name)==digest,name
    for name in ('articles.jsonl','pairs.parquet','graphs.json','companies.json'):
        assert sha(source/name)==sha(context/'training_inputs'/name),name
    files=[p for p in source.iterdir() if p.is_file()]
    files += [context/'training_inputs'/n for n in ('embeddings.npy','sentiment.npy','text_projection.npz')]
    paths={'original':source/'predictions_stage3.npz','old_no_content':evidence/'no_content/predictions.npz',
        'majority':source/'predictions_majority.npz','stage2':source/'predictions_stage2.npz',
        'market_prior':evidence/'predictions_market_prior.npz','ticker_prior':evidence/'predictions_ticker_prior.npz',
        'previous_context':context/'predictions.npz'}
    files+=list(paths.values());hashes={str(p):sha(p) for p in files}
    dump(out/'protocol.json',dict(source_hashes=hashes,modes=MODES,seeds=[0,1,2],epochs=30,patience=6,batch_size=24,
        hidden=64,dropout=.15,lr=.0008,weight_decay=.001,pca=64,context=str(context/'training_inputs'),
        loss='CE+Huber; mean horizons; equal direct/indirect group means per article; one group fallback 100%',
        checkpoint='minimum balanced validation loss; not comparable with old loss values',
        validation_only=True,test_forward=False,baselines=BASELINES,
        inference='HAC lag5 and20, 168 contrasts per lag Holm family, unadjusted CIs; reused validation research only',
        acceptance='Direct 4-cell macro accuracy above all 7 comparators, indirect accuracy and all MAE no worse than original; no automatic promotion',
        code_hashes={p.name:sha(p) for p in (Path(__file__),HERE/'train_balanced_news_gat.py',HERE/'train_news_impact.py',HERE/'news_impact_model.py')}))
    snapshot=out/'training_inputs';snapshot.mkdir()
    for name in COPIES:(snapshot/name).write_bytes((source/name).read_bytes())
    data,ctx_text,ctx_sent=load_inputs(snapshot,context/'training_inputs')
    with np.load(source/'text_projection.npz') as a,np.load(snapshot/'text_projection.npz') as b:
        for key in a.files:np.testing.assert_allclose(a[key],b[key],atol=1e-7)
    with np.load(source/'label_config.npz') as saved:
        np.testing.assert_array_equal(data.thresholds,saved['thresholds']);np.testing.assert_array_equal(data.scales,saved['scales'])
    np.savez(snapshot/'label_config.npz',thresholds=data.thresholds,scales=data.scales)
    # Exact projected arrays used in training allow independent checkpoint reload.
    np.save(snapshot/'original_text.npy',data.text);np.save(snapshot/'context_text.npy',ctx_text)
    np.save(snapshot/'context_sentiment.npy',ctx_sent)
    assert np.array_equal(data.mentions[data.article_idx,data.node_idx,0]>0,data.pairs.is_direct.to_numpy())
    outputs={};selections={}
    for mode in MODES:
        current=copy.copy(data);folder=out/mode;folder.mkdir()
        if mode=='balanced_context':current.text,current.sentiment=ctx_text,ctx_sent
        elif mode=='balanced_no_content':current.text=np.zeros_like(data.text);current.sentiment=np.zeros_like(data.sentiment)
        dump(folder/'MODEL_POLICY.json',dict(research_only=True,mode=mode,validation_only=True,source_snapshot=str(snapshot)))
        outputs[mode],selections[mode]=fit_balanced(current,folder)
        dump(out/'selections.json',selections)
    for name,path in paths.items():
        with np.load(path) as saved:outputs[name]=(saved['probabilities'][data.val],saved['magnitude'][data.val])
    verdict=evaluate(data,outputs,out)
    for path,digest in hashes.items():assert sha(Path(path))==digest,path
    dump(out/'completion.json',verdict|dict(seconds=time.monotonic()-start,source_unchanged=True))
    print(json.dumps(verdict,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
