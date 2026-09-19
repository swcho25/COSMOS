"""Three fixed 3-seed GAT arms on matched provisional grounded-news examples.

Same 2-layer TargetNewsGAT, 30 epochs, patience 6, article-weighted CE + Huber,
AdamW and validation-only checkpoint selection as train_target_news.fit_target.
This runner stages labels/mentions per batch to fit a 6 GB GPU and saves an
atomic epoch checkpoint for restart. Interrupted epochs restart from the last
completed epoch, with optimizer and all random states restored.

No-news zeros text/polarity but retains target membership/mention metadata.
No-new-graph retains the frozen annual baseline graph, not an edgeless graph.
Primary labels are direct candidate targets only; indirect price prediction is
not evaluated. Test and legacy validation predictions are produced only after
all nine fits finish. Previously exposed periods remain exploratory.
"""
from __future__ import annotations
import argparse
import gc
import json
import math
import os
from pathlib import Path
import time

import numpy as np
import pandas as pd
import torch
from scipy.stats import norm, spearmanr
from sklearn.metrics import f1_score, confusion_matrix

from prepare_grounded_training import ARMS, HERE, dump, load_data, replace_retry, require, sha, verify_prepared
from derive_grounded_training import verify_dataset
from target_news_model import TargetNewsGAT
from train_news_impact import batches, loss_fn
from audit_news_impact import hac_mean_se
from verify_news_predictive_value import holm, priors

SETTINGS = dict(epochs=30, patience=6, min_delta=1e-5, batch_size=24, lr=.0008,
                weight_decay=.001, gradient_clip=2., hidden=64, dropout=.15)
CODE_FILES = ('train_grounded_news.py', 'prepare_grounded_training.py', 'target_news_model.py',
              'news_impact_model.py', 'train_news_impact.py', 'news_impact_data.py',
              'audit_news_impact.py', 'verify_news_predictive_value.py', 'encode_grounded_features.py', 'derive_grounded_training.py')


def save_torch(path, value):
    path = Path(path); temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as stream:
        torch.save(value, stream); stream.flush(); os.fsync(stream.fileno())
    replace_retry(temporary, path)


def save_predictions(path, indices, probabilities, magnitude, covered):
    require(probabilities.shape == (len(indices),2,3) and magnitude.shape == (len(indices),2), 'Prediction dimensions differ')
    require(np.isfinite(probabilities).all() and np.isfinite(magnitude).all(), 'Non-finite predictions')
    require(np.allclose(probabilities[covered].sum(-1),1,atol=1e-6), 'Unnormalized predictions')
    require(not probabilities[~covered].any() and not magnitude[~covered].any(), 'Uncovered rows must remain explicit missing')
    path = Path(path); temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as stream:
        np.savez_compressed(stream,pair_indices=indices,probabilities=probabilities,magnitude=magnitude,covered=covered)
        stream.flush(); os.fsync(stream.fileno())
    replace_retry(temporary,path)


