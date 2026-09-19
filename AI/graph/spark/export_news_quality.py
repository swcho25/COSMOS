"""Read-only source audit and train-only financial-event candidate expansion.

Retain source timestamps/URLs/normalization provenance discarded by the old export.
Keywords select candidates, never human-reviewed events or directional gold labels.
No price/outcome/model predictions are read. Existing articles are enriched by ID,
not resampled; new candidates are only 2017-2022 and deterministic per year/region.
Writes only a NEW local output directory on the Spark host; never writes HDFS.
"""
import argparse
import json
import hashlib
import shutil
from pathlib import Path

NEWS='hdfs://localhost:9000/datasets/news/snapshots/20260908-prepared/news'
MENTIONS='hdfs://localhost:9000/data-lake/sandbox/news/junwoo/company-mentions/model_version=dict-v1.3'
KEYWORDS=r'(?i)(실적|영업이익|순이익|매출|수주|공급\s*계약|인수\s*합병|유상\s*증자|자사주|배당|리콜|소송|파산|earnings|quarterly results|revenue|net income|operating profit|supply agreement|awarded.{0,40}contract|acquisition|merger|buyback|dividend|recall|lawsuit|bankruptcy)'
META=['record_id','source_dataset','source_record_id','source_uri','url','canonical_url','published_at_raw','published_at','published_date','published_timezone','date_normalization_method','collected_at','metadata_json','source_category','source_category_text','publisher','language','region','year','body_sha256','body_dedup_sha256']


def main():
    from pyspark.sql import SparkSession,Window,functions as F
    ap=argparse.ArgumentParser();ap.add_argument('--out',required=True);ap.add_argument('--legacy-jsonl',required=True);ap.add_argument('--per-year-region',type=int,default=5000)
    ap.add_argument('--resume-domestic-from');ap.add_argument('--completed-domestic-rows',type=int);args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=False)
    spark=(SparkSession.builder.appName('cosmos-news-quality-audit').config('spark.sql.sources.partitionColumnTypeInference.enabled','false').config('spark.sql.session.timeZone','UTC').getOrCreate())
    spark.sparkContext.setLogLevel('WARN')
    def dump(name,value):(out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf8')
    def export(df,name):
        count=0
        with (out/name).open('x',encoding='utf8') as f:
            for r in df.toLocalIterator():f.write(json.dumps(r.asDict(recursive=True),ensure_ascii=False,default=str)+'\n');count+=1
        print(json.dumps(dict(file=name,rows=count)),flush=True);return count
    try:
        news=spark.read.option('basePath',NEWS).parquet(NEWS)
        cols=[c for c in META if c in news.columns]
        with Path(args.legacy_jsonl).open(encoding='utf8') as f:ids=[(json.loads(line)['record_id'],) for line in f]
        if args.resume_domestic_from:
            previous=Path(args.resume_domestic_from)
            for name in ('schema.json','source_time_counts.json','time_examples.jsonl','legacy_metadata.jsonl'):shutil.copyfile(previous/name,out/name)
            with (out/'legacy_metadata.jsonl').open(encoding='utf8') as f:legacy=sum(1 for _ in f)
            assert legacy==len(ids)
        else:
            dump('schema.json',news.schema.jsonValue())
            clock=F.col('published_at').rlike(r'[T ]\d{2}:\d{2}');offset=F.col('published_at').rlike(r'(Z|[+-]\d{2}:\d{2})$')
            groups=news.groupBy('source_dataset','region','year','date_normalization_method','published_timezone').agg(
                F.count('*').alias('rows'),F.sum(clock.cast('long')).alias('with_clock'),F.sum((clock & offset).cast('long')).alias('with_offset_clock'),
                F.sum(F.col('url').isNotNull().cast('long')).alias('with_url'),F.sum(F.col('collected_at').isNotNull().cast('long')).alias('with_collected_at'))
            dump('source_time_counts.json',[r.asDict() for r in groups.collect()])
            window=Window.partitionBy('source_dataset','date_normalization_method').orderBy(F.sha2('record_id',256))
            export(news.select(*cols).withColumn('_rank',F.row_number().over(window)).filter('_rank <= 3').drop('_rank'),'time_examples.jsonl')
            selected=spark.createDataFrame(ids,['record_id']).distinct()
            legacy=export(news.join(F.broadcast(selected),'record_id').select(*cols),'legacy_metadata.jsonl')
        counts=[];total=0
        with (out/'training_candidates.jsonl').open('x',encoding='utf8') as f:
            if args.resume_domestic_from:
                seen=set()
                with (previous/'training_candidates.jsonl').open(encoding='utf8') as old:
                    for line in old:
                        r=json.loads(line);assert r['region']=='domestic' and '2017'<=r['year']<='2022' and r['record_id'] not in seen
                        seen.add(r['record_id']);f.write(line);total+=1
                assert total==args.completed_domestic_rows
                print(json.dumps(dict(reused_completed_domestic=total)),flush=True)
            for region in (('overseas',) if args.resume_domestic_from else ('domestic','overseas')):
                root=f'{MENTIONS}/run_id=dict-v1.3-{region}-20260911-0655/data'
                mentions=spark.read.parquet(root).filter((F.col('confidence')>=.9)&(~F.col('is_sports'))&F.col('year').between('2017','2022'))
                linked=mentions.groupBy('record_id').agg(F.collect_list(F.struct('ticker','n_mentions','first_pos','confidence','method')).alias('companies'))
                base=news.filter((F.col('region')==region)&F.col('year').between('2017','2022')&F.col('text_eligible')&F.col('is_body_representative'))
                # Materialization prevents Catalyst pushing expensive regex before
                # the company join. Cache only the bounded lead, not whole bodies.
                lite=base.select('record_id','year','title',F.substring('body',1,1200).alias('_lead')).join(linked,'record_id').cache()
                print(json.dumps(dict(region=region,linked_articles=lite.count())),flush=True)
                eligible=lite.filter(F.concat_ws('\n',F.col('title'),F.col('_lead')).rlike(KEYWORDS)).cache()
                counts.extend(dict(region=region,**r.asDict()) for r in eligible.groupBy('year').count().collect())
                window=Window.partitionBy('year').orderBy(F.sha2(F.concat(F.lit('financial-quality-v1:'),F.col('record_id')),256))
                chosen=eligible.withColumn('_rank',F.row_number().over(window)).filter(F.col('_rank')<=args.per_year_region).select('record_id','companies')
                full=base.join(F.broadcast(chosen),'record_id').select(*cols,'title','body','companies')
                for row in full.toLocalIterator():f.write(json.dumps(row.asDict(recursive=True),ensure_ascii=False,default=str)+'\n');total+=1
                eligible.unpersist();lite.unpersist();print(json.dumps(dict(region=region,training_candidates=total)),flush=True)
        dump('manifest.json',dict(news_source=NEWS,mentions_source=MENTIONS,legacy_ids=len(ids),legacy_metadata=legacy,training_candidates=total,
            per_year_region=args.per_year_region,eligible_candidates=counts,keyword_regex=KEYWORDS,
            selection='train only 2017-2022, SHA256 financial-quality-v1:record_id per region/year',human_reviewed=False,labels_used=False,
            resumed_domestic_from=args.resume_domestic_from,domestic_eligible_count_available=not bool(args.resume_domestic_from),
            resumed_candidate_sha256=hashlib.sha256((previous/'training_candidates.jsonl').read_bytes()).hexdigest() if args.resume_domestic_from else None))
    finally:spark.stop()


if __name__=='__main__':main()
