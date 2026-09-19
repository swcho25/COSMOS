"""Freeze and export observed recent inputs to a new directory on the HDFS host.

This is an extraction stage, not publication. Source files stay read-only. Only
closed realtime batches with manifests are read; verification fixtures are
excluded. Date filtering is deliberately broad here; exact UTC validation and
the requested [as_of-90d,as_of) window are enforced by the normalizer later.
"""
import argparse
import datetime as dt
import json
import hashlib
import shutil
from pathlib import Path

from pyspark.sql import SparkSession, functions as F, types as T


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--audit', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--as-of', default='2026-09-16T00:00:00Z')
    ap.add_argument('--reuse-inputs', help='Completed prior export; only unchanged unfiltered stages may be reused')
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    audit = json.loads(Path(args.audit).read_text())
    end = dt.datetime.fromisoformat(args.as_of.replace('Z','+00:00'))
    first = (end - dt.timedelta(days=90)).date().isoformat()
    last = end.date().isoformat()
    broad_first=(end-dt.timedelta(days=91)).date().isoformat()
    broad_last=(end+dt.timedelta(days=1)).date().isoformat()
    spark = SparkSession.builder.appName('cosmos-lake-recent-export').getOrCreate()
    spark.sparkContext.setLogLevel('WARN')
    fs = spark._jvm.org.apache.hadoop.fs.FileSystem.get(spark._jsc.hadoopConfiguration())
    JP = spark._jvm.org.apache.hadoop.fs.Path
    manifest = {'as_of':args.as_of, 'start':first, 'sources':{}, 'counts':{}, 'schemas':{}, 'completion_controls':{}, 'coverage':{}}
    reuse=None
    if args.reuse_inputs:
        old=Path(args.reuse_inputs)
        if not (old/'_SUCCESS').exists(): raise ValueError('Cannot reuse unfinished export')
        reuse=json.loads((old/'manifest.json').read_text())
        if reuse['as_of']!=args.as_of: raise ValueError('Cannot reuse different window')

    def save():
        (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,default=str),encoding='utf8')

    def check(entries):
        for x in entries:
            st = fs.getFileStatus(JP(x['path']))
            if st.getLen()!=x['bytes'] or st.getModificationTime()!=x['modified_ms']:
                raise ValueError('Input changed since inventory: '+x['path'])
        return [x['path'] for x in entries]

    all_files={x['path']:x for files in audit['files'].values() for x in files}
    def control(path):
        entry=all_files.get(path)
        if entry is None: raise ValueError('Required completion control missing: '+path)
        check([entry])
        stream=fs.open(JP(path))
        try: value=bytes(stream.readAllBytes())
        finally: stream.close()
        sha=hashlib.sha256(value).hexdigest()
        manifest['completion_controls'][path]={**entry,'sha256':sha}
        return json.loads(value) if value.strip() else {}

    def export(name, frame, entries):
        manifest['sources'][name] = entries
        manifest['schemas'][name] = frame.schema.jsonValue()
        save()
        if reuse and name in ['news_realtime','prices','dart'] and entries==reuse['sources'].get(name):
            shutil.copyfile(Path(args.reuse_inputs)/(name+'.jsonl'),out/(name+'.jsonl'))
            n=reuse['counts'][name]
            manifest.setdefault('reused_stages',{})[name]=str(Path(args.reuse_inputs)/(name+'.jsonl'))
        else:
            n=0
            with (out/(name+'.jsonl')).open('x',encoding='utf8') as f:
                for row in frame.toLocalIterator():
                    f.write(json.dumps(row.asDict(recursive=True),ensure_ascii=False,default=str)+'\n')
                    n+=1
        check(entries)
        manifest['counts'][name]=n
        save()
        print(json.dumps({'exported':name,'rows':n}),flush=True)

    entries=[x for x in audit['files']['news_prepared'] if '/year=2026/' in x['path'] and any('/month='+m+'/' in x['path'] for m in ['06','07','08','09','6','7','8','9'])]
    paths=check(entries)
    frame=spark.read.option('basePath',audit['roots']['news_prepared']).parquet(*paths)
    frame=frame.filter((F.col('published_date')>=broad_first)&(F.col('published_date')<broad_last)).withColumn('source_path',F.input_file_name())
    export('news_prepared',frame,entries)

    all_news=audit['files']['news_realtime']
    complete={x['path'].rsplit('/',1)[0] for x in all_news if x['path'].endswith('/_manifest.json') and '/topic=news.raw/' in x['path']}
    # Pin closed-batch marker identity; each data file is checked before and after read.
    check([all_files[p+'/_manifest.json'] for p in complete])
    manifest['news_realtime_completion_markers']=[all_files[p+'/_manifest.json'] for p in sorted(complete)]
    entries=[x for x in all_news if x['path'].endswith('.parquet') and '/topic=news.raw/' in x['path'] and any(x['path'].startswith(p+'/') for p in complete)]
    paths=check(entries)
    frame=spark.read.option('recursiveFileLookup','true').parquet(*paths).withColumn('source_path',F.input_file_name())
    export('news_realtime',frame,entries)

    # The live mirror is a separate existing source. Pin file status before read.
    root='/datasets/news/live/mysql_overseas/data'
    it=fs.listFiles(JP(root),True)
    entries=[]
    while it.hasNext():
        s=it.next(); p=s.getPath().toUri().getPath()
        if p.endswith('.parquet'):
            entries.append({'path':p,'bytes':s.getLen(),'modified_ms':s.getModificationTime()})
    frame=spark.read.parquet(*check(entries))
    manifest['schemas']['news_live']=frame.schema.jsonValue(); save()
    # Only an explicit publication field may narrow this input, never collection time.
    date_col=next((x for x in ['published_at','published_date','pub_date','publish_date','publishedAt'] if x in frame.columns),None)
    if not date_col:
        raise ValueError('Live publication field needs mapping: '+str(frame.columns))
    frame=frame.filter((F.substring(F.col(date_col).cast('string'),1,10)>=broad_first)&(F.substring(F.col(date_col).cast('string'),1,10)<broad_last)).withColumn('source_path',F.input_file_name())
    export('news_live',frame,entries)

    price_entries=[x for x in audit['files']['prices_snapshot'] if x['path'].endswith('.parquet')]
    price_complete={x['path'].rsplit('/',1)[0] for x in audit['files']['prices_daily'] if x['path'].endswith('/_SUCCESS')}
    for p in sorted(price_complete): control(p+'/_SUCCESS')
    price_entries += [x for x in audit['files']['prices_daily'] if x['path'].endswith('/prices_daily.parquet') and x['path'].rsplit('/',1)[0] in price_complete]
    # Read sources separately: old and current decimal precision may differ.
    price_frames=[]
    for entry in price_entries:
        frame=spark.read.parquet(*check([entry])).withColumn('source_path',F.lit(entry['path']))
        for col in ['open_price','high_price','low_price','close_price','adj_close']:
            frame=frame.withColumn(col,F.col(col).cast('double'))
        frame=frame.withColumn('date',F.substring(F.col('trading_at').cast('string'),1,10))
        frame=frame.filter((F.col('date')>=(end-dt.timedelta(days=110)).date().isoformat())&(F.col('date')<last))
        price_frames.append(frame)
    frame=price_frames[0]
    for other in price_frames[1:]: frame=frame.unionByName(other,allowMissingColumns=True)
    export('prices',frame,price_entries)

    # Fixed schema avoids scanning every decade of filing bodies for inference.
    strings=['corp_code','stock_code','corp_name','rcept_dt','rcept_no','report_nm','fetched_at']
    schema=T.StructType([T.StructField(x,T.StringType()) for x in strings]+[T.StructField('files',T.ArrayType(T.StructType([T.StructField('filename',T.StringType()),T.StructField('text',T.StringType())])))])
    entries=[x for key in ['dart_imports','dart_snapshots'] for x in audit['files'][key] if x['path'].endswith('_detail.jsonl.gz')]
    dart_roots=sorted({'/'.join(x['path'].split('/')[:5]) for x in entries})
    for root in dart_roots:
        ready=control(root+'/ready.json')
        if str(ready.get('status','')).lower()!='ready': raise ValueError('DART not ready: '+root)
        m=control(root+'/manifest.json'); control(root+'/checksums.json')
        for name,key in [('manifest.json','manifest_sha256'),('checksums.json','checksums_sha256')]:
            if ready.get(key) and ready[key]!=manifest['completion_controls'][root+'/'+name]['sha256']:
                raise ValueError('DART completion hash mismatch: '+root+'/'+name)
        manifest['coverage'][root]={k:m.get(k) for k in ['snapshot_kind','begin','end','snapshot_documents','not_in_snapshot','source_unavailable_observed','source_errors_observed']}
    frame=spark.read.schema(schema).json(check(entries)).filter((F.col('rcept_dt')>=first.replace('-',''))&(F.col('rcept_dt')<last.replace('-',''))).withColumn('source_path',F.input_file_name())
    export('dart',frame,entries)

    complete={x['path'].rsplit('/',1)[0] for x in audit['files']['sec_realtime'] if x['path'].endswith('/_SUCCESS')}
    for p in sorted(complete): control(p+'/_SUCCESS')
    entries=[x for x in audit['files']['sec_snapshot'] if x['path'].endswith('/filings.jsonl')]
    entries += [x for x in audit['files']['sec_realtime'] if x['path'].endswith('/metadata.json') and x['path'].rsplit('/',1)[0] in complete]
    frame=spark.read.json(check(entries)).filter((F.col('filingDate')>=broad_first)&(F.col('filingDate')<broad_last)).withColumn('source_path',F.input_file_name())
    export('sec',frame,entries)
    check(list(manifest['completion_controls'].values()))
    check(manifest['news_realtime_completion_markers'])
    (out/'_SUCCESS').write_text('completed input extraction\n')
    spark.stop()


if __name__=='__main__':
    main()