def register_protocol(dataset, out):
    dataset, out = Path(dataset).resolve(), Path(out).resolve(); out.mkdir(parents=True,exist_ok=True)
    prepared = verify_dataset(dataset)
    encoded = json.loads((dataset/'encoded.json').read_text()); graphs = json.loads((dataset/'graphs_receipt.json').read_text())
    require(encoded.get('complete') is True and graphs.get('complete') is True, 'Features and graphs must be complete')
    for name,digest in encoded['files'].items(): require(sha(dataset/name)==digest, 'Encoded data changed: '+name)
    require(sha(dataset/'graphs_candidate.json')==graphs['candidate_sha256'] and sha(dataset/'graphs_base.json')==graphs['base_sha256'], 'Graph snapshot changed')
    require(sha(dataset/'encoding_protocol.json')==encoded['encoding_protocol_sha256'], 'Encoding protocol changed')
    encoding = json.loads((dataset/'encoding_protocol.json').read_text())
    require(encoding['code_sha256']==sha(HERE/'encode_grounded_features.py') and encoding['preparation_code_sha256']==sha(HERE/'prepare_grounded_training.py'), 'Encoding code changed')
    binding = {'schema':'grounded-gnn-fixed-comparison-v1','dataset':str(dataset),
               'source_hashes':{name:sha(dataset/name) for name in tuple(prepared['files'])+('prepared.json','encoded.json','encoding_protocol.json','graphs_receipt.json','graphs_candidate.json','target_text.npy','target_sentiment.npy')},
               'code_hashes':{name:sha(HERE/name) for name in CODE_FILES}, 'settings':SETTINGS,'arms':list(ARMS),'seeds':[0,1,2],
               'checkpoint_selection':'primary_2023_val_composite_loss_only; minimum improvement1e-5',
               'no_news':'zero_text_and_sentiment; preserve_candidate_membership_mentions_graph_labels',
               'no_new_graph':'frozen_baseline_annual_graphs_without_added_temporal_typed_relations',
               'direct_supervision_only':True,'indirect_prediction_validated':False,
               'validation_previously_exposed':True,'test_previously_exposed':True,
               'test_selection_used':False,'test_prediction_after_all_nine_fits':True,
               'legacy_val':'secondary_original_fixed_population; missing70features countedwrong; preprocessingdiffers',
               'baselines':['majority','market_prior','ticker_prior'],
               'comparisons':['candidate_vs_no_news','candidate_vs_no_new_graph','candidate_vs_majority','candidate_vs_market_prior','candidate_vs_ticker_prior'],
               'hac_lags':[5,20], 'holm_family':'all three evaluation splits x two markets x two horizons x five baselines; separate lag families',
               'confidence_intervals':'unadjusted approximate normal HAC95%; only descriptive exploratory evidence',
               'production_approved':False,'automatic_model_replacement':False,'guaranteed_target_accuracy':None}
    if (out/'training_protocol.json').exists(): require(json.loads((out/'training_protocol.json').read_text())==binding,'Training protocol/inputs changed')
    else: dump(out/'training_protocol.json',binding)
    return binding


class Runtime:
    def __init__(self,data,dataset,arm,device='cuda'):
        self.data,self.device = data,device
        self.mapping=np.load(Path(dataset)/'feature_mapping.npy',mmap_mode='r')
        text=np.load(Path(dataset)/'target_text.npy'); sent=np.load(Path(dataset)/'target_sentiment.npy')
        if arm=='no_news': text=np.zeros_like(text);sent=np.zeros_like(sent)
        self.text=torch.as_tensor(text,device=device); self.sent=torch.as_tensor(sent,device=device)
        self.static={k:torch.as_tensor(v,device=device) for k,v in data.static.items()}
        self.edges={k:(torch.as_tensor(e,device=device),torch.as_tensor(a,device=device)) for k,(e,a) in data.graph_arrays.items()}
        self.config=dict(n_companies=len(data.tickers),static_dim=next(iter(data.static.values())).shape[-1],text_dim=text.shape[-1],edge_dim=11,hidden=64,dropout=.15)

    def forward(self,model,key,ids):
        fi=torch.as_tensor(np.asarray(self.mapping[ids]),device=self.device)
        mentions=torch.as_tensor(self.data.mentions[ids],device=self.device)
        return model(self.text[fi],self.sent[fi],mentions,self.static[key],*self.edges[key])

    def epoch(self,model,split,settings,optimizer=None,rng=None,stop_file=None):
        model.train(optimizer is not None);total=0.;count=0
        for key,ids in batches(self.data,split,settings['batch_size'],rng):
            if stop_file is not None and stop_file.exists(): raise InterruptedError('STOP requested; current uncommitted epoch is discarded')
            with torch.set_grad_enabled(optimizer is not None):
                logits,pred=self.forward(model,key,ids)
                cls,mag,mask=(torch.as_tensor(array[ids],device=self.device) for array in (self.data.dense_cls,self.data.dense_mag,self.data.mask))
                loss=loss_fn(logits,pred,cls,mag,mask)
                require(bool(torch.isfinite(loss)), 'Nonfinite training loss')
                if optimizer is not None:
                    optimizer.zero_grad(set_to_none=True);loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(),settings['gradient_clip']);optimizer.step()
            total+=float(loss.detach().cpu())*len(ids);count+=len(ids)
        require(count>0,'Empty '+split+' article population')
        return total/count


