"""Read-only retrospective diagnosis of frozen news GNN predictions.

No TrainingData construction (it writes PCA), retraining, checkpoint selection or
service changes. Label thresholds are read from the frozen bundle. Title-only
keyword topics are overlapping heuristic tags, NOT human gold categories; OTHER
is retained. Freeze the rules/protocol before examining slice outcomes. Both val
and already-inspected test are reported; this is hypothesis generation only.

Same pair rows per model/slice; primary accuracy averages within session then
across sessions. HAC lags 5/20 use observed sessions, as in the original audit.
Holm covers ALL eligible test slice x seven baseline comparisons per lag, including
ALL and directness slices. At least 30 sessions are required for inference; smaller
slices still appear descriptively. CIs are unadjusted, conditional on fitted models.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm
from audit_news_impact import hac_mean_se
from verify_news_predictive_value import holm

HERE = Path(__file__).resolve().parent
TOPICS = {
    "earnings": r"실적|매출|영업이익|순이익|흑자|적자|어닝|\bearnings?\b|\brevenue\b|\bprofit\b|\bEPS\b",
    "contracts": r"계약|수주|납품|공급|협약|제휴|파트너십|\bcontract\b|\bsupply\b|\bpartnership\b|\bagreement\b",
    "regulation": r"규제|제재|소송|관세|과징금|반독점|\bregulat\w*\b|\blawsuit\b|\btariff\w*\b|\bantitrust\b|\bsanction\w*\b",
    "capital": r"인수|합병|지분|유상증자|자사주|배당|상장|\bacquisition\b|\bmerger\b|\bbuyback\b|\bdividend\b|\bIPO\b",
    "product": r"출시|신제품|양산|개발|특허|\blaunch\w*\b|\bpatent\w*\b|\bnew product\b",
    "macro_market": r"금리|환율|물가|고용|증시|주가|코스피|나스닥|\binflation\b|\binterest rate\b|\bstocks?\b|\bmarket\w*\b|\bfederal reserve\b",
}
BASELINES = ("majority", "stage1", "stage2", "market_prior", "ticker_prior", "no_content", "no_edges")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def topic_masks(titles):
    masks = {name: np.array([bool(re.search(pattern, t, re.I)) for t in titles])
             for name, pattern in TOPICS.items()}
    masks["other"] = ~np.logical_or.reduce(list(masks.values()))
    return {"all": np.ones(len(titles), dtype=bool), **masks}


def confusion(actual, predicted):
    return np.bincount(actual * 3 + predicted, minlength=9).reshape(3, 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=HERE/"artifacts/news_impact_v2")
    ap.add_argument("--evidence", type=Path, default=HERE/"artifacts/news_impact_v2_evidence")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    source, evidence, out = (p.resolve() for p in (args.source,args.evidence,args.out))
    if any(out == p or p in out.parents or out in p.parents for p in (source,evidence)):
        raise ValueError("Output must be a separate sibling analysis directory")
    start = time.monotonic()
    out.mkdir(parents=True, exist_ok=False)
    files = [p for p in source.iterdir() if p.is_file()]
    files += [evidence/"protocol.json", evidence/"predictions_market_prior.npz", evidence/"predictions_ticker_prior.npz"]
    files += [evidence/m/"predictions.npz" for m in ("no_content","no_edges")]
    hashes = {str(p): sha(p) for p in sorted(files)}
    protocol = {"scope":"retrospective diagnostic only; val and previously inspected test",
        "topics_title_regex":TOPICS, "topics_overlap":True,"scopes":["all","direct","indirect"],
        "baselines":BASELINES,"minimum_sessions_for_inference":30,
        "inference":"Holm all eligible test comparisons separately per HAC lag 5/20; unadjusted CIs",
        "case_selection":"highest-confidence wrong direct/indirect per market/horizon; unique article within slice; 5 each",
        "source_hashes":hashes, "analysis_code_sha256":sha(Path(__file__))}
    dump(out/"protocol.json", protocol)
    old = json.loads((evidence/"protocol.json").read_text(encoding="utf-8"))
    for name, digest in old["source_hashes"].items():
        if sha(source/name) != digest:
            raise ValueError(f"Source differs from matched-ablation protocol: {name}")
    docs = [json.loads(l) for l in (source/"articles.jsonl").open(encoding="utf-8")]
    pairs = pd.read_parquet(source/"pairs.parquet")
    article = pairs.article.to_numpy()
    if not np.array_equal(pairs.news_id.to_numpy(), np.array([d["record_id"] for d in docs])[article]):
        raise ValueError("Article/prediction alignment mismatch")
    y = pairs[["y_1d","y_3d"]].to_numpy(dtype="float32")
    with np.load(source/"label_config.npz") as config:
        thresholds, scales = config["thresholds"], config["scales"]
    cls = np.where(y > thresholds, 2, np.where(y < -thresholds, 0, 1))
    mag = np.minimum(abs(y)/scales,1)
    outputs = {}
    for name in (*BASELINES,"stage3"):
        path = (evidence/name/"predictions.npz" if name in ("no_content","no_edges") else
                (evidence if name.endswith("_prior") else source)/f"predictions_{name}.npz")
        with np.load(path) as saved:
            prob, strength = saved["probabilities"], saved["magnitude"]
        if prob.shape != (len(pairs),2,3) or strength.shape != (len(pairs),2):
            raise ValueError(f"Prediction shape mismatch: {name}")
        if not (np.isfinite(prob).all() and np.isfinite(strength).all()
                and np.allclose(prob.sum(-1),1,atol=2e-6)
                and ((prob>=0)&(prob<=1)).all() and ((strength>=0)&(strength<=1)).all()):
            raise ValueError(f"Invalid predictions: {name}")
        outputs[name] = (prob, strength, prob.argmax(-1))
    tags = topic_masks([d.get("title","") for d in docs])
    coverage = [{"split":split,"topic":tag,"n_articles":int((mask & np.array([d["split"]==split for d in docs])).sum())}
                for split in ("train","val","test") for tag,mask in tags.items()]
    pd.DataFrame(coverage).to_csv(out/"topic_coverage.csv",index=False)
    metrics, comparisons, cases = [], [], []
    for split in ("val","test"):
        for market in ("KOSPI","NASDAQ"):
            base = (pairs.split.eq(split)&pairs.market.eq(market)).to_numpy()
            for scope in ("all","direct","indirect"):
                scope_mask = np.ones(len(pairs),bool) if scope=="all" else pairs.is_direct.to_numpy() == (scope=="direct")
                for topic, mask in tags.items():
                    selected = base & scope_mask & mask[article]
                    idx = np.flatnonzero(selected)
                    if not len(idx):
                        continue
                    sub = pairs.iloc[idx]
                    session_codes, dates = pd.factorize(sub.anchor_date,sort=True)
                    counts = np.bincount(session_codes)
                    def daily(values):
                        return np.bincount(session_codes,weights=values)/counts
                    for h,horizon in enumerate((1,3)):
                        key = {"split":split,"market":market,"scope":scope,"topic":topic,"horizon":horizon,
                               "n_pairs":len(idx),"n_articles":int(sub.article.nunique()),"n_sessions":len(dates)}
                        daily_correct = {}
                        for name,(p,m,pred) in outputs.items():
                            truth, guess = cls[idx,h], pred[idx,h]
                            correct = truth == guess
                            daily_correct[name] = daily(correct)
                            cm = confusion(truth,guess)
                            recall = np.divide(cm.diagonal(),cm.sum(1),out=np.full(3,np.nan),where=cm.sum(1)>0)
                            row = key | {"model":name,"pair_accuracy":float(correct.mean()),
                                "session_accuracy":float(daily_correct[name].mean()),
                                "balanced_accuracy":float(np.nanmean(recall)),
                                "magnitude_mae":float(abs(m[idx,h]-mag[idx,h]).mean()),
                                "mean_confidence":float(p[idx,h].max(-1).mean()),
                                "disagreement_with_no_content":float((guess!=outputs["no_content"][2][idx,h]).mean())}
                            for c,label in enumerate(("negative","neutral","positive")):
                                row[f"true_{label}_share"] = float((truth==c).mean())
                                row[f"pred_{label}_share"] = float((guess==c).mean())
                                row[f"recall_{label}"] = float(recall[c])
                                for j in range(3): row[f"confusion_{c}_{j}"] = int(cm[c,j])
                            metrics.append(row)
                        if split=="test" and len(dates)>=30:
                            for name in BASELINES:
                                diff = daily_correct["stage3"]-daily_correct[name]
                                delta = float(diff.mean())
                                for lag in (5,20):
                                    se = float(hac_mean_se(diff,lag))
                                    p = float(2*norm.sf(abs(delta/se))) if se>0 else float(delta==0)
                                    comparisons.append(key|{"baseline":name,"lag":lag,"delta":delta,"se":se,
                                        "ci95_unadjusted_low":delta-1.96*se,"ci95_unadjusted_high":delta+1.96*se,"p":p})
                        if split=="test" and topic=="all" and scope!="all":
                            p,_,pred = outputs["stage3"]
                            wrong = idx[pred[idx,h]!=cls[idx,h]]
                            ordered = wrong[np.argsort(-p[wrong,h].max(-1),kind="stable")]
                            seen = set()
                            for i in ordered:
                                a = int(article[i])
                                if a in seen: continue
                                seen.add(a); d = docs[a]
                                cases.append({"market":market,"scope":scope,"horizon":horizon,"article":a,
                                    "news_id":d["record_id"],"title":d["title"],"published_date":d["published_date"],
                                    "ticker":pairs.iloc[i].ticker,"actual":int(cls[i,h])-1,"prediction":int(pred[i,h])-1,
                                    "confidence":float(p[i,h].max()),"excess_log_return":float(y[i,h]),
                                    "no_content":int(outputs["no_content"][2][i,h])-1,
                                    "lightgbm":int(outputs["stage2"][2][i,h])-1,
                                    "body_excerpt":d.get("body","")[:600]})
                                if len(seen)==5: break
            print(f"finished {split} {market}",flush=True)
    report = pd.DataFrame(metrics)
    report.to_csv(out/"slice_metrics.csv",index=False)
    comp = pd.DataFrame(comparisons)
    for lag in (5,20):
        selected = comp.lag.eq(lag)
        comp.loc[selected,"p_holm"] = holm(comp.loc[selected,"p"].to_numpy())
        comp.loc[selected,"family_size"] = int(selected.sum())
    comp["positive_significant"] = (comp.delta>0)&(comp.p_holm<.05)
    keys = ["market","scope","topic","horizon","baseline"]
    stable = comp.groupby(keys).positive_significant.all()
    comp["positive_at_both_lags"] = [bool(stable.loc[tuple(getattr(r,k) for k in keys)]) for r in comp.itertuples()]
    comp.to_csv(out/"slice_comparisons.csv",index=False)
    dump(out/"error_cases.json",cases)
    # Exact text overlap is a data diagnostic, not semantic near-duplicate detection.
    previous = set(); overlap = []
    for split in ("train","val","test"):
        selected = [d for d in docs if d["split"]==split]
        hashes_text = [d["text_hash"] for d in selected]
        overlap.append({"split":split,"articles":len(selected),
            "duplicate_texts_within_split":len(selected)-len(set(hashes_text)),
            "articles_matching_earlier_split":sum(x in previous for x in hashes_text)})
        previous.update(hashes_text)
    dump(out/"text_overlap.json",overlap)
    # Original evaluation uses float32 targets, but rounded frozen thresholds can
    # differ near the boundary: explicitly cross-check ALL aggregate cells.
    original = pd.read_csv(evidence/"evaluation.csv")
    check = report[(report.topic=="all")&(report.scope=="all")].merge(original,
        on=["split","market","horizon","model"],suffixes=("_new","_old"),validate="one_to_one")
    error = float(abs(check.pair_accuracy_new-check.pair_accuracy_old).max())
    if len(check)!=64 or error>1e-10:
        raise AssertionError(f"Aggregate reproduction mismatch: {len(check)} cells, {error}")
    if any(sha(Path(path))!=digest for path,digest in hashes.items()):
        raise AssertionError("Source files changed during diagnosis")
    summary = {"seconds":time.monotonic()-start,"source_unchanged":True,
        "aggregate_cells_reproduced":len(check),"max_accuracy_difference":error,
        "metric_rows":len(report),"comparison_rows":len(comp),"topic_coverage":coverage,
        "holm_family_size_per_lag":int(comp.lag.eq(5).sum()),
        "positive_no_content_slices":comp[(comp.baseline=="no_content")&(comp.lag==5)&comp.positive_at_both_lags][keys+["delta","p_holm"]].to_dict("records")}
    dump(out/"summary.json",summary)
    print(json.dumps({k:v for k,v in summary.items() if k!="topic_coverage"},ensure_ascii=False),flush=True)


if __name__=="__main__":
    main()
