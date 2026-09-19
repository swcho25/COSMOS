"""Build locally auditable DISCLOSURE evidence from pinned source records.

Largest-shareholder rows remain historical reported ownership, never a claim of
current ownership. A narrow Korean signed supply-contract table parser adds
supplier -> named counterparty evidence. Subsidiary filings, cancellations,
unknown counterparties, plans and ambiguous layouts are retained for review.
SEC records remain company observations; no SEC relationship is manufactured.
All publication dates and actual collection timestamps remain distinct. This
retrospective archive is not a contemporaneously collected backtest dataset.
"""
from __future__ import annotations
import argparse
import collections
import csv
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import re

from lake_disclosures import normalize_dart, normalize_sec, _holder_aliases, _identifier, _day
from extract_ownership import norm_name

POLICY='cosmos-grounded-disclosure-1'

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
    return h.hexdigest()

def canonical(obj): return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def objsha(obj): return hashlib.sha256(canonical(obj).encode('utf8')).hexdigest()
def read(path):
    with Path(path).open(encoding='utf8') as f:
        for line in f:
            if line.strip():yield json.loads(line)

def packets(path):
    count=0;complete=False
    with gzip.open(path,'rt',encoding='utf8') as f:
        for line in f:
            row=json.loads(line)
            if complete:raise ValueError('Rows after source completion')
            if row.get('kind')=='completion':
                if row.get('reports')!=count or row.get('remote_writes') is not False:raise ValueError('Bad source receipt')
                complete=True
            elif row.get('kind')=='source_report':
                count+=1;yield row
            else:raise ValueError('Unknown source packet')
    if not complete:raise ValueError('Incomplete source transfer')

def supply_tables(report,lookup,publication_day):
    """Exact numbered contract form; reject subsidiaries, termination and plans."""
    title=report.get('report_nm','')
    if '공급계약' not in title:return [],None
    if '해지' in title or '체결' not in title:return [],'not_signed_supply_or_terminated'
    if '자회사' in title or '종속회사' in title:return [],'subsidiary_filing'
    found=[];issues=[]
    for index,file in enumerate(report.get('files',[])):
        text=file.get('text','')
        headings=list(re.finditer(r'(?m)^단일판매[ㆍ·]?공급계약\s*체결\s*$',text))
        if not headings:continue
        start=headings[-1].start();section=text[start:]
        if re.search(r'(?:자회사인|종속회사인|의 주요경영사항신고)',section[:500]):
            issues.append('subsidiary_filing');continue
        party=re.search(r'(?m)^3\.\s*계약상대\s*\n\s*([^\n]+)',section)
        signed=re.search(r'(?m)^7\.\s*계약\(수주\)일자\s*\n\s*(\d{4}-\d{2}-\d{2})\s*$',section)
        if not party or not signed:
            issues.append('unrecognized_signed_contract_form');continue
        name=party.group(1).strip()
        # English translation in parentheses can accompany a complete Korean
        # legal name. Never strip a Korean subsidiary/branch qualification.
        lookup_name=re.sub(r'\([A-Za-z][A-Za-z0-9 .,&/\-]*\)$','',name).strip()
        normalized=norm_name(lookup_name)
        # POSCO is an unsafe current-English alias of the holding company:
        # a steel operating subsidiary must not be silently promoted to it.
        if normalized.casefold() in {'posco','포스코'}:
            issues.append('holding_vs_operating_company_ambiguity');continue
        target=lookup.get(normalized)
        if not target:
            issues.append('counterparty_outside_universe_or_ambiguous');continue
        day=_day(signed.group(1))
        if day is None or day>publication_day:
            issues.append('future_or_invalid_contract_date');continue
        end=start+signed.end()
        if end-start>6000:
            issues.append('oversized_contract_form');continue
        found.append({'dst_ticker':target,'evidence_file_index':index,'evidence_text':text[start:end],
            'evidence_start':start,'evidence_end':end,'target_quote':name,
            'contract_date':day.isoformat(),'relation_subtype':'signed_supply_contract'})
    return found, (';'.join(sorted(set(issues))) if issues else ('no_supported_contract_form' if not found else None))

