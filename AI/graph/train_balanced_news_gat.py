"""Research trainer: equal direct/indirect group loss within each article.

Architecture/optimizer/batches match train_news_impact.fit_gat. Only the per-article
reduction changes: 0.5*mean(direct)+0.5*mean(indirect), averaging CE+Huber over both
horizons first. If one group has no eligible labels, the other receives weight 1.
Validation checkpoint selection uses this same balanced objective. Predict ONLY
validation rows, with original pair indices saved, so no new test results exist.
The original trainer and its protocol hashes remain untouched.
"""
import json
import time
import numpy as np
import pandas as pd
import torch
from torch.nn import functional as F
from train_news_impact import batches
from news_impact_model import NewsGAT


def group_mean_loss(per_node,mask,direct):
    direct_mask=mask & direct.bool()
    indirect_mask=mask & ~direct.bool()
    nd=direct_mask.sum(-1);ni=indirect_mask.sum(-1)
    ld=(per_node*direct_mask).sum(-1)/nd.clamp(min=1)
    li=(per_node*indirect_mask).sum(-1)/ni.clamp(min=1)
    present=(nd>0).to(per_node.dtype)+(ni>0).to(per_node.dtype)
    return ((ld+li)/present.clamp(min=1)).mean()


def balanced_loss(logits,pred_mag,cls,mag,mask,direct):
    ce=F.cross_entropy(logits.reshape(-1,3),cls.reshape(-1),reduction="none").reshape(*cls.shape)
    err=F.smooth_l1_loss(pred_mag,mag,reduction="none")
    return group_mean_loss((ce+err).mean(-1),mask,direct)


def fit_balanced(data,out,seeds=3,epochs=30,patience=6,batch_size=24):
    dev=torch.device("cuda");ts=lambda a:torch.as_tensor(a,device=dev)
    text,sentiment,mentions=ts(data.text),ts(data.sentiment),ts(data.mentions)
    static={k:ts(v) for k,v in data.static.items()}
    edges={k:(ts(e),ts(a)) for k,(e,a) in data.graph_arrays.items()}
    cls,mag,mask=ts(data.dense_cls),ts(data.dense_mag),ts(data.mask)
    config=dict(n_companies=len(data.tickers),static_dim=next(iter(data.static.values())).shape[-1],text_dim=data.text.shape[-1],edge_dim=11)
    selected=np.flatnonzero(data.pairs.split.eq("val").to_numpy())
    va,vn=data.article_idx[selected],data.node_idx[selected]
    history=[];selections=[];ensemble_p=[];ensemble_m=[]
    def run(model,split,optimizer=None,rng=None):
        model.train(optimizer is not None);total=0.;n=0
        for key,ids in batches(data,split,batch_size,rng):
            ii=ts(ids)
            with torch.set_grad_enabled(optimizer is not None):
                logits,pred=model(text[ii],sentiment[ii],mentions[ii],static[key],*edges[key])
                loss=balanced_loss(logits,pred,cls[ii],mag[ii],mask[ii],mentions[ii,:,0]>0)
                if optimizer is not None:
                    optimizer.zero_grad(set_to_none=True);loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(),2.);optimizer.step()
            total+=float(loss.detach().cpu())*len(ids);n+=len(ids)
        return total/n
    for seed in range(seeds):
        torch.manual_seed(seed);np.random.seed(seed);torch.cuda.manual_seed_all(seed)
        model=NewsGAT(**config).to(dev)
        optimizer=torch.optim.AdamW(model.parameters(),lr=8e-4,weight_decay=1e-3)
        rng=np.random.default_rng(seed);best=float("inf");best_epoch=-1;state=None
        for epoch in range(epochs):
            start=time.monotonic();tr=run(model,"train",optimizer,rng);val=run(model,"val")
            row={"seed":seed,"epoch":epoch,"train_loss":tr,"val_loss":val,"seconds":time.monotonic()-start}
            history.append(row);print(json.dumps({"mode":out.name,**row}),flush=True)
            if val<best-1e-5:
                best,best_epoch=val,epoch;state={k:v.detach().cpu().clone() for k,v in model.state_dict().items()}
            elif epoch-best_epoch>=patience:break
        model.load_state_dict(state);model.eval()
        selection={"seed":seed,"best_epoch":best_epoch,"val_loss":best};selections.append(selection)
        torch.save({"config":config,"state_dict":state,"selection":selection,"loss_policy":"direct_indirect_equal_groups"},out/f"gat_seed{seed}.pt")
        p=np.zeros((len(data.docs),len(data.tickers),2,3),dtype="float32")
        m=np.zeros((len(data.docs),len(data.tickers),2),dtype="float32")
        with torch.inference_mode():
            for key,ids in batches(data,"val",batch_size):
                ii=ts(ids);logits,pred=model(text[ii],sentiment[ii],mentions[ii],static[key],*edges[key])
                p[ids]=logits.softmax(-1).cpu().numpy();m[ids]=pred.cpu().numpy()
        ensemble_p.append(p[va,vn]);ensemble_m.append(m[va,vn])
        np.savez_compressed(out/f"validation_seed{seed}.npz",pair_indices=selected,probabilities=ensemble_p[-1],magnitude=ensemble_m[-1])
        pd.DataFrame(history).to_csv(out/"training_history.csv",index=False)
    p=np.mean(ensemble_p,axis=0);m=np.mean(ensemble_m,axis=0)
    np.savez_compressed(out/"validation_predictions.npz",pair_indices=selected,probabilities=p,magnitude=m)
    return (p,m),selections
