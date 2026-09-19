"""Build canonical recent-news evidence in a new, local staging directory.

Uses existing prepared/live sources, audited text cleaning, dictionary matching,
literal Korean relation rules and DART shareholder tables. This is not GNN
training or a service deployment. All source completeness and NLP limitations
remain explicit; an empty extraction must not be confused with a missing input.
Run on the HDFS host after export_lake_recent_inputs.py has completed.
"""
from __future__ import annotations
import argparse
from collections import Counter
import csv
import datetime as dt
import hashlib
import json
import multiprocessing
from pathlib import Path
import re
import sys

from lake_news_normalize import normalize_article, analyze_article
from lake_document_ids import assign_document_ids
from lake_disclosures import normalize_dart, normalize_sec
from relationship_score_components import timestamp
from matcher import CompanyMatcher


_ARTICLE_MATCHER=None
_ARTICLE_AFFILIATES=()


def _initialize_article_worker(aliases_path,affiliate_pairs):
    """Construct an independent matcher in each spawn/fork worker."""
    global _ARTICLE_MATCHER,_ARTICLE_AFFILIATES
    _ARTICLE_MATCHER=CompanyMatcher.from_csv(aliases_path)
    _ARTICLE_AFFILIATES=affiliate_pairs


def _analyze_article_worker(article):
    return analyze_article(article,_ARTICLE_MATCHER,affiliate_pairs=_ARTICLE_AFFILIATES)


def analyze_articles(articles,aliases_path,affiliate_pairs,*,workers=1,mp_context=None):
    """Yield analyses in input order; only the caller writes output files.

    The child initializer reads the alias dictionary once. Text matching alone
    is parallel; identity assignment, selection and all output remain in main.
    """
    if type(workers) is not int or workers not in (1,2):
        raise ValueError('workers must be 1 or 2')
    affiliate_pairs=tuple(tuple(pair) for pair in affiliate_pairs)
    if workers==1:
        matcher=CompanyMatcher.from_csv(aliases_path)
        for article in articles:
            yield analyze_article(article,matcher,affiliate_pairs=affiliate_pairs)
        return
    context=multiprocessing.get_context(mp_context)
    with context.Pool(processes=workers,initializer=_initialize_article_worker,
                      initargs=(str(aliases_path),affiliate_pairs)) as pool:
        yield from pool.imap(_analyze_article_worker,articles,chunksize=64)


def rows(path):
    with Path(path).open(encoding='utf8') as stream:
        for line in stream:
            if line.strip(): yield json.loads(line)


def write_rows(path, records):
    count=0
    with Path(path).open('x',encoding='utf8') as stream:
        for record in records:
            stream.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n'); count+=1
    return count


def digest(path):
    sha=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): sha.update(block)
    return sha.hexdigest()


def map_news(row, kind):
    if kind=='news_prepared': return row
    if kind=='news_realtime':
        return {'record_id':row['event_id'],'source_dataset':'realtime_news_raw',
                'source_path':row['source_path'],'title':row.get('title'),'body':row.get('content'),
                'url':row.get('url'),'published_at':row.get('published_at'),
                'collected_at':row.get('collected_at'),'publisher':row.get('organization'),
                'region':row.get('region'),'language':row.get('language')}
    # This mapping is validated against the observed live mirror schema.
    return {'record_id':str(row.get('news_id') or row.get('id') or row.get('record_id') or ''),
            'source_dataset':'live_mysql_overseas','source_path':row['source_path'],
            'title':row.get('title'),'body':row.get('body') or row.get('content'),
            'url':row.get('url') or row.get('link'),
            'published_at':next((row[k] for k in ('published_at','published_date','pub_date','publish_date','publishedAt') if row.get(k)),None),
            'collected_at':row.get('crawled_at') or row.get('collected_at'),
            'collected_at_source':'crawled_at' if row.get('crawled_at') else 'collected_at',
            'publisher':row.get('publisher') or row.get('organization'),
            'organization':row.get('organization'),'provider':row.get('source'),
            'region':'overseas','language':row.get('language')}


NEWS_SEMANTIC_FIELDS=('title','body','published_at','url','canonical_url','region','language')


