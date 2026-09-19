"""Unblind frozen qualitative reviews; disagreement is NOT a label error rate."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from verify_news_predictive_value import dump,sha

HERE=Path(__file__).resolve().parent


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--audit',type=Path,required=True);args=ap.parse_args();out=args.audit
    blind=[json.loads(s) for s in (out/'blind.jsonl').read_text(encoding='utf8').splitlines()]
    review_file=HERE/'target_audit_reviews.txt';reviews={}
    for line in review_file.read_text(encoding='utf8').splitlines():
        i,kind,tone,note,quote=line.split('|');i=int(i)
        assert i not in reviews
        assert quote in blind[i-1]['source_text'],(i,quote)
        reviews[i]=dict(kind=kind,semantic_tone=tone,review_note=note,source_quote=quote)
    assert set(reviews)==set(range(1,101))
    dump(out/'frozen_review_manifest.json',dict(review_sha256=sha(review_file),outcome_blind=True,
        reviewed_material='title and extracted target passages for all 100; full source follow-up for IDs 18,23,25,48,71; not an exhaustive full-article read',
        reviewer='Codex qualitative review; not independent human gold',unblind_code_sha256=sha(Path(__file__))))
    answers={r['id']:r for r in map(json.loads,(out/'outcomes.jsonl').read_text(encoding='utf8').splitlines())}
    records=[]
    for r in blind:
        a=answers[r['id']];row={k:v for k,v in r.items() if k not in ('source_text','passages','passage_spans')}
        row.update(reviews[r['id']]);row.update(a)
        row['publication_anchor_calendar_days']=(pd.Timestamp(a['anchor_date'])-pd.Timestamp(r['published_date'][:10])).days
        records.append(row)
    df=pd.DataFrame(records);df.to_csv(out/'reviewed_100.csv',index=False,encoding='utf-8-sig')
    directed=df.semantic_tone.isin(['positive','negative']) & ~df.kind.eq('price_report')
    tone=df.semantic_tone.map({'positive':2,'negative':0})
    stats={f'h{h}':dict(n=int(directed.sum()),same=int((tone[directed]==df.loc[directed,f'label_{h}d']).sum()),
        neutral_price=int(df.loc[directed,f'label_{h}d'].eq(1).sum()),opposite=int(((tone[directed]+df.loc[directed,f'label_{h}d'])==2).sum())) for h in (1,3)}
    summary=dict(n=100,unique_articles=df.article.nunique(),kind_counts=df.kind.value_counts().to_dict(),
        missing_match=int((~df.matched).sum()),outside_original_input=int((~df.original_input_has_name).sum()),
        anchor_calendar_days=df.publication_anchor_calendar_days.value_counts().sort_index().to_dict(),
        semantic_vs_delayed_price=stats,warning='Descriptive stratified LLM review, not error prevalence or causal sentiment labels. No labels changed.')
    dump(out/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,default=int),flush=True)


if __name__=='__main__':main()
