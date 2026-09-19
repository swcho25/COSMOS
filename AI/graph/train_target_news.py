"""Direct-label-only GAT research trainer, three seeds and val-only predictions.

All graph nodes remain for message passing, but indirect/unverified labels receive
zero loss and no indirect performance claim. One eligible article = one loss unit.
Architecture, optimizer, seeds and early stopping match the existing GAT defaults.
Sparse feature IDs avoid keeping a dense article x company x embedding GPU tensor.
"""
import json
import time
import numpy as np
import pandas as pd
import torch
from train_news_impact import batches,loss_fn
from target_news_model import TargetNewsGAT


def fit_target(data,mapping,text_features,sent_features,out,indices):
    ts=lambda x:torch.as_tensor(x,device='cuda')
    mapping,text,sent,mentions=map(ts,(mapping,text_features,sent_features,data.mentions))
    static={k:ts(v) for k,v in data.static.items()};edges={k:(ts(e),ts(a)) for k,(e,a) in data.graph_arrays.items()}
    cls,mag,mask=map(ts,(data.dense_cls,data.dense_mag,data.mask))
    config=dict(n_companies=len(data.tickers),static_dim=next(iter(data.static.values())).shape[-1],text_dim=text_features.shape[-1],edge_dim=11)
    history=[];selections=[];ps=[];ms=[]
    article_rows={}
    for j,idx in enumerate(indices):article_rows.setdefault(int(data.article_idx[idx]),[]).append(j)
    def forward(model,key,ids):
        ii=ts(ids);fi=mapping[ii]
        return model(text[fi],sent[fi],mentions[ii],static[key],*edges[key])
    def run(model,split,optimizer=None,rng=None):
        model.train(optimizer is not None);total=0.;count=0
        for key,ids in batches(data,split,24,rng):
            with torch.set_grad_enabled(optimizer is not None):
                logits,pred=forward(model,key,ids);ii=ts(ids)
                loss=loss_fn(logits,pred,cls[ii],mag[ii],mask[ii])
                if optimizer is not None:
                    optimizer.zero_grad(set_to_none=True);loss.backward();torch.nn.utils.clip_grad_norm_(model.parameters(),2.);optimizer.step()
            total+=float(loss.detach().cpu())*len(ids);count+=len(ids)
        return total/count
    for seed in range(3):
        torch.manual_seed(seed);np.random.seed(seed);torch.cuda.manual_seed_all(seed)
        model=TargetNewsGAT(**config).cuda();optimizer=torch.optim.AdamW(model.parameters(),lr=8e-4,weight_decay=1e-3)
        rng=np.random.default_rng(seed);best=float('inf');best_epoch=-1;state=None
        for epoch in range(30):
            start=time.monotonic();tr=run(model,'train',optimizer,rng);val=run(model,'val')
            row=dict(seed=seed,epoch=epoch,train_loss=tr,val_loss=val,seconds=time.monotonic()-start);history.append(row)
            print(json.dumps(dict(mode=out.name,**row)),flush=True)
            if val<best-1e-5:best,best_epoch=val,epoch;state={k:v.detach().cpu().clone() for k,v in model.state_dict().items()}
            elif epoch-best_epoch>=6:break
        model.load_state_dict(state);model.eval();selection=dict(seed=seed,best_epoch=best_epoch,val_loss=best);selections.append(selection)
        torch.save(dict(config=config,state_dict=state,selection=selection,mode=out.name,loss='direct_only'),out/f'gat_seed{seed}.pt')
        p=np.zeros((len(indices),2,3),dtype='float32');m=np.zeros((len(indices),2),dtype='float32')
        with torch.inference_mode():
            for key,ids in batches(data,'val',24):
                logits,pred=forward(model,key,ids);pp=logits.softmax(-1).cpu().numpy();mm=pred.cpu().numpy()
                for j,i in enumerate(ids):
                    rows=np.asarray(article_rows.get(int(i),[]),dtype=int)
                    if len(rows):
                        ni=data.node_idx[indices[rows]];p[rows]=pp[j,ni];m[rows]=mm[j,ni]
        np.testing.assert_allclose(p.sum(-1),1,atol=1e-6);ps.append(p);ms.append(m)
        np.savez_compressed(out/f'validation_seed{seed}.npz',pair_indices=indices,probabilities=p,magnitude=m)
        pd.DataFrame(history).to_csv(out/'training_history.csv',index=False)
    p=np.mean(ps,axis=0);m=np.mean(ms,axis=0)
    np.savez_compressed(out/'validation_predictions.npz',pair_indices=indices,probabilities=p,magnitude=m)
    return (p,m),selections