def merge_news_receptions(records):
    """Collapse repeated source identities only when their semantic payload agrees."""
    groups={}
    for row in records:
        key=row['source_dataset'],row['record_id']
        if key in groups:
            conflict=[name for name in NEWS_SEMANTIC_FIELDS if groups[key][0].get(name)!=row.get(name)]
            if conflict:
                raise ValueError(f'Conflicting repeated news identity {key}: {conflict}')
        groups.setdefault(key,[]).append(row)
    merged=[]
    invalid_collection_times=0
    for key,copies in sorted(groups.items()):
        chosen=dict(copies[0] if len(copies)==1 else min(copies,key=lambda r:(str(r.get('source_path') or ''),json.dumps(r,sort_keys=True,ensure_ascii=False))))
        paths=sorted({p for row in copies for p in ([row.get('source_path')]+list(row.get('source_paths') or [])) if p})
        collection_values=sorted({str(row['collected_at']) for row in copies if row.get('collected_at')})
        collection_times=[]
        for value in collection_values:
            try:
                collection_times.append(timestamp(value))
            except (ValueError,OverflowError):
                invalid_collection_times+=1
        flags={flag for row in copies for flag in row.get('quality_flags') or []}
        if len(collection_times)!=len(collection_values):
            flags.add('collection_time_without_valid_timezone')
        # Preserve original collection clocks when their timezone is unknown.
        # They remain explicitly unqualified provenance, never UTC availability.
        chosen.update(source_path=paths[0] if paths else None,source_paths=paths,
                      collected_at=min(collection_times).isoformat().replace('+00:00','Z') if collection_times else (collection_values[0] if collection_values else None),
                      collected_at_precision='timestamp' if collection_times else ('unknown_timezone' if collection_values else None),
                      collected_at_values=collection_values,quality_flags=sorted(flags),source_reception_count=len(copies))
        merged.append(chosen)
    return merged,{'input_receptions':len(records),'unique_source_records':len(merged),
                   'merged_receptions':len(records)-len(merged),'invalid_collection_times':invalid_collection_times}


def _numeric_identifier(value,width):
    value=str(value or '').strip()
    if value.endswith('.0'): value=value[:-2]
    return value.zfill(width) if value.isdigit() and len(value)<=width else None


def _filing_day(value):
    value=str(value or '').replace('-','')
    if len(value)!=8 or not value.isdigit(): return None
    try: return dt.date(int(value[:4]),int(value[4:6]),int(value[6:])).isoformat()
    except ValueError: return None


def select_dart_filings(records,companies,aliases,*,as_of):
    """Choose one complete normalized copy, keeping its document and evidence together."""
    metadata={}; groups={}; invalid=0; count=0
    ranks={'parsed':4,'parsed_no_universe_holder':3,'no_recognized_table':2,
           'invalid_accounting_date':1,'not_supported_report':0}
    for report in records:
        count+=1
        receipt=str(report.get('rcept_no') or '')
        if re.fullmatch(r'\d{14}',receipt):
            identity=(_filing_day(report.get('rcept_dt')),_numeric_identifier(report.get('corp_code'),8))
            if receipt in metadata and metadata[receipt]!=identity:
                raise ValueError(f'Conflicting DART publication date/company for {receipt}')
            metadata[receipt]=identity
        normalized=normalize_dart(report,companies,aliases,as_of=as_of)
        if normalized is None:
            invalid+=1; continue
        doc=normalized['document']; key=doc['document_id']
        length=sum(len(file.get('text') or '') for file in report.get('files') or [] if isinstance(file,dict))
        rank=(ranks[doc['ownership_parse_status']],len(normalized['relationships']),length)
        groups.setdefault(key,[]).append((rank,normalized))
    chosen=[]
    for key,copies in sorted(groups.items()):
        identities={(x[1]['document']['ticker'],x[1]['document']['published_at']) for x in copies}
        if len(identities)!=1: raise ValueError(f'Conflicting normalized DART identity {key}')
        best_rank=max(rank for rank,_ in copies)
        best=min((value for rank,value in copies if rank==best_rank),
                 key=lambda value:(str(value['document'].get('source_path') or ''),json.dumps(value,sort_keys=True,ensure_ascii=False)))
        paths=sorted({value['document']['source_path'] for _,value in copies if value['document'].get('source_path')})
        best['document'].update(source_paths=paths,source_reception_count=len(copies))
        chosen.append(best)
    return chosen,{'input_receptions':count,'selected_filings':len(chosen),'invalid_or_future_rows':invalid,
                   'merged_receptions':sum(len(copies)-1 for copies in groups.values())}


