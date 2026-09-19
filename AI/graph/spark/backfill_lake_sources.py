"""Freeze all completed news sources, then normalize/index without collecting history.

Backfill at a fixed as_of, not training and not a historical point-in-time claim.
Original archives are read-only. Each frozen Parquet file is one SQLite transaction;
compressed payloads are durable before its checkpoint commits. The previous 90-day
identity map is seeded first. Global identity resolution finishes before publication.
Prepared archive, live mirror and closed production news.raw batches are included;
raw duplicate archives and verification fixtures are not alternative extra articles.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lake_news_normalize import normalize_article
from prepare_lake_recent import map_news
from lake_backfill_state import BackfillState

ROOTS = {
    'news_prepared': '/datasets/news/snapshots/20260908-prepared/news',
    'news_live': '/datasets/news/live/mysql_overseas/data',
    'news_realtime': '/data-lake/raw/realtime/news',
}

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def atomic(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    with tmp.open('x',encoding='utf8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,default=str); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)

def stamp(value): return dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('mode',choices=['inventory','ingest'])
    ap.add_argument('--run-dir',type=Path,required=True)
    ap.add_argument('--as-of',required=True)
    ap.add_argument('--minimum-free-gb',type=int,default=20)
    args=ap.parse_args(); root=args.run_dir.resolve(); root.mkdir(parents=True,exist_ok=True)
    from pyspark.sql import SparkSession,functions as F
    spark=SparkSession.builder.appName('cosmos-news-history-'+args.mode).config('spark.sql.session.timeZone','UTC').getOrCreate()
    spark.sparkContext.setLogLevel('WARN')
    fs=spark._jvm.org.apache.hadoop.fs.FileSystem.get(spark.sparkContext._jsc.hadoopConfiguration())
    JP=spark._jvm.org.apache.hadoop.fs.Path
    def entries(path):
        result=[]; it=fs.listFiles(JP(path),True)
        while it.hasNext():
            s=it.next(); result.append({'path':str(s.getPath().toUri().getPath()),'bytes':int(s.getLen()),'modified_ms':int(s.getModificationTime())})
        return sorted(result,key=lambda x:x['path'])
    def check(row):
        s=fs.getFileStatus(JP(row['path']))
        if (int(s.getLen()),int(s.getModificationTime())) != (row['bytes'],row['modified_ms']):
            raise ValueError('Frozen source changed: '+row['path'])
    try:
        inventory=root/'source_manifest.json'
        if args.mode=='inventory':
            if inventory.exists():
                saved=json.loads(inventory.read_text())
                if saved['as_of']!=args.as_of or saved['roots']!=ROOTS:raise ValueError('Existing inventory contract differs')
                for item in saved['files']+saved['controls']:check(item)
                atomic(root/'inventory.done.json',{'sha256':sha(inventory),'file_count':len(saved['files'])})
                return
            files=[]; controls=[]
            for kind,path in ROOTS.items():
                observed=entries(path)
                if kind=='news_realtime':
                    markers=[x for x in observed if '/topic=news.raw/' in x['path'] and x['path'].endswith('/_manifest.json')]
                    completed={x['path'].rsplit('/',1)[0] for x in markers}; controls.extend(markers)
                    selected=[x for x in observed if x['path'].endswith('.parquet') and any(x['path'].startswith(p+'/') for p in completed)]
                else: selected=[x for x in observed if x['path'].endswith('.parquet')]
                if not selected: raise ValueError('No source files: '+kind)
                files.extend({**x,'kind':kind,'id':hashlib.sha256(x['path'].encode()).hexdigest()[:24]} for x in selected)
            manifest={'schema_version':'lake-backfill-sources-1','as_of':args.as_of,'roots':ROOTS,'files':files,'controls':controls,
                      'source_file_count':len(files),'source_bytes':sum(x['bytes'] for x in files),
                      'scope':'all observed prepared/live/closed production news files, publication before as_of',
                      'excluded':'raw mirror duplicates, verification fixtures, unfinished live batches, publication at/after as_of',
                      'historical_point_in_time_certified':False}
            atomic(inventory,manifest); atomic(root/'inventory.done.json',{'sha256':sha(inventory),'file_count':len(files)})
            print(json.dumps({'inventory_files':len(files),'bytes':manifest['source_bytes']}),flush=True); return
        m=json.loads(inventory.read_text()); assert m['as_of']==args.as_of
        end=stamp(args.as_of); normalized=root/'normalized'; normalized.mkdir(exist_ok=True)
        prior=root/'inputs'/'document_id_map.jsonl'; prior_hash=sha(prior)
        with BackfillState(root/'identity.sqlite') as state:
            if not state.get_checkpoint('prior_id_map'):
                with prior.open(encoding='utf8') as f: state.seed_prior(json.loads(line) for line in f if line.strip())
                state.set_checkpoint('prior_id_map',prior_hash,0); state.commit()
            elif state.get_checkpoint('prior_id_map')['sha256'] != prior_hash:
                raise ValueError('Prior identity map changed')
            for control in m['controls']: check(control)
            totals={'source_rows':0,'accepted_rows':0,'invalid_rows':0,'outside_as_of_rows':0,'completed_files':0}
            for source in m['files']:
                fingerprint=hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest()
                checkpoint=state.get_checkpoint(source['id'])
                receipt=root/'source_receipts'/(source['id']+'.json')
                if checkpoint:
                    if checkpoint['sha256']!=fingerprint: raise ValueError('Source checkpoint fingerprint changed')
                    done=json.loads(receipt.read_text())
                    payload=root/done['payload_file']
                    if sha(payload)!=done['payload_sha256']: raise ValueError('Completed payload changed')
                    for k in ['source_rows','accepted_rows','invalid_rows','outside_as_of_rows']: totals[k]+=done[k]
                    totals['completed_files']+=1; continue
                if shutil.disk_usage(root).free < args.minimum_free_gb*1024**3: raise RuntimeError('DISK_FREE_BELOW_LIMIT')
                check(source)
                payload=normalized/(source['id']+'-'+uuid.uuid4().hex+'.bin')
                quarantine=root/'quarantine'; quarantine.mkdir(exist_ok=True)
                rejected=quarantine/(payload.stem+'.jsonl')
                counts={k:0 for k in ['source_rows','accepted_rows','invalid_rows','outside_as_of_rows']}
                frame=spark.read.option('basePath',ROOTS['news_prepared']).parquet(source['path']) if source['kind']=='news_prepared' else spark.read.parquet(source['path'])
                frame=frame.withColumn('source_path',F.lit(source['path']))
                with payload.open('xb') as out, rejected.open('x',encoding='utf8') as bad:
                    for raw in frame.toLocalIterator():
                        counts['source_rows']+=1
                        record=json.loads(json.dumps(raw.asDict(recursive=True),default=str,ensure_ascii=False))
                        row=map_news(record,source['kind'])
                        clean=normalize_article(row)
                        reason=None
                        if clean is None: reason='invalid_text_or_date'
                        elif not clean.get('record_id') or not clean.get('source_dataset'): reason='missing_source_identity'
                        elif stamp(clean['published_at'])>=end: reason='outside_as_of'
                        if reason:
                            counts['outside_as_of_rows' if reason=='outside_as_of' else 'invalid_rows']+=1
                            bad.write(json.dumps({'source_file':source['path'],'row':counts['source_rows'],'source_record_id':row.get('record_id'),'reason':reason},ensure_ascii=False)+'\n'); continue
                        keep=['record_id','source_dataset','source_path','title','body','url','canonical_url',
                              'publisher','organization','provider','region','language','published_at','published_date',
                              'published_at_precision','published_at_source','collected_at','collected_at_source',
                              'content_hash','identity_candidate','quality_flags']
                        clean={key:clean.get(key) for key in keep}
                        clean['record_id']=str(clean['record_id'])
                        data=zlib.compress(json.dumps(clean,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf8'),level=3)
                        offset=out.tell(); out.write(data)
                        state.ingest(clean,{'shard':payload.name,'offset':offset,'length':len(data)})
                        counts['accepted_rows']+=1
                        if counts['source_rows']%1000==0:
                            if shutil.disk_usage(root).free < args.minimum_free_gb*1024**3:raise RuntimeError('DISK_FREE_BELOW_LIMIT')
                            atomic(root/'ingest_progress.json',{'counts':{**totals,'active_source_rows':counts['source_rows']},'message':source['path']})
                    out.flush(); os.fsync(out.fileno()); bad.flush(); os.fsync(bad.fileno())
                check(source)
                done={**counts,'source':source,'payload_file':str(payload.relative_to(root)), 'payload_sha256':sha(payload),'quarantine_file':str(rejected.relative_to(root))}
                # Receipt may precede DB commit; an uncheckpointed attempt is never reused.
                atomic(receipt,done)
                state.set_checkpoint(source['id'],fingerprint,counts['source_rows']); state.commit()
                for k in counts: totals[k]+=counts[k]
                totals['completed_files']+=1
                atomic(root/'ingest_progress.json',{'counts':totals,'message':source['path']})
                print(json.dumps({'source_done':source['id'],**counts,'completed_files':totals['completed_files']}),flush=True)
            for control in m['controls']: check(control)
            state.finalize(); state.commit()
            summary=state.summary()
            atomic(root/'identity_summary.json',{'counts':totals,'identity':summary,'source_manifest_sha256':sha(inventory),'prior_map_sha256':prior_hash})
            atomic(root/'ingest.done.json',{'counts':totals,'identity':summary})
    finally: spark.stop()

if __name__=='__main__': main()
