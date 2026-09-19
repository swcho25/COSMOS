"""One fixed three-seed context-input experiment; validation-only reporting.

Existing article-level structure, labels, candidates, GAT, optimizer, loss, PCA64,
token limits and seeds remain unchanged. Only the text selected for frozen embedding
and sentiment encoders changes. PCA is refit on TRAIN new embeddings, never val/test.
The existing no_content ensemble remains a matched comparator: both old/new text and
sentiment are zeroed, and all remaining inputs/shape/parameters are identical.
fit_gat stores predictions for all splits AFTER val checkpoint selection, but this
experiment's evaluation and conclusions use validation only; no test metrics opened.
Research artifacts require the custom selection policy; do not use the default API.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from sklearn.decomposition import PCA
from scipy.stats import norm

from train_news_impact import TrainingData, fit_gat
from news_text_features import feature_manifest
from news_company_context import ContextEncoder
from news_content import sanitize_article
from audit_news_impact import hac_mean_se
from verify_news_predictive_value import sha, dump, holm, COPIES

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/"ner"))
from matcher import CompanyMatcher


def evaluate_validation(data,outputs,out):
    rows,comparisons=[],[]
    for market in ("KOSPI","NASDAQ"):
        for scope in ("all","direct","indirect"):
            mask=(data.pairs.split.eq("val")&data.pairs.market.eq(market)).to_numpy()
            if scope!="all":mask &= data.pairs.is_direct.to_numpy()==(scope=="direct")
            sub=data.pairs[mask]
            codes,dates=pd.factorize(sub.anchor_date,sort=True); counts=np.bincount(codes)
            def daily(values):return np.bincount(codes,weights=values)/counts
            for h,horizon in enumerate((1,3)):
                base={"market":market,"scope":scope,"horizon":horizon,"n_pairs":len(sub),"n_sessions":len(dates)}
                correct={}
                for name,(p,m) in outputs.items():
                    actual=data.cls[mask,h]; pred=p[mask,h].argmax(-1)
                    accuracy=daily(actual==pred); correct[name]=accuracy
                    recalls=[float((pred[actual==c]==c).mean()) for c in range(3) if (actual==c).any()]
                    rows.append(base|{"model":name,"session_accuracy":float(accuracy.mean()),
                        "pair_accuracy":float((actual==pred).mean()),"balanced_accuracy":float(np.mean(recalls)),
                        "session_magnitude_mae":float(daily(abs(m[mask,h]-data.mag[mask,h])).mean())})
                for name in ("original","no_content"):
                    diff=correct["context"]-correct[name]; delta=float(diff.mean())
                    for lag in (5,20):
                        se=float(hac_mean_se(diff,lag)); p=float(2*norm.sf(abs(delta/se))) if se>0 else float(delta==0)
                        comparisons.append(base|{"baseline":name,"lag":lag,"delta":delta,"se":se,
                            "ci95_unadjusted_low":delta-1.96*se,"ci95_unadjusted_high":delta+1.96*se,"p":p})
    report=pd.DataFrame(rows); comp=pd.DataFrame(comparisons)
    for lag in (5,20):
        mask=comp.lag.eq(lag)
        assert mask.sum()==24
        comp.loc[mask,"p_holm_24"]=holm(comp.loc[mask,"p"].to_numpy())
    report.to_csv(out/"validation_metrics.csv",index=False)
    comp.to_csv(out/"validation_comparisons.csv",index=False)
    macro=report.groupby(["scope","model"])[["session_accuracy","session_magnitude_mae"]].mean()
    macro.to_csv(out/"validation_macro.csv")
    # Prospective criteria for this run: descriptive gain against BOTH comparators on
    # direct accuracy, no aggregate indirect accuracy/strength MAE regression vs original.
    gains={name:float(macro.loc[("direct","context"),"session_accuracy"]-macro.loc[("direct",name),"session_accuracy"])
           for name in ("original","no_content")}
    indirect_delta=float(macro.loc[("indirect","context"),"session_accuracy"]-macro.loc[("indirect","original"),"session_accuracy"])
    mae_delta=float(macro.loc[("all","context"),"session_magnitude_mae"]-macro.loc[("all","original"),"session_magnitude_mae"])
    return {"validation_only":True,"direct_macro_accuracy_gains":gains,
        "indirect_macro_accuracy_delta":indirect_delta,"all_macro_magnitude_mae_delta":mae_delta,
        "descriptive_acceptance":all(d>0 for d in gains.values()) and indirect_delta>=0 and mae_delta<=0,
        "automatic_model_replacement":False}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--source",type=Path,default=HERE/"artifacts/news_impact_v2")
    ap.add_argument("--evidence",type=Path,default=HERE/"artifacts/news_impact_v2_evidence")
    ap.add_argument("--out",type=Path,required=True)
    args=ap.parse_args()
    source,evidence,out=(p.resolve() for p in (args.source,args.evidence,args.out))
    if any(out==p or p in out.parents or out in p.parents for p in (source,evidence)):
        raise ValueError("Use a new sibling research directory")
    out.mkdir(parents=True,exist_ok=False)
    start=time.monotonic(); torch.set_num_threads(4)
    if not torch.cuda.is_available():raise RuntimeError("This fixed run requires the existing CUDA environment")
    old=json.loads((evidence/"protocol.json").read_text(encoding="utf8"))
    for name,digest in old["source_hashes"].items():
        if sha(source/name)!=digest:raise ValueError(f"Original model changed: {name}")
    files=[p for p in source.iterdir() if p.is_file()]+[evidence/"no_content/predictions.npz"]
    hashes={str(p):sha(p) for p in files}
    policy={"research_only":True,"input_policy":"news_company_context.select_context",
        "warning":"Custom input selection required; not a deployment bundle; default API is incompatible"}
    dump(out/"MODEL_POLICY.json",policy)
    dump(out/"protocol.json",{"source_hashes":hashes,"seeds":[0,1,2],"epochs":30,"patience":6,
        "batch_size":24,"hidden":64,"dropout":.15,"lr":.0008,"weight_decay":.001,"pca":64,
        "loss":"unchanged direction CE + magnitude Huber, equal article weighting",
        "change":"mention-centered raw text for embedding and sentiment only",
        "token_limits":{"embedding":256,"sentiment":128},"title_limit":"min(48, token_limit//4)",
        "selection":"fixed once, no validation-driven input policy tuning",
        "comparison":"original and existing matched no_content three-seed ensembles; val only",
        "inference":"HAC lag 5/20; Holm all 24 contrasts per lag, CIs unadjusted",
        "acceptance":"direct 4-cell macro accuracy above both; indirect macro accuracy and all macro MAE no worse than original",
        "prediction_notice":"fit_gat computes all-split predictions after checkpoint selection; test metrics not evaluated",
        "code_hashes":{p.name:sha(p) for p in [Path(__file__),HERE/"news_company_context.py",HERE/"train_news_impact.py",HERE/"news_impact_model.py"]}})
    snapshot=out/"training_inputs";snapshot.mkdir()
    for name in COPIES:(snapshot/name).write_bytes((source/name).read_bytes())
    (snapshot/"aliases.csv").write_bytes((source/"aliases.csv").read_bytes())
    spec=json.loads((snapshot/"pretrained.json").read_text(encoding="utf8"))
    for role in spec:spec[role]["path"]=str((source/spec[role]["path"]).resolve())
    dump(snapshot/"pretrained.json",spec)
    # Load original representations in an isolated copy so all non-text structures
    # are constructed with the exact existing pipeline. Then replace only text features.
    dump(snapshot/"feature_manifest.json",feature_manifest(snapshot))
    data=TrainingData(snapshot)
    docs=[sanitize_article(d) for d in data.docs]
    matcher=CompanyMatcher.from_csv(snapshot/"aliases.csv")
    hits=[]
    for d in docs:
        direct={m["ticker"] for m in d["companies"]}
        hits.append([{"ticker":m.ticker,"start":m.start,"end":m.end}
                     for m in matcher.match(d.get("title"),d.get("body")).mentions
                     if m.ticker in direct and m.confidence>=1.0])
    encoder=ContextEncoder(snapshot,device="cuda")
    coverage=[]
    embedding,audit=encoder.encode_role("embedding",docs,hits);coverage.extend(audit);encoder.release()
    sentiment=np.zeros_like(data.sentiment)
    for role in ("sentiment_ko","sentiment_en"):
        values,audit=encoder.encode_role(role,docs,hits);sentiment+=values;coverage.extend(audit);encoder.release()
    dump(out/"input_coverage.json",coverage)
    coverage_rows=[]
    for role in ("embedding","sentiment_ko","sentiment_en"):
        lookup={r["article"]:r for r in coverage if r["role"]==role}
        for r in data.pairs[data.pairs.is_direct].itertuples():
            if r.article not in lookup:continue
            a=lookup[r.article]
            coverage_rows.append({"role":role,"split":r.split,"market":r.market,
                "covered":r.ticker in a["covered"],"matched":r.ticker in a["matched"]})
    stats=pd.DataFrame(coverage_rows).groupby(["role","split","market"]).agg(
        direct_pairs=("covered","size"),covered=("covered","mean"),matched=("matched","mean"))
    stats.to_csv(out/"input_coverage_summary.csv")
    print("INPUT COVERAGE\n"+stats.to_string(),flush=True)
    article_train=np.array([d["split"]=="train" for d in docs])
    pca=PCA(n_components=data.text.shape[-1],random_state=42)
    pca.fit(embedding[article_train]);data.text=pca.transform(embedding).astype("float32");data.sentiment=sentiment
    np.save(snapshot/"embeddings.npy",embedding);np.save(snapshot/"sentiment.npy",sentiment)
    np.savez(snapshot/"text_projection.npz",mean=pca.mean_,components=pca.components_)
    np.savez(snapshot/"label_config.npz",thresholds=data.thresholds,scales=data.scales)
    # Fail closed if someone tries to load these custom features with ordinary TrainingData.
    dump(snapshot/"feature_manifest.json",feature_manifest(snapshot)|{"custom_input_policy":policy})
    for name in ("pairs.parquet","graphs.json","companies.json","articles.jsonl"):
        assert sha(snapshot/name)==sha(source/name),name
    with np.load(source/"label_config.npz") as original:
        np.testing.assert_array_equal(data.thresholds,original["thresholds"])
        np.testing.assert_array_equal(data.scales,original["scales"])
    print("FEATURES READY; starting fixed 3-seed GAT",flush=True)
    data.out=out
    p,m,selections=fit_gat(data,seeds=3,epochs=30,patience=6,batch_size=24,device="cuda")
    np.savez_compressed(out/"predictions.npz",probabilities=p,magnitude=m)
    dump(out/"selections.json",selections)
    outputs={"context":(p,m)}
    for name,path in (("original",source/"predictions_stage3.npz"),("no_content",evidence/"no_content/predictions.npz")):
        with np.load(path) as saved:outputs[name]=(saved["probabilities"],saved["magnitude"])
    verdict=evaluate_validation(data,outputs,out)
    if any(sha(Path(path))!=digest for path,digest in hashes.items()):raise AssertionError("Original files changed")
    dump(out/"completion.json",verdict|{"seconds":time.monotonic()-start,"source_unchanged":True})
    print(json.dumps(verdict,ensure_ascii=False),flush=True)


if __name__=="__main__":main()
