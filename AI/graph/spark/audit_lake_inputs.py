"""Read-only inventory for the canonical recent-news batch.

Records completed input file names/sizes and actual date coverage locally on
the HDFS host. Does not publish artifacts or mutate any HDFS input. The report
is a frozen discovery record; downstream must still verify the chosen files.
"""
import argparse
import json
from pathlib import Path

from pyspark.sql import SparkSession, functions as F


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    spark = SparkSession.builder.appName('cosmos-lake-input-audit').getOrCreate()
    spark.sparkContext.setLogLevel('WARN')
    fs = spark._jvm.org.apache.hadoop.fs.FileSystem.get(spark._jsc.hadoopConfiguration())
    PathJ = spark._jvm.org.apache.hadoop.fs.Path
    roots = {
        'news_prepared': '/datasets/news/snapshots/20260908-prepared/news',
        'news_realtime': '/data-lake/raw/realtime/news',
        'news_backfill': '/data-lake/raw/news',
        'dart_imports': '/datasets/opendart/imports',
        'dart_snapshots': '/datasets/opendart/snapshots',
        'sec_snapshot': '/datasets/sec-edgar/snapshots/20260908-nasdaq100-10y',
        'sec_realtime': '/data-lake/raw/realtime/sec-edgar',
        'prices_snapshot': '/datasets/prices/snapshots/20260911-krx-nasdaq-daily',
        'prices_daily': '/data-lake/raw/stock-prices/daily',
        'mentions_current': '/data-lake/analyzed/news/company-mentions',
    }
    report = {'roots': roots, 'files': {}, 'samples': {}, 'coverage': {}}
    def save():
        pending = out / 'audit.json.tmp'
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf8')
        pending.replace(out / 'audit.json')
    for name, root in roots.items():
        it = fs.listFiles(PathJ(root), True)
        entries = []
        while it.hasNext():
            st = it.next()
            p = st.getPath().toUri().getPath()
            if any(x.startswith('.') for x in p.split('/')):
                continue
            entries.append({'path': p, 'bytes': st.getLen(), 'modified_ms': st.getModificationTime()})
        report['files'][name] = sorted(entries, key=lambda x:x['path'])
        print(json.dumps({'inventory':name,'files':len(entries),'bytes':sum(x['bytes'] for x in entries)}), flush=True)
        save()
    # Only materialized prepared partitions in the requested date range.
    df = spark.read.parquet(roots['news_prepared']).filter(
        (F.col('year') == '2026') & (F.col('month').cast('int').between(6,9))
    ).filter((F.col('published_date') >= '2026-06-18') & (F.col('published_date') < '2026-09-16'))
    rows = df.groupBy('region','source_dataset').agg(F.count('*').alias('rows'),
        F.min('published_date').alias('min_date'), F.max('published_date').alias('max_date'),
        F.sum(F.col('text_eligible').cast('long')).alias('eligible'),
        F.sum(F.col('is_body_representative').cast('long')).alias('body_representatives')).collect()
    report['coverage']['news_prepared'] = [x.asDict() for x in rows]
    report['schemas'] = {'news_prepared': df.schema.jsonValue()}
    print(json.dumps({'coverage':report['coverage']['news_prepared']}), flush=True)
    save()
    for key in ['prices_snapshot','prices_daily','news_realtime','mentions_current','dart_imports','dart_snapshots','sec_snapshot','sec_realtime']:
        files = [x['path'] for x in report['files'][key] if x['bytes'] and x['path'].endswith(('.parquet','.jsonl','.jsonl.gz','.json'))]
        # Prefer one actual data file over metadata, preserve schemas + terse values.
        files.sort(key=lambda p: (not p.endswith(('.parquet','.jsonl','.jsonl.gz')), 'manifest' in p, p))
        if not files:
            continue
        p = files[0]
        sample = spark.read.parquet(p) if p.endswith('.parquet') else spark.read.json(p)
        report['schemas'][key] = sample.schema.jsonValue()
        def terse(v):
            if isinstance(v,dict): return {k:terse(z) for k,z in v.items()}
            if isinstance(v,list): return [terse(z) for z in v[:1]]
            if isinstance(v,str) and len(v)>180: return v[:80]+' [truncated; chars='+str(len(v))+']'
            return v
        report['samples'][key] = {'path':p,'row':terse(sample.first().asDict(recursive=True))}
        if key.startswith('prices'):
            report['coverage'][key] = {'sample_columns':sample.columns, 'sample_file_rows':sample.count()}
        print(json.dumps({'sample':key,'path':p,'columns':sample.columns}), flush=True)
        save()
    spark.stop()


if __name__ == '__main__':
    main()