def fit_seed(runtime,folder,seed,protocol_sha,*,settings=None,stop_file=None):
    settings=dict(SETTINGS if settings is None else settings);folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    done_path=folder/'fit_complete.json'
    if done_path.exists():
        done=json.loads(done_path.read_text());require(done['training_protocol_sha256']==protocol_sha and done['seed']==seed,'Fit identity changed')
        require(sha(folder/'model.pt')==done['model_sha256'],'Completed model changed');return done
    torch.manual_seed(seed);np.random.seed(seed)
    if str(runtime.device).startswith('cuda'):torch.cuda.manual_seed_all(seed)
    model=TargetNewsGAT(**runtime.config).to(runtime.device)
    optimizer=torch.optim.AdamW(model.parameters(),lr=settings['lr'],weight_decay=settings['weight_decay'])
    rng=np.random.default_rng(seed);best=float('inf');best_epoch=-1;best_state=None;history=[];start_epoch=0
    state_path=folder/'epoch_state.json'
    if state_path.exists():
        marker=json.loads(state_path.read_text());require(marker['training_protocol_sha256']==protocol_sha,'Epoch protocol changed')
        checkpoint=folder/marker['file'];require(checkpoint.name==marker['file'] and sha(checkpoint)==marker['sha256'],'Epoch checkpoint changed')
        state=torch.load(checkpoint,map_location=runtime.device,weights_only=False)
        require(state['seed']==seed and state['settings']==settings,'Epoch run identity differs')
        model.load_state_dict(state['state_dict']);optimizer.load_state_dict(state['optimizer'])
        rng.bit_generator.state=state['rng'];np.random.set_state(state['numpy_rng']);torch.set_rng_state(state['torch_rng'].cpu())
        if str(runtime.device).startswith('cuda'):torch.cuda.set_rng_state_all([x.cpu() for x in state['cuda_rng']])
        best,best_epoch,best_state,history,start_epoch=state['best'],state['best_epoch'],state['best_state'],state['history'],state['epoch']+1
    for epoch in range(start_epoch,settings['epochs']):
        if epoch-best_epoch>settings['patience'] and best_epoch>=0:break
        started=time.monotonic()
        tr=runtime.epoch(model,'train',settings,optimizer,rng,stop_file)
        val=runtime.epoch(model,'val',settings,stop_file=stop_file)
        if val<best-settings['min_delta']:
            best,best_epoch=val,epoch;best_state={k:v.detach().cpu().clone() for k,v in model.state_dict().items()}
        row={'seed':seed,'epoch':epoch,'train_loss':tr,'val_loss':val,'seconds':time.monotonic()-started};history.append(row)
        checkpoint=folder/f'epoch_{epoch:02d}.pt'
        save_torch(checkpoint,dict(seed=seed,settings=settings,epoch=epoch,state_dict=model.state_dict(),optimizer=optimizer.state_dict(),
                                  rng=rng.bit_generator.state,numpy_rng=np.random.get_state(),torch_rng=torch.get_rng_state(),
                                  cuda_rng=torch.cuda.get_rng_state_all() if str(runtime.device).startswith('cuda') else [],
                                  best=best,best_epoch=best_epoch,best_state=best_state,history=history))
        dump(state_path,{'file':checkpoint.name,'sha256':sha(checkpoint),'training_protocol_sha256':protocol_sha,'epoch':epoch})
        dump(folder/'history.json',history);print(json.dumps(dict(arm=folder.parent.name,**row)),flush=True)
        if epoch-best_epoch>=settings['patience']:break
    require(best_state is not None,'No completed epoch')
    save_torch(folder/'model.pt',dict(config=runtime.config,state_dict=best_state,selection={'seed':seed,'best_epoch':best_epoch,'val_loss':best},
                                    mode=folder.parent.name,loss='direct_only',training_protocol_sha256=protocol_sha))
    done={'seed':seed,'best_epoch':best_epoch,'val_loss':best,'completed_epochs':len(history),'model_sha256':sha(folder/'model.pt'),
          'training_protocol_sha256':protocol_sha,'test_evaluated':False}
    dump(done_path,done);return done


def predict(runtime,model,split,batch_size=24):
    data=runtime.data;indices=np.flatnonzero(data.pairs.split.eq(split).to_numpy());positions={}
    for j,index in enumerate(indices):positions.setdefault(int(data.article_idx[index]),[]).append(j)
    covered=data.mask[data.article_idx[indices],data.node_idx[indices]]
    probability=np.zeros((len(indices),2,3),dtype='float32');magnitude=np.zeros((len(indices),2),dtype='float32')
    model.eval()
    with torch.inference_mode():
        for key,ids in batches(data,split,batch_size):
            logits,mag=runtime.forward(model,key,ids);p=logits.softmax(-1).cpu().numpy();m=mag.cpu().numpy()
            for j,article in enumerate(ids):
                dest=np.asarray(positions.get(int(article),[]),dtype='int64');dest=dest[covered[dest]]
                if len(dest):
                    node=data.node_idx[indices[dest]];probability[dest]=p[j,node];magnitude[dest]=m[j,node]
    return indices,probability,magnitude,covered


