"""Outcome-blind, deterministic 100-pair target-context review pack.

25 unique articles from each market x train/val stratum, SHA256 order, one
direct target per article. No selection on labels, predictions or errors. This
is a stratified diagnostic sample, not prevalence estimation or human gold.
Export source text plus bounded target passages before reviewing outcomes.
Never rewrite labels or source data based on semantic judgments.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd
from transformers import AutoTokenizer
from news_content import sanitize_article
from verify_news_predictive_value import dump,sha

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'ner'))
from matcher import CompanyMatcher


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,required=True);args=ap.parse_args()
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    source=HERE/'artifacts/news_impact_v2'
    docs=[sanitize_article(json.loads(l)) for l in (source/'articles.jsonl').open(encoding='utf8')]
    pairs=pd.read_parquet(source/'pairs.parquet');meta=pd.read_json(source/'companies.json',dtype={'ticker':str}).set_index('ticker')
    matcher=CompanyMatcher.from_csv(source/'aliases.csv')
    spec=json.loads((source/'pretrained.json').read_text(encoding='utf8'))
    tok=AutoTokenizer.from_pretrained(source/spec['embedding']['path'],local_files_only=True)
    chosen=[]
    for split in ('train','val'):
        for market in ('KOSPI','NASDAQ'):
            sub=pairs[pairs.split.eq(split)&pairs.market.eq(market)&pairs.is_direct].copy()
            sub['order']=[hashlib.sha256(('target-audit-v1|'+docs[r.article]['record_id']+'|'+r.ticker).encode()).hexdigest() for r in sub.itertuples()]
            chosen.extend(sub.sort_values('order').drop_duplicates('article').head(25).index.tolist())
    assert len(chosen)==100
    dump(out/'protocol.json',dict(selection='SHA256 target-audit-v1|record_id|ticker; 25 unique articles per market x train/val; no outcomes',
        reviewer='Codex qualitative source-passage review, not human gold',pair_indices=list(map(int,chosen)),
        source_hashes={str(source/n):sha(source/n) for n in ('articles.jsonl','pairs.parquet','aliases.csv')},code_hash=sha(Path(__file__))))
    blind=[];answers=[]
    with np.load(source/'label_config.npz') as cfg:thresholds=cfg['thresholds']
    for audit_id,idx in enumerate(chosen,1):
        r=pairs.loc[idx];d=docs[int(r.article)];title=d.get('title') or '';body=d.get('body') or '';full=title+'\n'+body
        hits=[m for m in matcher.match(title,body).mentions if m.ticker==r.ticker and m.confidence>=1]
        offsets=tok(title+'\n'+body[:6000],add_special_tokens=True,truncation=True,max_length=256,return_offsets_mapping=True)['offset_mapping']
        visible_end=max((b for a,b in offsets),default=0)
        body_hits=[m for m in hits if m.start>len(title)]
        selected=(body_hits or hits)[:2]
        spans=[]
        for m in selected:
            start=max(0,full.rfind('\n',0,m.start)+1);end=full.find('\n',m.end)
            end=len(full) if end<0 else end
            if end-start>900:start=max(start,m.start-170);end=min(end,m.end+460)
            if (start,end) not in spans:spans.append((start,end))
        if not spans:spans=[(0,min(len(full),700))]
        passages=[full[a:b] for a,b in spans]
        name=meta.loc[r.ticker,'name_official']
        blind.append(dict(id=audit_id,pair_index=int(idx),article=int(r.article),ticker=r.ticker,name=name,split=r.split,market=r.market,
            published_date=d['published_date'],title=title,passages=passages,passage_spans=spans,
            matched=bool(hits),original_input_has_name=any(m.end<=visible_end for m in hits),
            text_characters=len(full),source_text=full))
        ys=np.array([r.y_1d,r.y_3d],dtype='float32');cls=np.where(ys>thresholds,2,np.where(ys< -thresholds,0,1))
        answers.append(dict(id=audit_id,y_1d=float(ys[0]),y_3d=float(ys[1]),label_1d=int(cls[0]),label_3d=int(cls[1]),
            available_at=d['available_at'],anchor_date=r.anchor_date,label_end=r.label_end))
    for name,rows in (('blind.jsonl',blind),('outcomes.jsonl',answers)):
        (out/name).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf8')
    print(json.dumps(dict(out=str(out),pairs=len(blind),missing_match=sum(not r['matched'] for r in blind),
        outside_original_input=sum(not r['original_input_has_name'] for r in blind)),ensure_ascii=False))


if __name__=='__main__':main()
