"""Follow-up descriptive audit after failure slices: loss weighting and text truncation.

Read-only frozen tokenizer/alias/label inputs, no encoder forward or model training.
The matching check measures whether an existing direct company's matched name fits
inside the actual embedding token prefix, not whether its full event context fits.
Unreproduced mentions are counted separately, never assumed to be truncated.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from transformers import AutoTokenizer

HERE = Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/"ner"))
from matcher import CompanyMatcher
from news_content import sanitize_article


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--source",type=Path,default=HERE/"artifacts/news_impact_v2")
    ap.add_argument("--out",type=Path,required=True)
    args=ap.parse_args()
    source,out=args.source.resolve(),args.out.resolve()
    if source==out or source in out.parents or out in source.parents:
        raise ValueError("Use a separate sibling output directory")
    out.mkdir(parents=True,exist_ok=False)
    spec=json.loads((source/"pretrained.json").read_text(encoding="utf8"))
    tokenizer_path=source/spec["embedding"]["path"]
    tracked=[source/n for n in ("articles.jsonl","pairs.parquet","aliases.csv","pretrained.json","label_config.npz")]
    tracked += [p for p in tokenizer_path.iterdir() if p.is_file() and p.suffix not in (".bin",".safetensors")]
    before={str(p):sha(p) for p in tracked}
    (out/"protocol.json").write_text(json.dumps({"scope":"exploratory follow-up to failure slices",
        "source_hashes":before,"code_sha256":sha(Path(__file__)),
        "max_embedding_tokens":256,"max_body_characters":6000},indent=2),encoding="utf8")
    docs=[json.loads(l) for l in (source/"articles.jsonl").open(encoding="utf8")]
    pairs=pd.read_parquet(source/"pairs.parquet")
    tokenizer=AutoTokenizer.from_pretrained(tokenizer_path,local_files_only=True,trust_remote_code=False,use_fast=True)
    if not tokenizer.is_fast:
        raise ValueError("Offset audit requires fast tokenizer")
    matcher=CompanyMatcher.from_csv(source/"aliases.csv")
    article_rows,mention_rows=[],[]
    for start in range(0,len(docs),128):
        group=[sanitize_article(d) for d in docs[start:start+128]]
        texts=[(d.get("title") or "")+"\n"+(d.get("body") or "")[:6000] for d in group]
        encoded=tokenizer(texts,truncation=True,max_length=256,return_offsets_mapping=True)
        for j,(d,text,offsets) in enumerate(zip(group,texts,encoded["offset_mapping"])):
            idx=start+j; end=max(b for a,b in offsets)
            matches=matcher.match(d.get("title"),d.get("body")).mentions
            article_rows.append({"article":idx,"split":d["split"],"region":d["region"],
                "embedding_character_end":end,"truncated":end<len(text.rstrip()) or len(d.get("body") or "")>6000,
                "n_embedding_tokens":len(encoded["input_ids"][j])})
            for m in d["companies"]:
                hits=[h for h in matches if h.ticker==m["ticker"] and h.confidence>=1.0]
                mention_rows.append({"article":idx,"split":d["split"],"region":d["region"],"ticker":m["ticker"],
                    "first_pos":m["first_pos"],"matched_full_text":bool(hits),
                    "visible_embedding":any(h.end<=end for h in hits),
                    "beyond_embedding":bool(hits) and not any(h.end<=end for h in hits)})
        if start%4096==0: print(f"token/mention audit {start}/{len(docs)}",flush=True)
    articles=pd.DataFrame(article_rows); mentions=pd.DataFrame(mention_rows)
    articles.to_csv(out/"article_coverage.csv",index=False)
    mentions.to_csv(out/"mention_coverage.csv",index=False)
    # Restrict name visibility to direct pairs that actually have training/evaluation labels.
    direct=pairs[pairs.is_direct].merge(mentions,on=["article","ticker","split"],validate="one_to_one")
    if len(direct)!=int(pairs.is_direct.sum()): raise AssertionError("Missing direct-pair audit")
    coverage=direct.groupby(["split","market"]).agg(n_direct_pairs=("ticker","size"),
        reproduced=("matched_full_text","mean"),visible_embedding=("visible_embedding","mean"),
        beyond_embedding=("beyond_embedding","mean"))
    coverage.to_csv(out/"direct_pair_coverage.csv")
    loss=[]
    for split in ("train","val","test"):
        sub=pairs[pairs.split==split]
        share=sub.groupby("article").is_direct.mean()
        loss.append({"split":split,"n_pairs":len(sub),"n_direct_pairs":int(sub.is_direct.sum()),
            "direct_pair_share":float(sub.is_direct.mean()),"direct_loss_coefficient_share":float(share.mean()),
            "median_candidates":float(sub.groupby("article").size().median())})
    pd.DataFrame(loss).to_csv(out/"loss_weighting.csv",index=False)
    with np.load(source/"label_config.npz") as config: thresholds=config["thresholds"]
    y=pairs[["y_1d","y_3d"]].to_numpy(dtype="float32")
    cls=np.where(y>thresholds,2,np.where(y< -thresholds,0,1))
    distribution=[]
    for split in ("train","val","test"):
        for market in ("KOSPI","NASDAQ"):
            selected=(pairs.split.eq(split)&pairs.market.eq(market)).to_numpy()
            for h,horizon in enumerate((1,3)):
                distribution.append({"split":split,"market":market,"horizon":horizon,
                    **{label:float((cls[selected,h]==c).mean()) for c,label in enumerate(("negative","neutral","positive"))}})
    pd.DataFrame(distribution).to_csv(out/"label_distribution.csv",index=False)
    if any(sha(Path(p))!=value for p,value in before.items()):raise AssertionError("Inputs changed")
    summary={"source_unchanged":True,"loss_weighting":loss,"direct_pair_coverage":coverage.reset_index().to_dict("records"),
             "truncation":articles.groupby(["split","region"]).truncated.mean().rename("fraction").reset_index().to_dict("records")}
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf8")
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=="__main__":main()
