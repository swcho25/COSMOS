"""Read-only distributions for an explicitly partial provisional dataset.

This audits input availability and concentration, not semantic correctness or
price-prediction performance. Company listing market is not news source region.
Language is a declared script heuristic over the first 1,000 text characters.
Dates are counted only when an unambiguous leading YYYY-MM-DD variant exists;
timezone/market session availability is intentionally not invented here.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent

def leading_date(value):
    match = re.match(r'^\s*(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?:\D|$)', value or '')
    if not match:
        return None
    try:
        return date(*(int(x) for x in match.groups()))
    except ValueError:
        return None

def language_hint(text):
    head = text[:1000]
    ko = len(re.findall(r'[가-힣]', head))
    en = len(re.findall(r'[A-Za-z]', head))
    if ko >= 20 and ko / max(1, ko + en) >= .05:
        return 'korean_script'
    if en >= 80 and ko < 10:
        return 'latin_script'
    return 'undetermined'

def body_bucket(size):
    for upper, label in ((200, '0-199'), (1000, '200-999'), (3000, '1000-2999'), (10000, '3000-9999')):
        if size < upper:
            return label
    return '10000+'

def audit(merged_dir, output, companies_path):
    merged_dir, output = merged_dir.resolve(), output.resolve()
    if output.exists():
        raise ValueError('Use a new output file')
    path = merged_dir / 'state.sqlite3'
    receipt_path = merged_dir / 'partial_receipt.json'
    if not receipt_path.is_file():
        raise ValueError('A verified partial-artifact receipt is required')
    receipt = json.loads(receipt_path.read_text(encoding='utf8'))
    if receipt.get('artifact_complete') is not True or receipt.get('full_population_complete') is not False:
        raise ValueError('Expected a completed artifact for an explicitly partial population')
    status = json.loads((merged_dir / 'status.json').read_text(encoding='utf8'))
    if (status.get('stage') != 'partial_artifact_complete'
            or status.get('artifact_complete') is not True
            or status.get('full_population_complete') is not False
            or status.get('processed_unique_pairs') != receipt.get('covered_unique_pairs')
            or status.get('decisions') != receipt.get('decisions')):
        raise ValueError('Both final status and receipt must agree before auditing')
    if not path.is_file() or (Path(str(path)+'-wal').exists() and Path(str(path)+'-wal').stat().st_size):
        raise ValueError('A closed and checkpointed merge database is required')
    companies = {r['ticker']: r for r in csv.DictReader(companies_path.open(encoding='utf-8-sig'))}
    dimensions = {name: defaultdict(Counter) for name in ('year', 'company_market', 'company_region', 'ticker',
                  'language_hint', 'body_chars', 'url_host', 'component')}
    defects, decisions = defaultdict(Counter), Counter()
    date_bounds = {}
    seen = 0
    with sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True) as db:
        columns = {r[1] for r in db.execute('PRAGMA table_info(pairs)')}
        required = {'article_id', 'ticker', 'decision', 'component', 'input_json'}
        if not required <= columns:
            raise ValueError('Missing merged source columns: '+str(required-columns))
        for article, ticker, decision, component, raw in db.execute('SELECT article_id,ticker,decision,component,input_json FROM pairs ORDER BY rowid'):
            source = json.loads(raw)
            if source['article_id'] != article or source['ticker'] != ticker:
                raise ValueError('Input identity differs from merged key')
            text = source['title']+'\n'+source['body']
            when = leading_date(source['published_at'])
            market = companies.get(ticker, {}).get('market', 'UNKNOWN')
            region = 'KR_listed_company' if market in ('KOSPI','KOSDAQ','KRX') else 'US_listed_company' if market in ('NASDAQ','NYSE','AMEX') else 'UNKNOWN'
            try:
                host = urlsplit(source['source_url']).hostname or 'MISSING'
            except ValueError:
                host = 'INVALID'
            values = {'year': str(when.year) if when else 'UNPARSED', 'company_market':market,
                      'company_region':region, 'ticker':ticker, 'language_hint':language_hint(text),
                      'body_chars':body_bucket(len(source['body'])), 'url_host':host, 'component':component}
            for key, value in values.items():
                dimensions[key][decision][value] += 1
            decisions[decision] += 1
            if when:
                lo, hi = date_bounds.get(decision, (when, when))
                date_bounds[decision] = (min(lo,when), max(hi,when))
                if when > date(2026,9,18):
                    defects[decision]['future_calendar_date'] += 1
                if when.year < 2000:
                    defects[decision]['pre_2000_calendar_date_check_required'] += 1
            else:
                defects[decision]['date_not_parsed'] += 1
            if not source['body'].strip(): defects[decision]['empty_body'] += 1
            if market == 'UNKNOWN': defects[decision]['unknown_company'] += 1
            if host in ('MISSING','INVALID'): defects[decision]['missing_or_invalid_url'] += 1
            for fragment, tag in (('재무분석차트영역상세보기','chart_boilerplate_fragment'),
                                  ('<script','html_script_fragment'), ('<iframe','html_iframe_fragment')):
                if fragment in text: defects[decision][tag] += 1
            seen += 1
            if seen % 250000 == 0:
                print(json.dumps({'stage':'distribution_scan','pairs':seen}),flush=True)
        unique_articles = db.execute('SELECT COUNT(DISTINCT article_id) FROM pairs').fetchone()[0]
        accepted_articles = db.execute("SELECT COUNT(DISTINCT article_id) FROM pairs WHERE decision='include'").fetchone()[0]
        accepted_per_article = dict(db.execute("SELECT n,COUNT(*) FROM (SELECT COUNT(*) n FROM pairs WHERE decision='include' GROUP BY article_id) GROUP BY n"))
        mixed_decision_articles = db.execute('SELECT COUNT(*) FROM (SELECT article_id FROM pairs GROUP BY article_id HAVING COUNT(DISTINCT decision)>1)').fetchone()[0]
    if seen != receipt.get('covered_unique_pairs') or dict(decisions) != receipt.get('decisions'):
        raise ValueError('Audit counts differ from the verified partial-artifact receipt')
    top = dimensions['ticker']['include'].most_common(15)
    result = {'schema':'cosmos-partial-provisional-distribution-audit-1', 'processed_pairs':seen,
              'decisions':dict(decisions), 'processed_unique_articles':unique_articles,
              'include_unique_articles':accepted_articles, 'include_pairs_per_article':accepted_per_article,
              'mixed_decision_articles':mixed_decision_articles,
              'dimensions':{k:{d:dict(c) for d,c in v.items()} for k,v in dimensions.items()},
              'date_bounds':{k:[a.isoformat(),b.isoformat()] for k,(a,b) in date_bounds.items()},
              'potential_input_issues':{k:dict(v) for k,v in defects.items()},
              'top_include_companies':[{'ticker':t,'name':companies.get(t,{}).get('name_official'), 'pairs':n} for t,n in top],
              'top10_include_company_share':sum(n for t,n in top[:10])/max(1,decisions['include']),
              'news_source_region_available':False,
              'language_hint_method':'script counts in first 1000 title+body characters; not verified article language or country',
              'date_method':'leading unambiguous YYYY-MM-DD, YYYY.MM.DD or YYYY/MM/DD only; no timezone/session inference',
              'market_metadata_sha256':hashlib.sha256(companies_path.read_bytes()).hexdigest(),
              'human_gold':False, 'semantic_quality_approved':False, 'production_approved':False}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x',encoding='utf8') as stream: json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k not in ('dimensions','potential_input_issues')},ensure_ascii=False),flush=True)
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--merged-dir',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--companies',type=Path,default=HERE.parent/'ner/data/companies.csv')
    args = parser.parse_args()
    audit(args.merged_dir,args.out,args.companies)