def baseline_outputs(data,indices):
    output=priors(data.pairs,data.cls,data.mag)
    train=data.pairs.split.eq('train').to_numpy();require(train.any(),'Missing baseline train rows')
    majority=np.stack([np.bincount(data.cls[train,h],minlength=3).argmax() for h in range(2)])
    probability=np.broadcast_to(np.eye(3,dtype='float32')[majority],(len(indices),2,3)).copy()
    magnitude=np.broadcast_to(np.median(data.mag[train],axis=0),(len(indices),2)).copy()
    result={name:(p[indices],m[indices],np.ones(len(indices),dtype=bool)) for name,(p,m) in output.items()}
    result['majority']=(probability,magnitude,np.ones(len(indices),dtype=bool));return result


def evaluate_metrics(data,indices,outputs,split):
    frame=data.pairs.iloc[indices];actual=data.cls[indices];target_mag=data.mag[indices]
    metrics=[];daily={};comparisons=[]
    for market in ('KOSPI','NASDAQ'):
        selected=frame.market.eq(market).to_numpy()
        if not selected.any():continue
        for h in range(2):
            for name,(prob,mag,covered) in outputs.items():
                pred=prob[:,h].argmax(-1);pred=pred.copy();pred[~covered]=-1
                correct=(pred==actual[:,h]).astype(float)
                sessions=pd.Series(correct[selected],index=frame.loc[selected,'anchor_date']).groupby(level=0).mean().sort_index()
                daily[name,market,h]=sessions
                valid=selected & covered
                rank=[]
                for _,local in frame.loc[valid].groupby('news_id').indices.items():
                    positions=np.flatnonzero(valid)[local]
                    if len(positions)<2:continue
                    a,b=target_mag[positions,h],mag[positions,h]
                    if np.std(a)==0:continue
                    rank.append(0. if np.std(b)==0 else float(spearmanr(a,b).statistic))
                metrics.append(dict(split=split,model=name,market=market,horizon=(1,3)[h],pairs=int(selected.sum()),
                    covered_pairs=int(valid.sum()),abstentions=int((selected & ~covered).sum()),sessions=len(sessions),
                    pair_accuracy=float(correct[selected].mean()),session_accuracy=float(sessions.mean()),
                    macro_f1=float(f1_score(actual[selected,h],pred[selected],labels=[0,1,2],average='macro',zero_division=0)),
                    confusion_true_rows_pred_columns=[[int(np.sum((actual[selected,h]==truth)&(pred[selected]==guess))) for guess in (0,1,2,-1)] for truth in (0,1,2)],
                    confusion_columns='below_market_threshold,within_neutral_band,above_market_threshold,abstain',
                    magnitude_mae_covered=float(np.abs(mag[valid,h]-target_mag[valid,h]).mean()) if valid.any() else None,
                    within_article_magnitude_spearman=float(np.mean(rank)) if rank else None,rank_articles=len(rank)))
            for baseline in ('no_news','no_new_graph','majority','market_prior','ticker_prior'):
                difference=(daily['candidate',market,h]-daily[baseline,market,h]).to_numpy()
                for lag in (5,20):
                    mean=float(np.mean(difference));se=hac_mean_se(difference,lag)
                    p=2*norm.sf(abs(mean/se)) if se is not None and se>0 else (1. if mean==0 or se is None else 0.)
                    comparisons.append(dict(split=split,market=market,horizon=(1,3)[h],baseline=baseline,lag=lag,sessions=len(difference),
                                            accuracy_delta=mean,standard_error=float(se) if se is not None else None,
                                            ci95_low=float(mean-1.96*se) if se is not None else None,
                                            ci95_high=float(mean+1.96*se) if se is not None else None,p=float(p)))
    return metrics,comparisons