def _sec_acceptance(report):
    value=report.get('acceptanceDateTime')
    if not isinstance(value,str) or not re.search(r'[T ]\d{2}:\d{2}',value): return None
    try: return timestamp(value)
    except (ValueError,OverflowError): return None


def select_sec_filings(records,companies,*,as_of):
    """Prefer exact acceptance before normalization; never bypass future acceptance."""
    groups={}; invalid=0; count=0; future=0; documents=[]
    for report in records:
        count+=1
        accession=str(report.get('accessionNumber') or '').strip()
        if not re.fullmatch(r'\d{10}-\d{2}-\d{6}|\d{18}',accession):
            invalid+=1; continue
        groups.setdefault(accession.replace('-',''),[]).append(report)
    for accession,copies in sorted(groups.items()):
        accepted={at for report in copies if (at:=_sec_acceptance(report)) is not None}
        if len(accepted)>1: raise ValueError(f'Conflicting SEC acceptance timestamps for {accession}')
        ciks={_numeric_identifier(report.get('cik'),10) for report in copies}
        if len(ciks)>1: raise ValueError(f'Conflicting SEC issuer CIK for {accession}')
        if accepted and next(iter(accepted))>=timestamp(as_of):
            future+=1; continue
        candidates=[report for report in copies if _sec_acceptance(report) is not None] if accepted else copies
        if not accepted:
            days={day for report in candidates if (day:=_filing_day(report.get('filingDate'))) is not None}
            if len(days)>1: raise ValueError(f'Conflicting SEC filing dates for {accession}')
            if days: candidates=[report for report in candidates if _filing_day(report.get('filingDate')) is not None]
        best=dict(min(candidates,key=lambda report:(str(report.get('source_path') or ''),json.dumps(report,sort_keys=True,ensure_ascii=False))))
        # A CIK may have multiple share classes recorded by different source copies.
        best['symbols']=sorted({symbol for report in copies if isinstance(report.get('symbols'),(list,tuple))
                                for symbol in report['symbols'] if isinstance(symbol,str)})
        normalized=normalize_sec(best,companies,as_of=as_of)
        paths=sorted({report.get('source_path') for report in copies if report.get('source_path')})
        for document in normalized:
            document.update(source_paths=paths,source_reception_count=len(copies))
            documents.append(document)
    return documents,{'input_receptions':count,'accessions':len(groups),'selected_company_filings':len(documents),
                       'invalid_accessions':invalid,'future_acceptance_accessions':future,
                       'merged_receptions':sum(len(copies)-1 for copies in groups.values())}


def _price_generation(path):
    match=re.search(r'/snapshot=(\d{8}T\d{6}Z)',path)
    if match: return match.group(1),path
    match=re.search(r'/snapshots/(\d{8})',path)
    if match: return match.group(1)+'T000000Z',path
    raise ValueError('Price source generation cannot be determined: '+path)


