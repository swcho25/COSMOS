"""Build a resumable LOCAL candidate knowledge graph from provisional B includes.

This consumes the verified partial snapshot, not the original stopped workers.
It preserves B decisions and original article text. Separate deterministic
grounding decisions, target-context FinBERT outputs, and explicitly worded
company relations remain provisional, not human gold or proven price effects.
There are no HDFS, service DB, network, training, or deployment operations.

SQLite FULL/WAL transactions commit outputs and cursors together. A STOP file
pauses after the current batch. Resume requires identical code, source, models,
dictionaries and settings. A bounded --limit-pairs pilot is a separate artifact.
The final manifest covers only that declared input scope, never all raw news.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

import knowledge_graph_news_preview as preview

HERE = Path(__file__).resolve().parent
SCHEMA = 'cosmos-grounded-candidate-kg-1'
FLAGS = {'human_gold':False, 'production_approved':False, 'graph_quality_validated':False,
         'ready_for_gnn_training':False, 'price_effect_established':False,
         'scope':'local_provisional_evidence_backed_candidate_graph'}
PROCESSOR = None

def now(): return datetime.now(timezone.utc).isoformat()
def canonical(value): return preview.canonical(value)
def digest(value): return hashlib.sha256(canonical(value).encode('utf8')).hexdigest()
def read(path): return json.loads(Path(path).read_text(encoding='utf8'))
def sha(path): return preview.file_sha256(path)

def atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.pending')
    with temporary.open('x',encoding='utf8',newline='\n') as stream:
        stream.write(canonical(value)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    # Windows readers/indexers can briefly deny delete-sharing on status.json.
    # Retry only access/sharing failures, keeping the old complete file until
    # replacement succeeds. Persistent failures still propagate after 5 s.
    deadline=time.monotonic()+5.0
    delay=0.02
    while True:
        try:
            os.replace(temporary,path)
            break
        except PermissionError as error:
            if error.errno!=13 and getattr(error,'winerror',None) not in {5,32,33}:
                raise
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise
            time.sleep(min(delay,remaining))
            delay=min(0.2,delay*2)

@contextmanager
def run_lock(out):
    import msvcrt
    path = out/'run.lock'
    handle = path.open('a+b')
    try:
        handle.seek(0,2)
        if handle.tell() == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try: msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        except OSError as exc: raise RuntimeError('Another builder holds this output lock') from exc
        try: yield
        finally:
            handle.seek(0); msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
    finally: handle.close()

def initialize_processor():
    global PROCESSOR
    from kg_grounded_news import GroundedNewsProcessor
    PROCESSOR = GroundedNewsProcessor(historical_registry=read(HERE/'data/historical_company_context_v1.json'))

def ground(row):
    global PROCESSOR
    if PROCESSOR is None: initialize_processor()
    return PROCESSOR.process(row)

def meta(db,key,default=None):
    row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default

def put(db,key,value):
    db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,canonical(value)))

def schema(db):
    db.executescript('''
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE articles(article_id TEXT PRIMARY KEY,source_sha TEXT NOT NULL,cleaned_sha TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE pairs(article_id TEXT,ticker TEXT,decision TEXT NOT NULL,source_rowid INTEGER UNIQUE,
            payload TEXT NOT NULL,PRIMARY KEY(article_id,ticker));
        CREATE INDEX pairs_decision ON pairs(decision);
        CREATE TABLE sentiments(article_id TEXT,ticker TEXT,payload TEXT NOT NULL,PRIMARY KEY(article_id,ticker));
        CREATE TABLE relations(edge_id TEXT PRIMARY KEY,article_id TEXT,src_ticker TEXT,dst_ticker TEXT,
            rel_type TEXT,directed INTEGER,payload TEXT NOT NULL);
        CREATE TABLE relation_reviews(article_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
    ''')
    for key,value in [('stage','prepare'),('source_cursor',0),('relation_cursor',0),('sentiment_cursor',0),
                      ('prepared_pairs',0),('decision_counts',{}),('sentiment_counts',{}),
                      ('relation_counts',{}),('relation_articles',0),('sentiment_pairs',0)]: put(db,key,value)
    db.commit()

def status(out,db,state='running',**extra):
    value={key:meta(db,key) for key in ('stage','prepared_pairs','decision_counts','sentiment_counts',
                                      'relation_counts','relation_articles','sentiment_pairs')}
    value.update(schema=SCHEMA,state=state,updated_at=now(),**FLAGS,**extra)
    atomic(out/'status.json',value)
    print(canonical(value),flush=True)

def source_contract(merged):
    receipt=read(merged/'partial_receipt.json'); state=read(merged/'status.json')
    preview.require(receipt.get('artifact_complete') is True and state.get('stage')=='partial_artifact_complete'
                    and state.get('artifact_complete') is True, 'Partial source is not complete')
    preview.require(state['decisions']==receipt['decisions'], 'Source counters disagree')
    path=merged/'state.sqlite3'
    wal=Path(str(path)+'-wal')
    preview.require(not wal.exists() or wal.stat().st_size==0,'Source must be closed/checkpointed')
    preview.require(sha(path)==receipt['state_sqlite_sha256'],'Source database checksum differs')
    for filename,key in [('coverage.json','coverage_sha256'),('protocol.json','protocol_sha256')]:
        preview.require(sha(merged/filename)==receipt[key],'Source metadata checksum differs')
    return {'directory':str(merged),'receipt_sha256':sha(merged/'partial_receipt.json'),
            'db_sha256':receipt['state_sqlite_sha256'],'include_pairs':receipt['decisions']['include'],
            'parent_covered_pairs':receipt['covered_unique_pairs'],
            'parent_unprocessed_pairs':receipt['unprocessed_unique_pairs'],
            'full_population_complete':receipt['full_population_complete']}

def code_and_assets():
    # Import every executing local adapter before enumerating its dependencies.
    import kg_grounded_news, kg_grounded_relations, kg_target_sentiment
    initialize_processor()
    files={Path(__file__).resolve()}
    ai=HERE.parent.resolve()
    for module in tuple(sys.modules.values()):
        value=getattr(module,'__file__',None)
        if value:
            path=Path(value).resolve()
            if path.is_file() and path.suffix=='.py' and path.is_relative_to(ai) and '.venv' not in path.parts:
                files.add(path)
    files.update([ai/'ner/data/companies.csv',ai/'ner/data/aliases.csv',HERE/'data/historical_company_context_v1.json'])
    return {str(path):sha(path) for path in sorted(files)}

def runtime_versions():
    packages={}
    for name in ('torch','transformers','tokenizers','safetensors','numpy','pyahocorasick'):
        try: packages[name]=metadata.version(name)
        except metadata.PackageNotFoundError: packages[name]=None
    return {'python':sys.version,'sqlite':sqlite3.sqlite_version,'packages':packages}

def maintenance_receipts(out):
    """Expose explicitly audited non-semantic maintenance in the frozen run."""
    return [{'path':p.relative_to(out).as_posix(),'sha256':sha(p)}
            for p in sorted((out/'maintenance').glob('*/receipt.json'))]

def ensure_bindings(bindings):
    for path,expected in bindings.items():
        preview.require(sha(path)==expected,'Code or asset changed: '+path)

def validate_checkpoint(db, expected):
    """Check transactional cursors, referential coverage and stored counters."""
    preview.require(db.execute('PRAGMA quick_check').fetchall()==[('ok',)],'SQLite integrity check failed')
    stage=meta(db,'stage')
    preview.require(stage in {'prepare','relations','sentiment','export','complete'},'Unknown checkpoint stage')
    prepared,cursor=db.execute('SELECT COUNT(*),COALESCE(MAX(source_rowid),0) FROM pairs').fetchone()
    preview.require(prepared==meta(db,'prepared_pairs') and cursor==meta(db,'source_cursor') and prepared<=expected,
                    'Prepared count or source cursor differs')
    counts=dict(db.execute('SELECT decision,COUNT(*) FROM pairs GROUP BY decision'))
    preview.require(counts==meta(db,'decision_counts'),'Decision counters differ')
    invalid=db.execute("""SELECT COUNT(*) FROM pairs WHERE json_extract(payload,'$.article_id') IS NOT article_id
        OR json_extract(payload,'$.ticker') IS NOT ticker OR json_extract(payload,'$.materialization_decision') IS NOT decision
        OR article_id NOT IN (SELECT article_id FROM articles)""").fetchone()[0]
    preview.require(invalid==0,'Pair identities or article references differ')
    rc=meta(db,'relation_cursor')
    processed_articles=db.execute('SELECT COUNT(*) FROM articles WHERE rowid<=?',(rc,)).fetchone()[0]
    preview.require(processed_articles==meta(db,'relation_articles'),'Relation article cursor differs')
    preview.require(dict(db.execute('SELECT rel_type,COUNT(*) FROM relations GROUP BY rel_type'))==meta(db,'relation_counts'),
                    'Relation counters differ')
    dangling=db.execute("""SELECT COUNT(*) FROM relations r
        LEFT JOIN articles a USING(article_id)
        LEFT JOIN pairs s ON s.article_id=r.article_id AND s.ticker=r.src_ticker
        LEFT JOIN pairs t ON t.article_id=r.article_id AND t.ticker=r.dst_ticker
        WHERE a.rowid IS NULL OR a.rowid>? OR s.decision IS NULL OR t.decision IS NULL
        OR s.decision!='candidate_ready' OR t.decision!='candidate_ready'""",(rc,)).fetchone()[0]
    preview.require(dangling==0,'Relationship has an unprocessed or unselected endpoint')
    sc=meta(db,'sentiment_cursor')
    n=db.execute('SELECT COUNT(*) FROM sentiments').fetchone()[0]
    eligible=db.execute("SELECT COUNT(*) FROM pairs WHERE decision='candidate_ready' AND rowid<=?",(sc,)).fetchone()[0]
    preview.require(n==meta(db,'sentiment_pairs')==eligible,'Sentiment coverage or cursor differs')
    mood_counts=dict(db.execute("SELECT json_extract(payload,'$.status'),COUNT(*) FROM sentiments GROUP BY 1"))
    preview.require(mood_counts==meta(db,'sentiment_counts'),'Sentiment counters differ')
    bad_mood=db.execute("""SELECT COUNT(*) FROM sentiments s LEFT JOIN pairs p USING(article_id,ticker)
        WHERE p.rowid IS NULL OR p.rowid>? OR p.decision!='candidate_ready'
        OR json_extract(s.payload,'$.article_id') IS NOT s.article_id OR json_extract(s.payload,'$.ticker') IS NOT s.ticker
        OR json_extract(s.payload,'$.status') IS NULL
        OR (json_extract(s.payload,'$.status')!='analyzed' AND json_extract(s.payload,'$.label') IS NOT NULL)
        OR (json_extract(s.payload,'$.status')='analyzed' AND COALESCE(json_extract(s.payload,'$.label'),'') NOT IN ('POSITIVE','NEGATIVE','NEUTRAL'))""",(sc,)).fetchone()[0]
    preview.require(bad_mood==0,'Sentiment identity, eligibility or NULL semantics differ')
    if stage!='prepare': preview.require(prepared==expected,'Advanced stage has incomplete grounding')
    if stage in {'sentiment','export','complete'}:
        preview.require(processed_articles==db.execute('SELECT COUNT(*) FROM articles').fetchone()[0],
                        'Advanced stage has incomplete relations')
    if stage in {'export','complete'}: preview.require(n==counts.get('candidate_ready',0),'Export has incomplete sentiment')

def verify_manifest(out, protocol, manifest):
    preview.require(manifest.get('schema')==SCHEMA and manifest.get('artifact_complete') is True
                    and manifest.get('protocol_sha256')==sha(out/'protocol.json')
                    and manifest.get('input_pairs')==protocol['expected_pairs']
                    and manifest.get('full_population_complete') is False
                    and manifest.get('graph_materialized') is True
                    and manifest.get('bounded_pilot')==(protocol['settings']['limit_pairs'] is not None),
                    'Final manifest contract differs')
    preview.require(all(manifest.get(k)==v for k,v in FLAGS.items()),'Final manifest scope or validation flags differ')
    expected_maintenance=protocol.get('maintenance_receipts',[])
    preview.require(manifest.get('execution_maintenance',[])==expected_maintenance
                    and maintenance_receipts(out)==expected_maintenance,'Maintenance provenance differs')
    preview.require(sha(out/'state.sqlite3')==manifest['state_sqlite_sha256'],'Completed output database changed')
    required={'selection_audit','news_nodes','company_nodes','news_company_edges','relationship_evidence','company_relationships','relation_review'}
    preview.require(set(manifest.get('files',{}))==required,'Final manifest is missing export files')
    preview.require(sum(manifest['decision_counts'].values())==protocol['expected_pairs']
                    and manifest['files']['selection_audit']['rows']==protocol['expected_pairs']
                    and manifest['files']['news_company_edges']['rows']==manifest['decision_counts'].get('candidate_ready',0)
                    and sum(manifest['sentiment_counts'].values())==manifest['files']['news_company_edges']['rows']
                    and sum(manifest['relation_counts'].values())==manifest['files']['relationship_evidence']['rows'],
                    'Final manifest coverage differs')
    for item in manifest['files'].values():
        path=(out/item['path']).resolve()
        preview.require(path.is_relative_to(out.resolve()) and path.is_file(),'Invalid export path')
        preview.require(path.stat().st_size==item['bytes'] and sha(path)==item['sha256'],'Completed export changed')
        with path.open('rb') as stream:
            preview.require(sum(1 for _ in stream)==item['rows'],'Completed export row count differs')

def article_payload(row,result):
    original={k:row[k] for k in ('article_id','title','body','published_at','source_url')}
    value={**original,'node_id':'news:'+row['article_id'],'node_type':'news',
           'cleaned_title':result['cleaned_title'],'cleaned_body':result['cleaned_body'],
           'cleaned_text_sha256':result['cleaned_text_sha256'],'date_audit':result['date_audit'],
           'cleaning_audit':result['cleaning_audit'],'event_time':None,'event_time_status':'not_extracted',**FLAGS}
    return value,digest(original)

def validate_grounding(row,result):
    preview.require(result['article_id']==row['article_id'] and result['ticker']==row['ticker'],'Grounding identity differs')
    preview.require(result['source_input_sha256']==preview.canonical_input_sha256(row),'Grounding source hash differs')
    decision=result['materialization_decision']
    preview.require(decision in {'candidate_ready','exclude','review'},'Unknown grounding decision')
    for evidence in result['evidence']:
        field=evidence['source']; start,end=evidence['start'],evidence['end']
        preview.require(field in {'title','body'} and type(start) is int and type(end) is int
                        and 0<=start<end<=len(row[field]),'Invalid original evidence span')
        preview.require(row[field][start:end]==evidence['quote'],'Evidence quote differs from original')
    if decision=='candidate_ready': preview.require(bool(result['evidence']),'Ready pair lacks evidence')

def prepare(db,source,out,protocol,workers,batch_size):
    expected=protocol['expected_pairs']
    pool=ProcessPoolExecutor(max_workers=workers,initializer=initialize_processor) if workers>1 else None
    try:
        while meta(db,'prepared_pairs')<expected:
            if (out/'STOP').exists(): return False
            amount=min(batch_size,expected-meta(db,'prepared_pairs'))
            records=source.execute("SELECT rowid,input_sha256,result_sha256,input_json FROM pairs WHERE decision='include' AND rowid>? ORDER BY rowid LIMIT ?",
                                   (meta(db,'source_cursor'),amount)).fetchall()
            preview.require(bool(records),'Source exhausted before declared include count')
            inputs=[json.loads(record[3]) for record in records]
            for record,row in zip(records,inputs):
                preview.require(preview.canonical_input_sha256(row)==record[1],'Source input hash differs')
            results=list(pool.map(ground,inputs,chunksize=16)) if pool else [ground(row) for row in inputs]
            counts=Counter(meta(db,'decision_counts'))
            with db:
                for record,row,result in zip(records,inputs,results):
                    validate_grounding(row,result)
                    article,source_sha=article_payload(row,result)
                    old=db.execute('SELECT source_sha,cleaned_sha FROM articles WHERE article_id=?',(row['article_id'],)).fetchone()
                    if old:
                        preview.require(old==(source_sha,result['cleaned_text_sha256']),'Conflicting content or cleaning for one article')
                    else:
                        db.execute('INSERT INTO articles VALUES (?,?,?,?)',(row['article_id'],source_sha,result['cleaned_text_sha256'],canonical(article)))
                    pair={k:v for k,v in result.items() if k not in {'cleaned_title','cleaned_body','cleaning_audit'}}
                    pair.update(company_name=row['company_name'],published_at=row['published_at'],source_url=row['source_url'],
                                upstream_decision='include',upstream_result_sha256=record[2],**FLAGS)
                    db.execute('INSERT INTO pairs VALUES (?,?,?,?,?)',(row['article_id'],row['ticker'],result['materialization_decision'],record[0],canonical(pair)))
                    counts[result['materialization_decision']]+=1
                put(db,'source_cursor',records[-1][0]);put(db,'prepared_pairs',meta(db,'prepared_pairs')+len(records))
                put(db,'decision_counts',dict(counts))
            status(out,db,expected_pairs=expected)
        with db: put(db,'stage','relations')
        return True
    finally:
        if pool: pool.shutdown(wait=True,cancel_futures=True)

def whitespace(value): return ' '.join(value.split())

def relations(db,out,batch_size):
    from kg_grounded_relations import extract_grounded_relations
    while True:
        if (out/'STOP').exists(): return False
        rows=db.execute('SELECT rowid,article_id,payload FROM articles WHERE rowid>? ORDER BY rowid LIMIT ?',
                        (meta(db,'relation_cursor'),batch_size)).fetchall()
        if not rows:
            with db: put(db,'stage','sentiment')
            return True
        counts=Counter(meta(db,'relation_counts'))
        with db:
            for rowid,article_id,payload in rows:
                article=json.loads(payload)
                pairs=[json.loads(r[0]) for r in db.execute("SELECT payload FROM pairs WHERE article_id=? AND decision='candidate_ready'",(article_id,))]
                entities=[{'ticker':p['ticker'],'company_name':p['company_name'],
                           'aliases':p['target_context']['matched_names']} for p in pairs]
                result=extract_grounded_relations(article['title'],article['body'],entities) if len(entities)>1 else {'relationships':[],'review_candidates':[]}
                reviews=list(result['review_candidates']); allowed={p['ticker'] for p in pairs}
                raw=article['title']+'\n'+article['body']; cleaned=whitespace(article['cleaned_title']+'\n'+article['cleaned_body'])
                for edge in result['relationships']:
                    preview.require(edge['src_ticker'] in allowed and edge['dst_ticker'] in allowed and edge['src_ticker']!=edge['dst_ticker'],
                                    'Relation has an ungrounded endpoint')
                    preview.require(raw[edge['sentence_start']:edge['sentence_end']]==edge['sentence'],'Relation quote differs')
                    if whitespace(edge['sentence']) not in cleaned:
                        reviews.append({**edge,'reason':'relation_sentence_not_retained_in_cleaned_article'});continue
                    identity=[article_id,edge['src_ticker'],edge['dst_ticker'],edge['rel_type'],edge['sentence_start'],edge['sentence_end']]
                    key='relationship:'+digest(identity)
                    value={**edge,'edge_id':key,'article_id':article_id,'news_node':'news:'+article_id,
                           'source_node':'company:'+edge['src_ticker'],'target_node':'company:'+edge['dst_ticker'],
                           'published_at':article['published_at'],'source_url':article['source_url'],
                           'event_time':None,'event_time_status':'not_extracted','offset_basis':'original_title_newline_body',**FLAGS}
                    db.execute('INSERT INTO relations VALUES (?,?,?,?,?,?,?)',(key,article_id,edge['src_ticker'],edge['dst_ticker'],edge['rel_type'],int(edge['directed']),canonical(value)))
                    counts[edge['rel_type']]+=1
                if reviews: db.execute('INSERT INTO relation_reviews VALUES (?,?)',(article_id,canonical(reviews)))
            put(db,'relation_cursor',rows[-1][0]);put(db,'relation_articles',meta(db,'relation_articles')+len(rows))
            put(db,'relation_counts',dict(counts))
        status(out,db)

class AliasCatalog:
    def __init__(self):
        self.names=defaultdict(set)
        with (HERE.parent/'ner/data/aliases.csv').open(encoding='utf-8-sig') as stream:
            for r in csv.DictReader(stream):
                if r.get('ambiguity')=='safe' and r.get('alias_type') in {'official','official_en','en_rule','ko_manual','en_manual'}:
                    self.names[r['alias'].casefold()].add(r['ticker'])
        self.names={name:owners for name,owners in self.names.items() if len(owners)==1 and len(name)>=2}
        def pattern(name):
            body=re.escape(name)
            return r'(?<![A-Za-z0-9])'+body+r'(?![A-Za-z0-9])' if re.search('[A-Za-z]',name) else body
        self.regex=re.compile('|'.join(pattern(name) for name in sorted(self.names,key=lambda x:(-len(x),x))),re.I)
        self.owners={k.casefold():v for k,v in self.names.items()}
    def others(self,text,ticker):
        return sorted({m.group(0) for m in self.regex.finditer(text) if ticker not in self.owners.get(m.group(0).casefold(),set())})

def sentiment_input(pair,article,catalog):
    evidence=pair['evidence']
    text='\n'.join(e.get('cleaned_quote',e['quote']) for e in evidence)
    return {'article_id':pair['article_id'],'ticker':pair['ticker'],'company_name':pair['company_name'],
            'title':article['title'],'body':article['body'],
            'cleaned_title':article['cleaned_title'],'cleaned_body':article['cleaned_body'],
            'input_sha256':pair['source_input_sha256'],
            'evidence':evidence,'target_name_aliases':pair['target_context']['matched_names'],
            'other_company_names':catalog.others(text,pair['ticker'])}

def sentiments(db,out,bundle,device,batch_size,model_batch):
    from kg_target_sentiment import TargetSentimentPredictor
    catalog=AliasCatalog()
    predictor=TargetSentimentPredictor(bundle,device=device,batch_size=model_batch)
    try:
        while True:
            if (out/'STOP').exists(): return False
            rows=db.execute("SELECT p.rowid,p.payload,a.payload FROM pairs p JOIN articles a USING(article_id) WHERE p.decision='candidate_ready' AND p.rowid>? ORDER BY p.rowid LIMIT ?",
                            (meta(db,'sentiment_cursor'),batch_size)).fetchall()
            if not rows:
                with db: put(db,'stage','export')
                return True
            pairs=[json.loads(r[1]) for r in rows]
            inputs=[sentiment_input(p,json.loads(r[2]),catalog) for p,r in zip(pairs,rows)]
            outputs=predictor.predict(inputs)
            preview.require(len(outputs)==len(inputs),'Sentiment coverage differs')
            counts=Counter(meta(db,'sentiment_counts'))
            with db:
                for pair,result in zip(pairs,outputs):
                    preview.require((result['article_id'],result['ticker'])==(pair['article_id'],pair['ticker']),'Sentiment identity differs')
                    preview.require(result.get('status') and (result.get('label') in {'POSITIVE','NEGATIVE','NEUTRAL',None}),'Invalid sentiment output')
                    db.execute('INSERT INTO sentiments VALUES (?,?,?)',(pair['article_id'],pair['ticker'],canonical(result)))
                    counts[result['status']]+=1
                put(db,'sentiment_cursor',rows[-1][0]);put(db,'sentiment_pairs',meta(db,'sentiment_pairs')+len(rows))
                put(db,'sentiment_counts',dict(counts))
            status(out,db)
    finally:
        predictor.close()

def export(db,out,protocol):
    validate_checkpoint(db,protocol['expected_pairs'])
    directory=out/'exports'/('attempt_'+uuid.uuid4().hex)
    directory.mkdir(parents=True)
    files={}
    def write(name,rows):
        path=directory/(name+'.jsonl'); count=0
        with path.open('x',encoding='utf8',newline='\n') as stream:
            for value in rows: stream.write(canonical(value)+'\n');count+=1
        files[name]={'path':path.relative_to(out).as_posix(),'rows':count,'bytes':path.stat().st_size,'sha256':sha(path)}
    write('selection_audit',(json.loads(r[0]) for r in db.execute('SELECT payload FROM pairs ORDER BY article_id,ticker')))
    write('news_nodes',(json.loads(r[0]) for r in db.execute("SELECT a.payload FROM articles a WHERE EXISTS(SELECT 1 FROM pairs p WHERE p.article_id=a.article_id AND p.decision='candidate_ready') ORDER BY a.article_id")))
    def links():
        for raw,mood in db.execute("SELECT p.payload,s.payload FROM pairs p LEFT JOIN sentiments s USING(article_id,ticker) WHERE p.decision='candidate_ready' ORDER BY p.article_id,p.ticker"):
            preview.require(mood is not None,'A selected link has no sentiment status')
            pair=json.loads(raw)
            yield {**pair,'edge_id':'economic-info:'+digest([pair['article_id'],pair['ticker']]),
                   'source_node':'news:'+pair['article_id'],'target_node':'company:'+pair['ticker'],
                   'predicate':'HAS_ECONOMIC_INFORMATION_ABOUT','sentiment':json.loads(mood)}
    write('news_company_edges',links())
    write('company_nodes',({'node_id':'company:'+ticker,'node_type':'company','ticker':ticker,
                           'display_name':json.loads(payload)['company_name'],
                           'display_name_scope':'current_dictionary_not_historical_identity_proof',**FLAGS}
                           for ticker,payload in db.execute("SELECT ticker,MIN(payload) FROM pairs WHERE decision='candidate_ready' GROUP BY ticker ORDER BY ticker")))
    write('relationship_evidence',(json.loads(r[0]) for r in db.execute('SELECT payload FROM relations ORDER BY edge_id')))
    write('company_relationships',({'source_node':'company:'+src,'target_node':'company:'+dst,'src_ticker':src,'dst_ticker':dst,
                                   'rel_type':kind,'directed':bool(directed),'evidence_count':count,'news_count':articles,**FLAGS}
                                  for src,dst,kind,directed,count,articles in db.execute('SELECT src_ticker,dst_ticker,rel_type,directed,COUNT(*),COUNT(DISTINCT article_id) FROM relations GROUP BY src_ticker,dst_ticker,rel_type,directed')))
    write('relation_review',({'article_id':article,'candidates':json.loads(raw),**FLAGS}
                             for article,raw in db.execute('SELECT article_id,payload FROM relation_reviews ORDER BY article_id')))
    ready=meta(db,'decision_counts').get('candidate_ready',0)
    preview.require(files['selection_audit']['rows']==protocol['expected_pairs'] and files['news_company_edges']['rows']==ready
                    and meta(db,'sentiment_pairs')==ready,'Export coverage differs')
    ensure_bindings(protocol['code_and_assets'])
    preview.require(sha(Path(protocol['source']['directory'])/'state.sqlite3')==protocol['source']['db_sha256'],'Original snapshot changed')
    manifest={'schema':SCHEMA,'artifact_complete':True,'graph_materialized':True,**FLAGS,
              'protocol_sha256':sha(out/'protocol.json'),'input_pairs':protocol['expected_pairs'],
              'bounded_pilot':protocol['settings']['limit_pairs'] is not None,
              'parent_unprocessed_pairs':protocol['source']['parent_unprocessed_pairs'],
              'full_population_complete':False,'decision_counts':meta(db,'decision_counts'),
              'sentiment_counts':meta(db,'sentiment_counts'),'relation_counts':meta(db,'relation_counts'),
              'files':files,'finished_at':now(),'execution_maintenance':protocol.get('maintenance_receipts',[])}
    with db: put(db,'stage','complete')
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    manifest['state_sqlite_sha256']=sha(out/'state.sqlite3')
    atomic(out/'manifest.json',manifest)
    status(out,db,'complete',artifact_complete=True,graph_materialized=True)
    return manifest

def run(args):
    out=args.out.resolve(); merged=args.merged_dir.resolve(); bundle=args.bundle.resolve()
    if not out.exists():
        preview.require(not args.resume,'Resume output does not exist')
        out.mkdir(parents=True)
    else: preview.require(args.resume,'Output exists; explicitly resume or choose a new directory')
    with run_lock(out):
        from kg_target_sentiment import TargetSentimentPredictor
        source_info=source_contract(merged)
        model_audit=TargetSentimentPredictor(bundle,device=args.device,batch_size=args.model_batch_size)
        provenance=model_audit.provenance
        # No model is loaded by initialization; avoid initializing CUDA here.
        del model_audit
        settings={'workers':args.workers,'batch_size':args.batch_size,'device':args.device,
                  'model_batch_size':args.model_batch_size,'limit_pairs':args.limit_pairs,'bundle':str(bundle)}
        protocol={'schema':SCHEMA,'source':source_info,'code_and_assets':code_and_assets(),'runtime':runtime_versions(),
                  'maintenance_receipts':maintenance_receipts(out),
                  'sentiment_models':provenance,'settings':settings,
                  'expected_pairs':min(source_info['include_pairs'],args.limit_pairs) if args.limit_pairs else source_info['include_pairs'],**FLAGS}
        if args.resume:
            preview.require(protocol==read(out/'protocol.json'),'Source/code/model/settings differ from frozen run')
        else: atomic(out/'protocol.json',protocol)
        source=sqlite3.connect((merged/'state.sqlite3').as_uri()+'?mode=ro&immutable=1',uri=True)
        db=sqlite3.connect(out/'state.sqlite3')
        try:
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
            if not args.resume: schema(db)
            validate_checkpoint(db,protocol['expected_pairs'])
            if meta(db,'stage')=='complete':
                if not (out/'manifest.json').exists():
                    # Recovery after the final DB commit but before atomic manifest publication.
                    return export(db,out,protocol)
                manifest=read(out/'manifest.json')
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                verify_manifest(out,protocol,manifest)
                status(out,db,'complete',artifact_complete=True);return manifest
            status(out,db,expected_pairs=protocol['expected_pairs'])
            while meta(db,'stage')!='complete':
                if (out/'STOP').exists(): status(out,db,'paused_stop_file');return
                stage=meta(db,'stage')
                if stage=='prepare': finished=prepare(db,source,out,protocol,args.workers,args.batch_size)
                elif stage=='relations': finished=relations(db,out,args.batch_size)
                elif stage=='sentiment': finished=sentiments(db,out,bundle,args.device,args.batch_size,args.model_batch_size)
                elif stage=='export': return export(db,out,protocol)
                else: raise ValueError('Unknown checkpoint stage: '+str(stage))
                if not finished: status(out,db,'paused_stop_file');return
        except BaseException as error:
            db.rollback()
            status(out,db,'failed',error={'type':type(error).__name__,'message':str(error)})
            raise
        finally:
            db.close();source.close()

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--merged-dir',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--bundle',type=Path,default=HERE/'artifacts/news_impact_v2_bundle')
    p.add_argument('--workers',type=int,default=4);p.add_argument('--batch-size',type=int,default=256)
    p.add_argument('--model-batch-size',type=int,default=32);p.add_argument('--device',default='cuda')
    p.add_argument('--limit-pairs',type=int);p.add_argument('--resume',action='store_true')
    return p

if __name__=='__main__':
    args=parser().parse_args()
    preview.require(args.workers>=1 and args.batch_size>=1 and args.model_batch_size>=1,'Batch sizes/workers must be positive')
    preview.require(args.limit_pairs is None or args.limit_pairs>0,'Pilot limit must be positive')
    run(args)