def evaluate(dataset,out,device='cuda'):
    protocol=register_protocol(dataset,out);out=Path(out);protocol_sha=sha(out/'training_protocol.json')
    require(not (out/'evaluation_complete.json').exists(),'Evaluation already completed; do not tune and repeat this registered comparison')
    for arm in ARMS:
        for seed in range(3):
            folder=out/'models'/arm/f'seed{seed}';done=json.loads((folder/'fit_complete.json').read_text())
            require(done['training_protocol_sha256']==protocol_sha and sha(folder/'model.pt')==done['model_sha256'],'All nine immutable fits must complete before evaluation')
    predictions={split:{} for split in ('val','test','legacy_val')};data=None
    for arm in ARMS:
        data=load_data(dataset,arm);runtime=Runtime(data,dataset,arm,device);saved={split:[] for split in predictions}
        for seed in range(3):
            folder=out/'models'/arm/f'seed{seed}';checkpoint=torch.load(folder/'model.pt',map_location=device,weights_only=False)
            model=TargetNewsGAT(**checkpoint['config']).to(device);model.load_state_dict(checkpoint['state_dict'])
            for split in predictions:
                indices,p,m,covered=predict(runtime,model,split);saved[split].append((p,m))
                save_predictions(folder/f'{split}_predictions.npz',indices,p,m,covered)
            del model
        for split in predictions:
            indices=np.flatnonzero(data.pairs.split.eq(split).to_numpy());covered=data.mask[data.article_idx[indices],data.node_idx[indices]]
            p=np.mean([x[0] for x in saved[split]],axis=0);m=np.mean([x[1] for x in saved[split]],axis=0)
            predictions[split][arm]=(p,m,covered);save_predictions(out/'models'/arm/f'{split}_ensemble.npz',indices,p,m,covered)
        del runtime;gc.collect()
        if str(device).startswith('cuda'):torch.cuda.empty_cache()
    metrics=[];contrasts=[]
    for split,outputs in predictions.items():
        indices=np.flatnonzero(data.pairs.split.eq(split).to_numpy());outputs.update(baseline_outputs(data,indices))
        records,tests=evaluate_metrics(data,indices,outputs,split);metrics.extend(records);contrasts.extend(tests)
    for lag in (5,20):
        group=[row for row in contrasts if row['lag']==lag]
        for row,adjusted in zip(group,holm([row['p'] for row in group])):row['holm_p']=float(adjusted)
    pd.DataFrame(metrics).to_csv(out/'metrics.csv',index=False);pd.DataFrame(contrasts).to_csv(out/'paired_hac.csv',index=False)
    artifacts=[p for p in out.rglob('*') if p.is_file() and (p.suffix=='.npz' or p.name in ('metrics.csv','paired_hac.csv','model.pt','fit_complete.json'))]
    receipt={'complete':True,'training_protocol_sha256':protocol_sha,'fits':9,'seeds_per_arm':3,
             'files':{str(p.relative_to(out)):sha(p) for p in artifacts},'indirect_labels':0,'direct_candidate_labels_only':True,
             'test_previously_exposed':True,'production_approved':False,'automatic_model_replacement':False,
             'uncertainty':'approximate paired-trading-day HAC; joint Holm family; no fresh-holdout or causal claims'}
    dump(out/'evaluation_complete.json',receipt);return receipt


def train(dataset,out,device='cuda'):
    torch.set_num_threads(4);out=Path(out);register_protocol(dataset,out);protocol_sha=sha(out/'training_protocol.json')
    try:
        for arm in ARMS:
            data=load_data(dataset,arm);runtime=Runtime(data,dataset,arm,device)
            for seed in range(3):
                if (out/'STOP').exists():return {'status':'paused_stop_file','arm':arm,'seed':seed}
                fit_seed(runtime,out/'models'/arm/f'seed{seed}',seed,protocol_sha,stop_file=out/'STOP')
            del runtime,data;gc.collect()
            if str(device).startswith('cuda'):torch.cuda.empty_cache()
    except InterruptedError as error:
        status={'status':'paused_stop_file','arm':arm,'seed':seed,'detail':str(error)};dump(out/'status.json',status);return status
    status={'status':'nine_fits_complete','training_protocol_sha256':protocol_sha};dump(out/'status.json',status);return status


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--dataset',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--device',default='cuda');parser.add_argument('--phase',choices=('train','evaluate','all'),default='all');args=parser.parse_args()
    result=train(args.dataset,args.out,args.device) if args.phase in ('train','all') else None
    if args.phase=='evaluate' or (args.phase=='all' and result['status']=='nine_fits_complete'):result=evaluate(args.dataset,args.out,args.device)
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
