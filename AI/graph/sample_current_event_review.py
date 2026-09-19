"""Fresh input-only semantic check, without showing rule evidence or outcomes.

Two new training articles per region/year, selected by a frozen SHA256 ordering,
excluding every article in the earlier 84-row development review. It is a small
AI-assisted quality check, not a human test set or the model's price validation.
"""
import argparse
import hashlib
import json
from pathlib import Path


def main(experiment,out):
    out.mkdir(parents=True,exist_ok=False)
    root=Path(__file__).resolve().parent
    old=root/'artifacts/news_quality_dataset_20260915_v2/human_review.json'
    excluded={r['record_id'] for r in json.loads(old.read_text(encoding='utf8'))}
    grouped={};packet=[]
    with (experiment/'articles.jsonl').open(encoding='utf8') as f:
        for line in f:
            r=json.loads(line)
            if 'current_event_decision' not in r or r['record_id'] in excluded:continue
            for c in r['companies']:
                key=(r['region'],r['published_date'][:4]);order=hashlib.sha256(('fresh-semantic-v1:'+r['record_id']+':'+c['ticker']).encode()).hexdigest()
                grouped.setdefault(key,[]).append((order,r,c['ticker']))
    for key,group in sorted(grouped.items()):
        used=set()
        for _,r,t in sorted(group,key=lambda x:x[0]):
            if r['record_id'] in used:continue
            used.add(r['record_id']);packet.append(dict(review_id=len(packet)+1,record_id=r['record_id'],region=key[0],year=key[1],published_date=r['published_date'],ticker=t,title=r['title'],body=r['body']))
            if len(used)==2:break
    assert len(packet)==24 and len({r['record_id'] for r in packet})==24
    (out/'packet.json').write_text(json.dumps(packet,ensure_ascii=False,indent=2),encoding='utf8')
    def sha(p):
        with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
    (out/'protocol.json').write_text(json.dumps(dict(source=str(experiment/'articles.jsonl'),source_sha256=sha(experiment/'articles.jsonl'),previous_review_sha256=sha(old),packet_sha256=sha(out/'packet.json'),selection='SHA256 fresh-semantic-v1:record_id:ticker, 2 distinct articles per region/year',review_type='blinded AI-assisted semantic check; not human gold',price_labels_shown=False,rule_evidence_shown=False,for_rule_tuning=False),indent=2),encoding='utf8')
    print('Fresh blinded review packet:',len(packet))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--experiment',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();main(a.experiment,a.out)