def run(inputs,out,as_of):
    inputs=Path(inputs).resolve();out=Path(out).resolve()
    if (out/'manifest.json').exists():raise ValueError('Output already complete; use a new output directory')
    out.mkdir(parents=True,exist_ok=True); (out/'source_documents').mkdir(exist_ok=True)
    companies=list(csv.DictReader((inputs/'companies.csv').open(encoding='utf-8-sig')))
    aliases=list(csv.DictReader((inputs/'aliases.csv').open(encoding='utf-8-sig')))
    lookup=_holder_aliases(companies,aliases);by_ticker={c['ticker']:c for c in companies}
    evidence={};docs={};reviews=[];source_inventory={};stats=collections.Counter();seen_reports={}
    started=dt.datetime.now(dt.timezone.utc).isoformat()
    for collection in ('recent','historical'):
        path=inputs/(collection+'_sources.jsonl.gz')
        if not path.exists():raise FileNotFoundError(path)
        for packet in packets(path):
            report=dict(packet['report']); rid=str(report.get('rcept_no',''))
            report['source_path']=report.get('source_path') or packet['container_source_path']
            # Old HDFS objects can lack corp_code; stable eight-digit filename
            # is acceptable only when explicitly recorded as the identity basis.
            identity_basis='report_corp_code'
            if not report.get('corp_code'):
                m=re.search(r'/([0-9]{8})_detail\.jsonl\.gz$',packet['container_source_path'])
                if m:report['corp_code']=m.group(1);identity_basis='archive_corp_code_filename'
            normalized=normalize_dart(report,companies,aliases,as_of=as_of)
            if normalized is None:
                reviews.append({'report_id':rid,'collection':collection,'reason':'invalid_filing_identity_or_publication','source_path':report['source_path']});continue
            doc=normalized['document'];ticker=doc['ticker'];company=by_ticker[ticker]
            stock=_identifier(report.get('stock_code'),6)
            if stock and stock!=ticker:raise ValueError('Filing corp/stock identity conflict '+rid)
            report_hash=objsha(packet['report'])
            # Same receipt in another snapshot is not a second event. Different
            # source serialization is retained; evidence identity deduplicates.
            seen_reports.setdefault(rid,set()).add(report_hash)
            collected=report.get('fetched_at') or report.get('collected_at')
            try:
                observed=dt.datetime.fromisoformat(str(collected).replace('Z','+00:00'))
                if observed.tzinfo is None:raise ValueError('naive observation')
                observed_at=observed.isoformat()
            except (ValueError,TypeError):observed_at=None
            doc.update({'source_kind':'DISCLOSURE','source_url':'https://dart.fss.or.kr/dsaf001/main.do?rcpNo='+rid,
                'issuer_corp_code':_identifier(report.get('corp_code'),8),'issuer_stock_code':ticker,
                'issuer_name':report.get('corp_name') or company['name_official'],
                'observed_at':observed_at,'local_verified_at':started,'identity_basis':identity_basis,
                'original_report_sha256':report_hash,'source_line_sha256':packet['source_line_sha256']})
            docs[doc['document_id']]=doc;stats[collection+'_reports']+=1
            relations=[]
            for edge in normalized['relationships']:
                edge=dict(edge);edge['relation_subtype']='reported_common_shareholding';edge['target_quote']='';edge['source_quote']=edge['holder_name']
                edge['src_identity_basis']='unique_exact_holder_legal_alias';edge['dst_identity_basis']=identity_basis
                relations.append(edge)
            supplies,issue=supply_tables(report,lookup,_day(report['rcept_dt']))
            for edge in supplies:
                if edge['dst_ticker']==ticker:continue
                edge.update({'src_ticker':ticker,'rel_type':'SUPPLY','source_quote':'',
                    'src_identity_basis':identity_basis,'dst_identity_basis':'unique_exact_contract_counterparty'})
                relations.append(edge)
            if issue:reviews.append({'report_id':rid,'collection':collection,'reason':issue,'title':report.get('report_nm'),'source_path':report['source_path']})
            if not relations and not issue:
                reviews.append({'report_id':rid,'collection':collection,'reason':doc['ownership_parse_status'],'title':report.get('report_nm'),'source_path':report['source_path']})
            for edge in relations:
                fileindex=edge['evidence_file_index'];file=report['files'][fileindex];text=file['text'];a=edge['evidence_start'];b=edge['evidence_end'];quote=edge['evidence_text']
                if text[a:b]!=quote or not quote:raise ValueError('Nonliteral evidence '+rid)
                textbytes=text.encode('utf8'); textsha=hashlib.sha256(textbytes).hexdigest()
                relative='source_documents/'+textsha+'.txt';document=out/relative
                if document.exists():
                    if sha(document)!=textsha:raise ValueError('Source text collision')
                else:document.write_bytes(textbytes)
                source_inventory[relative]={'path':relative,'sha256':textsha,'bytes':len(textbytes)}
                key=(doc['document_id'],edge['src_ticker'],edge['dst_ticker'],edge['rel_type'],edge['relation_subtype'],quote)
                eid='disclosure_'+objsha(key)
                candidate={'evidence_id':eid,'record_id':doc['document_id'],'report_id':rid,'src_ticker':edge['src_ticker'],
                    'dst_ticker':edge['dst_ticker'],'rel_type':edge['rel_type'],'directed':True,'relation_subtype':edge['relation_subtype'],
                    'source_kind':'DISCLOSURE','source_authority':'DART','published_at':doc['published_at'],
                    'published_at_precision':'date','observed_at':observed_at,'local_verified_at':started,
                    'source_url':doc['source_url'],'title':doc['report_name'],'quote':quote,'source_quote':edge['source_quote'],
                    'target_quote':edge['target_quote'],'quote_start':a,'quote_end':b,'offset_basis':'original_file_text_unicode_codepoints',
                    'source_document_sha256':textsha,'source_document_path':relative,'source_document_original_filename':file.get('filename'),
                    'source_document_file_index':fileindex,'source_archive_path':report['source_path'],
                    'original_report_sha256':report_hash,'source_line_sha256':packet['source_line_sha256'],
                    'confidence':1.0,'confidence_meaning':'deterministic_parsing_confidence_not_calibrated_probability',
                    'accounting_date':edge.get('as_of_date'),'share_pct':edge.get('share_pct'),'contract_date':edge.get('contract_date'),
                    'src_corp_code':_identifier(by_ticker[edge['src_ticker']].get('dart_corp_code'),8),
                    'dst_corp_code':_identifier(by_ticker[edge['dst_ticker']].get('dart_corp_code'),8),
                    'src_identity_basis':edge['src_identity_basis'],'dst_identity_basis':edge['dst_identity_basis'],
                    'source_identity_basis':'literal_quote' if edge['source_quote'] else 'filing_metadata',
                    'target_identity_basis':'literal_quote' if edge['target_quote'] else 'filing_metadata',
                    'filing_role':'source' if edge['src_ticker']==ticker else 'target','filing_ticker':ticker,
                    'filing_corp_code':doc['issuer_corp_code'],'filing_name':doc['issuer_name'],
                    'quality_flags':{'literal_quote_verified':True,'filing_identity_verified':True,'source_from_filing_metadata':not bool(edge['source_quote']),
                        'target_from_filing_metadata':not bool(edge['target_quote']),'retrospective_archive':True,
                        'current_relation_asserted':False,'independent_human_validated':False,'point_in_time_collection_complete':False,
                        'production_approved':False,'all_disclosures_complete':False},'policy_version':POLICY}
                evidence.setdefault(eid,candidate)
    # Revalidate the exact existing 48 source rows, independently of dedup IDs.
    old=list(read(inputs/'recent/disclosure_relationships.jsonl'))
    old_matched=sum(any(e['report_id']==r['report_id'] and e['src_ticker']==r['src_ticker'] and e['dst_ticker']==r['dst_ticker'] and e['quote']==r['evidence_text'] and e['quote_start']==r['evidence_start'] for e in evidence.values()) for r in old)
    if old_matched!=len(old):raise ValueError(f'Existing evidence mismatch {old_matched}/{len(old)}')
    sec=[]
    for report in read(inputs/'recent/sec.jsonl'):
        for doc in normalize_sec(report,companies,as_of=as_of):
            doc.update({'source_kind':'DISCLOSURE','source_url':report.get('sourceUrl'),
                'observed_at':report.get('collectedAt'),'local_verified_at':started,'relationship_evidence_created':False})
            sec.append(doc)
    files={}
    outputs={'disclosure_relationships':sorted(evidence.values(),key=lambda r:r['evidence_id']),
        'disclosure_documents':sorted(docs.values(),key=lambda r:r['document_id']),
        'disclosure_review':reviews,'sec_company_observations':sec}
    for kind,rows in outputs.items():
        p=out/(kind+'.jsonl')
        with p.open('w',encoding='utf8',newline='\n') as f:
            for row in rows:f.write(canonical(row)+'\n')
        files[kind]={'path':p.name,'sha256':sha(p),'bytes':p.stat().st_size,'rows':len(rows)}
    counts={'relationships':len(evidence),'relation_types':dict(collections.Counter(e['rel_type'] for e in evidence.values())),
        'unique_company_pairs':len({(e['src_ticker'],e['dst_ticker'],e['rel_type']) for e in evidence.values()}),
        'dart_documents':len(docs),'source_text_files':len(source_inventory),'review_rows':len(reviews),
        'sec_company_observations':len(sec),'sec_relationships':0,'existing_evidence_verified':old_matched,**stats}
    manifest={'schema':'cosmos-grounded-disclosures-1','artifact_complete':True,'as_of':as_of,'created_at':started,
        'counts':counts,'files':files,'source_documents':sorted(source_inventory.values(),key=lambda r:r['path']),
        'input_files':[{'path':str(p.relative_to(inputs)),'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(inputs.rglob('*')) if p.is_file()],
        'code_sha256':sha(__file__),'policy_version':POLICY,'production_approved':False,'graph_quality_validated':False,
        'source_kind':'DISCLOSURE','remote_writes':False,'hdfs_published':False,'db_loaded':False,
        'limitations':['Selected historical positive ownership reports and recent explicit report families, not all disclosures.',
          'Shareholding is as of accounting_date, published later; never assume current ownership.',
          'Unknown or subsidiary counterparties are not promoted to listed parents.',
          'Publication date and observation time differ; retrospective sources do not prove live historical availability.',
          'SEC creates company-document observations only; no relationship extraction.',
          'Exact extraction and counts are verified; this is not human-gold semantic accuracy.']}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    return manifest

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--inputs',type=Path,required=True);ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--as-of',default='2026-09-19T00:00:00Z');args=ap.parse_args()
    print(json.dumps(run(args.inputs,args.out,args.as_of)['counts'],ensure_ascii=False,indent=2))

if __name__=='__main__':main()
