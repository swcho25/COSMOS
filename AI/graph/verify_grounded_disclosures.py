"""Independent integrity audit of disclosure artifacts (not semantic accuracy).

Read source text with bytes.decode, preserving CRLF and therefore original
Unicode codepoint offsets. Text-mode universal newlines would corrupt offsets.
This verifier never changes source records, manifests or relation decisions.
"""
from pathlib import Path
import argparse
import collections
import csv
import datetime as dt
import hashlib
import json

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def rows(path):
    with path.open(encoding='utf8') as f:return [json.loads(line) for line in f]

def verify(base):
    base=Path(base).resolve();manifest=json.loads((base/'manifest.json').read_bytes());errors=[]
    if manifest.get('artifact_complete') is not True:errors.append('incomplete_manifest')
    for item in list(manifest['files'].values())+manifest['source_documents']:
        p=(base/item['path']).resolve()
        if not p.is_relative_to(base):raise ValueError('Unsafe artifact path')
        if sha(p)!=item['sha256'] or p.stat().st_size!=item['bytes']:errors.append('file:'+item['path'])
        if 'rows' in item and len(rows(p))!=item['rows']:errors.append('rows:'+item['path'])
    relations=rows(base/manifest['files']['disclosure_relationships']['path'])
    docs={r['document_id']:r for r in rows(base/manifest['files']['disclosure_documents']['path'])}
    if len({r['evidence_id'] for r in relations})!=len(relations):errors.append('duplicate_evidence_id')
    for r in relations:
        def fail(kind):errors.append(kind+':'+r['evidence_id'])
        text=(base/r['source_document_path']).read_bytes().decode('utf8')
        if hashlib.sha256(text.encode('utf8')).hexdigest()!=r['source_document_sha256']:fail('text_sha')
        if text[r['quote_start']:r['quote_end']]!=r['quote'] or not r['quote']:fail('quote')
        d=docs[r['record_id']]
        for side,key in [('source','src'),('target','dst')]:
            if r[side+'_identity_basis']=='literal_quote':
                if not r[side+'_quote'] or r[side+'_quote'] not in r['quote']:fail('party_quote')
            elif r[side+'_identity_basis']=='filing_metadata':
                if r['filing_role']!=side or r[key+'_ticker']!=r['filing_ticker']:fail('filing_role')
            else:fail('unknown_party_identity_basis')
        if d['issuer_corp_code']!=r['filing_corp_code'] or d['issuer_stock_code']!=r['filing_ticker']:fail('filing_identity')
        if d['published_at']!=r['published_at'] or d['source_url']!=r['source_url']:fail('filing_metadata')
        if r['rel_type']=='INVEST':
            if not(0<r['share_pct']<=100 and r['accounting_date']<=r['published_at'][:10]):fail('accounting')
        elif r['rel_type']=='SUPPLY':
            if r['contract_date']>r['published_at'][:10]:fail('contract_date')
        else:fail('unsupported_relation_type')
        if not r['observed_at'] or dt.datetime.fromisoformat(r['observed_at'])<dt.datetime.fromisoformat(r['published_at'].replace('Z','+00:00')):fail('observation')
    reviews=rows(base/manifest['files']['disclosure_review']['path'])
    return {'schema':'cosmos-grounded-disclosures-independent-integrity-audit-1',
        'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'manifest_sha256':sha(base/'manifest.json'),
        'relation_rows':len(relations),'source_texts':len(manifest['source_documents']),
        'exact_quote_checks':len(relations),'party_identity_checks':len(relations)*2,
        'errors':errors,'status':'PASS' if not errors else 'FAIL',
        'review_counts':dict(collections.Counter(r['reason'] for r in reviews)),
        'source_text_reader':'read_bytes().decode(utf8); original CRLF preserved',
        'human_gold':False,'semantic_accuracy_measured':False,'production_approved':False}

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--artifact',type=Path,required=True);ap.add_argument('--report',type=Path,required=True);args=ap.parse_args()
    result=verify(args.artifact)
    if args.report.exists():raise ValueError('Audit output already exists')
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['status']=='PASS' else 1)
