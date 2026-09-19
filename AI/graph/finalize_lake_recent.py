"""Join measured article sentiment, aggregate actual evidence, and validate a batch.

No HDFS or database writes happen here. The generated publication.json contains
explicit Parquet schemas and immutable file hashes for publish_lake_recent.py.
Sentiment is title/lead sentiment, not a company price-direction prediction.
Company/relationship natural keys must be resolved by a later RDB loader.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import shutil
import uuid

from lake_window_features import build_features
from relationship_score_components import timestamp, SCORE_FIELDS


def read(path):
    with Path(path).open(encoding='utf8') as f:
        return [json.loads(line) for line in f if line.strip()]


def sha(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''): result.update(part)
    return result.hexdigest()


def write(path,records):
    with Path(path).open('x',encoding='utf8') as f:
        for row in records: f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')


DOUBLES={'news_score','disclosure_score','score','confidence','sentiment_score','sentiment_coverage',
         'price_return','price_volatility','last_close','last_adjusted_close','price_correlation',
         'news_confidence_sum','disclosure_confidence_sum','share_pct','weight'}
LONGS={'positive_count','negative_count','neutral_count','news_mention_count','relationship_count',
       'disclosure_count','sentiment_analyzed_count','sentiment_labeled_count','sentiment_scored_count',
       'evidence_count','price_observation_count','price_return_observation_count','price_aligned_return_count',
       'news_evidence_count','disclosure_evidence_count','duplicate_count'}


def empty_schema(name):
    """An observed empty relationship batch has a real schema and zero rows."""
    score_fields=list(SCORE_FIELDS)+['period_start','period_end']
    definitions={
        'news_relationships':['document_id','record_id','source_record_id','published_at','published_date',
                              'src_ticker','dst_ticker','rel_type','confidence','pattern','sentence',
                              'sentence_start','sentence_end','source_start','source_end','target_start','target_end',
                              'offset_basis','source'],
        'relationship_scores':score_fields,
        'relationship_features':score_fields+['feature_version',
            'news_evidence_count','news_confidence_sum','disclosure_evidence_count','disclosure_confidence_sum',
            'price_correlation','price_aligned_return_count','price_correlation_status'],
        'graph_edges':score_fields+['relationship_key','snapshot_id'],
    }
    if name not in definitions: raise ValueError('No empty-dataset contract: '+name)
    return {'type':'struct','fields':[{'name':k,'type':'double' if k in DOUBLES else
                                      'long' if k in LONGS or k.endswith(('_start','_end')) and k not in ['period_start','period_end'] else 'string',
                                      'nullable':True,'metadata':{}} for k in sorted(definitions[name])]}


def schema(records):
    """Produce a concrete schema, with semantic overrides for nullable numerics."""
    def infer(values,name=''):
        present=[v for v in values if v is not None]
        if name in DOUBLES: return 'double'
        if name in LONGS: return 'long'
        if name=='probs3': return {'type':'array','elementType':'double','containsNull':True}
        if not present: return 'string'
        if all(isinstance(v,bool) for v in present): return 'boolean'
        if all(isinstance(v,int) and not isinstance(v,bool) for v in present): return 'long'
        if all(isinstance(v,(int,float)) and not isinstance(v,bool) for v in present): return 'double'
        if all(isinstance(v,str) for v in present): return 'string'
        if all(isinstance(v,list) for v in present):
            return {'type':'array','elementType':infer([x for v in present for x in v]),'containsNull':True}
        if all(isinstance(v,dict) for v in present):
            return {'type':'struct','fields':[{'name':k,'type':infer([v.get(k) for v in present],k),'nullable':True,'metadata':{}}
                                            for k in sorted(set().union(*(v.keys() for v in present)))]}
        raise ValueError('Mixed incompatible schema values: '+name)
    if not records: raise ValueError('Explicit empty-dataset schema required')
    return infer(records)


def independent_score_check(scores,relations,as_of):
    end=timestamp(as_of); expected={}
    for days in [7,30,90]:
        docs=defaultdict(dict)
        for r in relations:
            if not end-dt.timedelta(days=days)<=timestamp(r['published_at'])<end: continue
            a,b=r['src_ticker'],r['dst_ticker']
            if r['rel_type'] in ['PARTNER','COMPETE']: a,b=sorted([a,b])
            key=a,b,r['rel_type'],r['source']
            docs[key][r['record_id']]=max(docs[key].get(r['record_id'],0),float(r['confidence']))
        for a,b,kind,_ in docs:
            key=a,b,kind,f'{days}D'
            if key in expected: continue
            parts=[]
            for source in ['NEWS','DISCLOSURE']:
                values=docs.get((a,b,kind,source))
                parts.append(round(100*(1-math.exp(-math.fsum(values.values())/5)),6) if values else None)
            valid=[x for x in parts if x is not None]
            expected[key]=(*parts,round(sum(valid)/len(valid),6))
    if len(scores)!=len(expected): raise ValueError('Independent relationship row count differs')
    for r in scores:
        key=r['source_stock_code'],r['target_stock_code'],r['relationship_type_code'],r['window_type']
        target=expected[key]
        for field,value in zip(['news_score','disclosure_score','score'],target):
            actual=r[field]
            if (actual is None)!=(value is None) or (value is not None and abs(actual-value)>1.1e-6):
                raise ValueError('Independent score disagreement: '+str((key,field,actual,value)))


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--prepared',required=True)
    ap.add_argument('--sentiment',required=True)
    ap.add_argument('--companies',required=True)
    ap.add_argument('--out',required=True)
    ap.add_argument('--run-id',required=True)
    args=ap.parse_args()
    base,out=Path(args.prepared),Path(args.out)
    if not (base/'_PREPARED').exists(): raise ValueError('Evidence preparation incomplete')
    provenance=json.loads((base/'manifest.json').read_text(encoding='utf8'))
    for name,expected in provenance['files'].items():
        if sha(base/name)!=expected['sha256']: raise ValueError('Prepared input hash changed: '+name)
    if sha(args.companies)!=provenance['companies_sha256']: raise ValueError('Company universe changed')
    report=json.loads(Path(str(args.sentiment)+'.report.json').read_text(encoding='utf8'))
    # SHA field names are read explicitly below; unsupported contracts fail.
    input_hash=report.get('input_sha256') or report.get('input',{}).get('sha256')
    output_hash=report.get('output_sha256') or report.get('output',{}).get('sha256')
    if input_hash!=sha(base/'sentiment_inputs.jsonl') or output_hash!=sha(args.sentiment):
        raise ValueError('Sentiment input/output hash mismatch')
    out.mkdir(parents=True,exist_ok=False)
    with Path(args.companies).open(encoding='utf-8-sig') as f: companies=list(csv.DictReader(f))
    tickers={c['ticker'] for c in companies}; markets={c['ticker']:c['market'] for c in companies}
    articles=read(base/'cleaned_news.jsonl'); by_id={a['document_id']:a for a in articles}
    if len(by_id)!=len(articles): raise ValueError('Duplicate cleaned document')
    mentions=read(base/'news_mentions.jsonl')
    news_relations=read(base/'news_relationships.jsonl')
    filings=read(base/'disclosures.jsonl'); disclosure_relations=read(base/'disclosure_relationships.jsonl')
    sentiment=read(args.sentiment); senti={x['document_id']:x for x in sentiment}
    wanted={x['document_id'] for x in read(base/'sentiment_inputs.jsonl')}
    if len(senti)!=len(sentiment) or set(senti)!=wanted: raise ValueError('Incomplete/duplicate sentiment results')
    for row in mentions:
        if row['document_id'] not in by_id or row['ticker'] not in tickers: raise ValueError('Invalid mention reference')
        s=senti[row['document_id']]
        row.update(sentiment_label=s['label'],sentiment_score=s['score'],sentiment_status=s['status'],
                   sentiment_scope='article_title_lead')
        if s['status']=='ok':
            if not -1<=s['score']<=1 or abs(sum(s['probs3'])-1)>1e-5: raise ValueError('Invalid sentiment probability')
    filing_ids={x['document_id'] for x in filings}
    for r in news_relations:
        a=by_id[r['document_id']]; text=a['title']+'\n'+a['body']
        if text[r['sentence_start']:r['sentence_end']]!=r['sentence']: raise ValueError('News quote offset mismatch')
        if r['published_at']!=a['published_at']: raise ValueError('News evidence time mismatch')
    for r in disclosure_relations:
        if r['record_id'] not in filing_ids: raise ValueError('Disclosure evidence missing filing')
    relations=news_relations+disclosure_relations
    for r in relations:
        if r['src_ticker'] not in tickers or r['dst_ticker'] not in tickers or r['src_ticker']==r['dst_ticker']:
            raise ValueError('Invalid relationship company reference')
    features=build_features(companies,mentions,relations,filings,read(base/'prices.jsonl'),provenance['as_of'])
    independent_score_check(features['relationship_scores'],relations,provenance['as_of'])
    if len(features['company_metrics'])!=3*len(companies): raise ValueError('Incomplete company windows')
    for row in features['company_metrics']:
        end=timestamp(row['measured_at']); start=end-dt.timedelta(days=int(row['window_type'][:-1]))
        observed={m['document_id'] for m in mentions if m['ticker']==row['stock_code'] and start<=timestamp(m['published_at'])<end}
        if len(observed)!=row['news_mention_count']: raise ValueError('Independent company count mismatch')
    snapshot_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'urn:cosmos:graph-snapshot:'+args.run_id))
    nodes=[{'stock_code':c['ticker'],'market':c['market'],'name':c['name_official'],
            'name_en':c.get('name_en'),'sector':c.get('sector'),'snapshot_id':snapshot_id} for c in companies]
    edges=[]
    for r in features['relationship_scores']:
        key=':'.join(str(r[k]) for k in ['source_market','source_stock_code','target_market','target_stock_code','relationship_type_code'])
        edge={**r,'relationship_key':key,'snapshot_id':snapshot_id}
        edges.append(edge)
    datasets=[]
    def dataset(name,records,root,keys,path='data'):
        file=out/(name+'.jsonl'); write(file,records)
        datasets.append({'name':name,'local_file':file.name,'target_root':root,'dataset_path':path,
                         'schema':schema(records) if records else empty_schema(name),'key_columns':keys,'row_count':len(records),
                         'source_sha256':sha(file)})
    dataset('cleaned_news',articles,'/data-lake/cleaned/news',['document_id'])
    dataset('news_relationships',news_relations,'/data-lake/analyzed/news/relationships',
            ['document_id','src_ticker','dst_ticker','rel_type','sentence_start','source_start','target_start'])
    for name,root,keys in [
        ('company_features','/data-lake/features/company',['market','stock_code','window_type']),
        ('relationship_features','/data-lake/features/relationship',['source_market','source_stock_code','target_market','target_stock_code','relationship_type_code','window_type']),
        ('company_metrics','/data-lake/aggregated/company-metrics',['market','stock_code','window_type']),
        ('relationship_scores','/data-lake/aggregated/relationship-scores',['source_market','source_stock_code','target_market','target_stock_code','relationship_type_code','window_type'])]:
        dataset(name,features[name],root,keys)
    dataset('graph_nodes',nodes,'/data-lake/snapshots/graph',['market','stock_code'],'nodes')
    dataset('graph_edges',edges,'/data-lake/snapshots/graph',['relationship_key','window_type'],'edges')
    dataset('document_id_map',read(base/'document_id_map.jsonl'),'/data-lake/metadata/id-maps/news',['identity_key'])
    dataset('news_mentions',mentions,'/data-lake/analyzed/news/company-mentions',['document_id','ticker'])
    dataset('news_sentiment',sentiment,'/data-lake/analyzed/news/sentiment',['document_id'])
    dataset('disclosure_mentions',filings,'/data-lake/analyzed/disclosures/company-mentions',['document_id','ticker'])
    if disclosure_relations:
        dataset('disclosure_relationships',disclosure_relations,'/data-lake/analyzed/disclosures/relationships',
                ['record_id','src_ticker','dst_ticker','rel_type','evidence_file_index','evidence_start','evidence_end'])
    publication={'run_id':args.run_id,'as_of':provenance['as_of'],'snapshot_id':snapshot_id,
                 'status':'LOCAL_VALIDATED_NOT_PUBLISHED','datasets':datasets,
                 'limitations':provenance['limitations']+[
                     'Sentiment is article title/lead sentiment shared by its mentioned companies; not company-specific price direction.',
                     'Unsupported sentiment languages remain NULL; inspect sentiment_coverage.',
                     'Price features require adjusted closes. Missing adjusted history and cross-market alignment remain NULL.',
                     'This is a single bounded batch, not a scheduled continuous-refresh pipeline.'
                 ],'preparation':provenance,'sentiment_report':report,
                 'validation':{'independent_score_formula':True,'independent_company_counts':True,
                               'news_quote_offsets':True,'company_and_document_references':True,
                               'source_hashes':True,'company_windows':len(features['company_metrics'])}}
    (out/'publication.json').write_text(json.dumps(publication,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'datasets':{d['name']:d['row_count'] for d in datasets},'snapshot_id':snapshot_id}),flush=True)


if __name__=='__main__': main()