def select_price_history(records,companies,source_manifest,*,as_of):
    """Use one completed file per market; report gaps rather than mixing revisions."""
    records=list(records)
    markets={str(c['ticker']):str(c['market']) for c in companies}
    all_markets=set(markets.values())
    source_files={entry['path'] for entry in source_manifest['sources']['prices'] if entry['path'].endswith('.parquet')}
    file_markets={path:set() for path in source_files}
    for path in source_files:
        match=re.search(r'/market=([^/]+)/',path)
        if match: file_markets[path].add(match.group(1))
        elif '/datasets/prices/snapshots/' in path: file_markets[path].update(all_markets)
    for row in records:
        path=row['source_path']; ticker=str(row['ticker'])
        if path not in source_files: raise ValueError('Unpinned price source: '+path)
        market=row.get('market') or markets.get(ticker)
        if ticker in markets and market!=markets[ticker]: raise ValueError('Conflicting price market for '+ticker)
        path_market=re.search(r'/market=([^/]+)/',path)
        if path_market and market and path_market.group(1)!=market:
            raise ValueError('Price row market disagrees with source path: '+path)
        if market: file_markets[path].add(market)
    selected={}
    for market in sorted(all_markets):
        candidates=[path for path in source_files if market in file_markets[path]]
        if candidates: selected[market]=max(candidates,key=_price_generation)
    end=timestamp(as_of); start=end-dt.timedelta(days=90)
    unique={}; skipped=0; duplicates=0; older_keys=set(); selected_keys=set()
    for row in records:
        ticker=str(row['ticker']); market=markets.get(ticker)
        if market is None: continue
        day=_filing_day(row['date'])
        if day is None: raise ValueError('Invalid price date: '+str(row['date']))
        at=timestamp(day,evidence=True)
        if at>=end: continue
        key=ticker,day
        if row['source_path']!=selected.get(market):
            skipped+=1
            if start<=at: older_keys.add(key)
            continue
        output={'ticker':ticker,'market':market,'date':day,'close':row['close_price'],
                'adjusted_close':row.get('adj_close'),'volume':row.get('trading_volume'),
                'source_path':row['source_path'],'provider':row.get('provider'),'price_basis':row.get('price_basis')}
        if key in unique:
            duplicates+=1
            if unique[key]!=output: raise ValueError(f'Conflicting price rows in selected file: {key}')
        unique[key]=output
        if start<=at: selected_keys.add(key)
    missing_dates={}
    for ticker,day in sorted(older_keys-selected_keys): missing_dates.setdefault(ticker,[]).append(day)
    windows={}
    for days in (7,30,90):
        observed={ticker for ticker,day in selected_keys if timestamp(day,evidence=True)>=end-dt.timedelta(days=days)}
        windows[f'{days}D']=sorted(set(markets)-observed)
    summary={'selection_policy':'one_latest_complete_file_per_market_no_revision_mixing',
             'selected_files':selected,'markets_without_source':sorted(all_markets-set(selected)),
             'input_rows':len(records),'selected_rows':len(unique),'duplicate_rows':duplicates,
             'older_source_rows_excluded':skipped,'window_missing_known_tickers':windows,
             'dates_only_available_in_older_sources':missing_dates,
             'note':'Missing selected-history dates remain missing; older adjusted-price bases are not spliced in.'}
    return [unique[key] for key in sorted(unique)],summary


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--inputs',required=True)
    ap.add_argument('--out',required=True)
    ap.add_argument('--companies',required=True)
    ap.add_argument('--aliases',required=True)
    ap.add_argument('--prior-id-map')
    ap.add_argument('--workers',type=int,choices=(1,2),default=1,
                    help='article analysis processes; all publication/output remains in the main process')
    args=ap.parse_args()
    inputs,out=Path(args.inputs),Path(args.out)
    if not (inputs/'_SUCCESS').exists(): raise ValueError('Input export is incomplete')
    source_manifest=json.loads((inputs/'manifest.json').read_text())
    end=timestamp(source_manifest['as_of']); start=end-dt.timedelta(days=90)
    out.mkdir(parents=True,exist_ok=False)
    with Path(args.companies).open(encoding='utf-8-sig') as f: companies=list(csv.DictReader(f))
    with Path(args.aliases).open(encoding='utf-8-sig') as f: aliases=list(csv.DictReader(f))
    manifest={'as_of':source_manifest['as_of'],'period_start':start.isoformat(),
              'schema_version':'cosmos-lake-recent-v1','counts':{},'quarantine_counts':{},
              'analysis_workers':args.workers,
              'source_export_manifest_sha256':digest(inputs/'manifest.json'),
              'companies_sha256':digest(args.companies),'aliases_sha256':digest(args.aliases),
              'limitations':[
                  'Observed archive coverage; not a certification of all published news or filings.',
                  'Retrospective source preparation; collection after as_of is recorded, not a historical backtest.',
                  'Korean rule relations only; English relation extraction is unsupported.',
                  'DART parsed largest-shareholder evidence only; SEC relationship extraction unsupported.',
                  'Company keys are market/stock_code; the RDB loader must resolve its own company UUIDs.',
                  'No source or service DB changes; this stage does not publish.'
              ]}
    cleaned=[]; failures=Counter(); dates={}
    for kind in ['news_prepared','news_realtime','news_live']:
        total=0
        for original in rows(inputs/(kind+'.jsonl')):
            total+=1; row=map_news(original,kind)
            clean=normalize_article(row)
            if clean is None:
                failures[kind+':invalid_text_or_date']+=1; continue
            if not row.get('record_id') or not row.get('source_dataset'):
                raise ValueError('Unmapped source identity: '+kind)
            at=timestamp(clean['published_at'])
            if not start<=at<end:
                failures[kind+':outside_window']+=1; continue
            keep=['record_id','source_dataset','source_path','title','body','url','canonical_url',
                  'publisher','organization','provider','region','language','published_at','published_date','published_at_precision',
                  'published_at_source','collected_at','collected_at_source','content_hash','identity_candidate','quality_flags']
            clean={k:clean.get(k) for k in keep}
            cleaned.append(clean)
            dates.setdefault(kind,Counter())[clean['published_date']]+=1
        manifest['counts'][kind+'_source_rows']=total
    cleaned,receptions=merge_news_receptions(cleaned)
    manifest['source_receptions']=receptions
    prior=list(rows(args.prior_id_map)) if args.prior_id_map else None
    representatives,id_map,dedup=assign_document_ids(cleaned,prior)
    manifest['dedup']=dedup
    manifest['quarantine_counts']=dict(failures)
    manifest['news_daily_source_counts']={k:dict(sorted(v.items())) for k,v in dates.items()}
    write_rows(out/'cleaned_news.jsonl',representatives)
    write_rows(out/'document_id_map.jsonl',id_map)
    print('cleaned',len(representatives),'from',len(cleaned),flush=True)
    del cleaned,id_map

    dart,dart_selection=select_dart_filings(rows(inputs/'dart.jsonl'),companies,aliases,as_of=source_manifest['as_of'])
    sec,sec_selection=select_sec_filings(rows(inputs/'sec.jsonl'),companies,as_of=source_manifest['as_of'])
    filings=[]; disclosure_relations=[]; filing_status=Counter()
    for normalized in dart:
        document=normalized['document']
        if start<=timestamp(document['published_at'])<end:
            filings.append(document)
            disclosure_relations.extend(normalized['relationships'])
    filings.extend(document for document in sec if start<=timestamp(document['published_at'])<end)
    filings.sort(key=lambda document:(document['document_id'],document['ticker']))
    for document in filings: filing_status[document.get('ownership_parse_status','sec_relation_unsupported')]+=1
    write_rows(out/'disclosures.jsonl',filings)
    write_rows(out/'disclosure_relationships.jsonl',disclosure_relations)
    manifest['filing_parse_status']=dict(filing_status)
    manifest['filing_selection']={'dart':dart_selection,'sec':sec_selection}
    affiliate_pairs=sorted({(x['src_ticker'],x['dst_ticker']) for x in disclosure_relations})
    mention_count=relation_count=0; analysis_flags=Counter(); senti=[]
    with (out/'news_mentions.jsonl').open('x',encoding='utf8') as mentions_out, (out/'news_relationships.jsonl').open('x',encoding='utf8') as relations_out:
        analyses=analyze_articles(representatives,args.aliases,affiliate_pairs,workers=args.workers)
        for i,(article,result) in enumerate(zip(representatives,analyses)):
            analysis_flags.update(result['analysis_flags'])
            for mention in result['mentions']:
                mention['document_id']=article['document_id']
                mentions_out.write(json.dumps(mention,ensure_ascii=False)+'\n'); mention_count+=1
            for evidence in result['relationships']:
                evidence.update(record_id=article['document_id'],document_id=article['document_id'],
                                source_record_id=article['record_id'],source='NEWS')
                relations_out.write(json.dumps(evidence,ensure_ascii=False)+'\n'); relation_count+=1
            if result['mentions']:
                senti.append({'document_id':article['document_id'],'title':article['title'],
                              'body':article['body'][:200],'region':article['region'],
                              'language':article['language']})
            if i%5000==0: print('analyzed',i,'mentions',mention_count,'relations',relation_count,flush=True)
    write_rows(out/'sentiment_inputs.jsonl',senti)

    prices,price_selection=select_price_history(rows(inputs/'prices.jsonl'),companies,source_manifest,as_of=source_manifest['as_of'])
    write_rows(out/'prices.jsonl',prices)
    manifest['price_duplicate_rows']=price_selection['duplicate_rows']
    manifest['price_selection']=price_selection
    manifest['analysis_flags']=dict(analysis_flags)
    manifest['counts'].update(cleaned_news=len(representatives),news_mentions=mention_count,
                             news_relationships=relation_count,disclosures=len(filings),
                             disclosure_relationships=len(disclosure_relations),prices=len(prices),
                             sentiment_inputs=len(senti))
    manifest['files']={p.name:{'bytes':p.stat().st_size,'sha256':digest(p)} for p in out.glob('*.jsonl')}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    (out/'_PREPARED').write_text('evidence prepared; sentiment and publication pending\n')
    print(json.dumps(manifest['counts']),flush=True)


if __name__=='__main__':
    main()
