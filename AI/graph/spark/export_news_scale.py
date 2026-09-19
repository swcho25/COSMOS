"""Read-only HDFS export for a nested, train-only news learning curve.

The financial candidate definition and ``financial-quality-v1:`` SHA256 order
are the same as export_news_quality.py. Raising the cap therefore extends each
region/year prefix when the immutable source cohort is unchanged. The cap is
applied to candidate articles, BEFORE local cleaning/event/price checks; it does
not guarantee that 50k or 100k eligible training articles will survive.

Only 2017-2022 are exported. Original timestamps, URLs, metadata and body text
are preserved as provenance, not trusted event times or training targets. No
price, labels, predictions, validation or test text participates in selection.
Source row counts are written first, then each cohort's selection counts BEFORE
streaming its bodies. Twelve separate cohorts and non-broadcast body joins avoid
caching millions of full articles or collecting them in driver memory.

Writes exclusively to a NEW local directory. Never writes HDFS and never resumes
or overwrites a previous run. A failed run has no completion manifest; its counts
and completed cohort files remain available for diagnosis. Selected conflicting
source identities and duplicate IDs across cohorts are errors, not silent drops.
Explicit --recover-export may copy validated exhaustive shards from a failed run
into a NEW run; its provenance is always marked as partial recovery.
"""

import argparse
import hashlib
import json
from pathlib import Path

NEWS = 'hdfs://localhost:9000/datasets/news/snapshots/20260908-prepared/news'
MENTIONS = 'hdfs://localhost:9000/data-lake/sandbox/news/junwoo/company-mentions/model_version=dict-v1.3'
KEYWORDS = r'(?i)(실적|영업이익|순이익|매출|수주|공급\s*계약|인수\s*합병|유상\s*증자|자사주|배당|리콜|소송|파산|earnings|quarterly results|revenue|net income|operating profit|supply agreement|awarded.{0,40}contract|acquisition|merger|buyback|dividend|recall|lawsuit|bankruptcy)'
META = ['record_id', 'source_dataset', 'source_record_id', 'source_uri', 'url',
        'canonical_url', 'published_at_raw', 'published_at', 'published_date',
        'published_timezone', 'date_normalization_method', 'collected_at',
        'metadata_json', 'source_category', 'source_category_text', 'publisher',
        'language', 'region', 'year', 'body_sha256', 'body_dedup_sha256']
REGIONS = ('domestic', 'overseas')
YEARS = tuple(str(year) for year in range(2017, 2023))
HASH_PREFIX = 'financial-quality-v1:'


def sample_key(record_id):
    """Python equivalent of Spark's frozen SHA256 order, for local audits."""
    return hashlib.sha256((HASH_PREFIX + record_id).encode('utf8')).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def dump(path, payload):
    with Path(path).open('x', encoding='utf8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)


def validate_previous_protocol(protocol):
    expected = {'news_source': NEWS, 'mentions_source': MENTIONS,
                'years': list(YEARS), 'regions': list(REGIONS), 'keyword_regex': KEYWORDS,
                'sampling': 'SHA256 financial-quality-v1:record_id ascending per region/year; ID tie breaker',
                'keyword_scope': 'title plus first 1200 body characters',
                'company_filter': 'confidence >= 0.9 and is_sports == false',
                'article_filter': 'text_eligible and is_body_representative',
                'labels_used': False}
    for field, value in expected.items():
        if protocol.get(field) != value:
            raise ValueError(f'Incompatible prior export protocol: {field}')


def reuse_exhaustive_cohorts(previous, out, cap):
    """Reuse only complete, exhausted cohorts from a hash-verified prior export.

    Prior shard files have no independent hash in v1, so read the verified
    canonical candidate file instead. Reuse is impossible for capped cohorts,
    even if the requested new cap happens to be smaller. Immutable HDFS snapshot
    paths and the candidate definition must match exactly.
    """
    previous = Path(previous).resolve()
    manifest_path, protocol_path = previous/'manifest.json', previous/'protocol.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf8'))
    protocol = json.loads(protocol_path.read_text(encoding='utf8'))
    if manifest.get('complete') is not True:
        raise ValueError('Reuse requires a completed prior export')
    validate_previous_protocol(protocol)
    candidates = previous/'training_candidates.jsonl'
    prior_sha = file_sha(candidates)
    if prior_sha != manifest['training_candidates_sha256']:
        raise ValueError('Prior canonical candidate hash mismatch')
    reusable = {}
    seen_cohorts = set()
    for record in manifest['eligible_candidates']:
        key = record['region'], str(record['year'])
        if key in seen_cohorts:
            raise ValueError(f'Duplicate prior cohort: {key!r}')
        seen_cohorts.add(key)
        if (key[0] not in REGIONS or key[1] not in YEARS or
                record.get('conflicting_record_ids') or record.get('invalid_record_ids')):
            raise ValueError(f'Invalid prior cohort: {key!r}')
        if record['selected_articles'] == record['eligible_articles'] and record['selected_articles'] <= cap:
            reusable[key] = record
    if seen_cohorts != {(region, year) for region in REGIONS for year in YEARS}:
        raise ValueError('Prior completed export lacks a full region/year census')
    dump(out/'reuse_plan.json', dict(previous_export=str(previous),
         manifest_sha256=file_sha(manifest_path), protocol_sha256=file_sha(protocol_path),
         training_candidates_sha256=prior_sha, reusable_counts=list(reusable.values())))
    handles = {}
    counts = {key: 0 for key in reusable}
    total = 0
    try:
        for region, year in reusable:
            handles[(region, year)] = (out/'cohorts'/f'{region}_{year}.jsonl.part').open('x', encoding='utf8')
        with candidates.open(encoding='utf8') as source:
            for line in source:
                row = json.loads(line)
                key = row['region'], str(row['year'])
                total += 1
                if key in handles:
                    handles[key].write(line)
                    counts[key] += 1
    finally:
        for handle in handles.values():
            handle.close()
    if total != manifest['training_candidates']:
        raise ValueError('Prior manifest candidate count mismatch')
    for key, count in counts.items():
        if count != reusable[key]['selected_articles']:
            raise ValueError(f'Prior cohort count mismatch: {key!r}')
        region, year = key
        temporary = out/'cohorts'/f'{region}_{year}.jsonl.part'
        temporary.rename(temporary.with_suffix(''))
    audit = dict(previous_export=str(previous), manifest_sha256=file_sha(manifest_path),
                 protocol_sha256=file_sha(protocol_path), training_candidates_sha256=prior_sha,
                 reused_cohorts=[dict(region=r, year=y, articles=counts[(r, y)])
                                 for r, y in sorted(reusable)],
                 source_assumption='same named immutable HDFS snapshot and frozen selection definition')
    dump(out/'reuse_audit.json', audit)
    return reusable, audit


def recover_exhaustive_cohorts(previous, out, cap):
    """Recover validated completed shards from an explicitly INCOMPLETE run.

    This is deliberately different from --reuse-export: an incomplete run has no
    trustworthy canonical completion manifest. Only final .jsonl + counts.json
    pairs with exhausted cohorts qualify. Their current bytes are fingerprinted
    before and after reuse; internal identity/rank/count contracts are checked.
    A .part file is never read. Missing cohorts are recalculated by the new run.
    Source authenticity before these fingerprints still relies on the named
    frozen HDFS snapshot and the retained prior export protocol, not a fabricated
    claim that the failed run completed.
    """
    previous = Path(previous).resolve()
    protocol_path = previous/'protocol.json'
    protocol_bytes = protocol_path.read_bytes()
    protocol = json.loads(protocol_bytes)
    validate_previous_protocol(protocol)
    old_cap = protocol.get('per_year_region')
    if type(old_cap) is not int or old_cap < 1:
        raise ValueError('Recovery requires a valid original sampling cap')
    manifest_path = previous/'manifest.json'
    dependencies = {str(protocol_path): hashlib.sha256(protocol_bytes).hexdigest()}
    if manifest_path.exists():
        manifest_bytes = manifest_path.read_bytes()
        if json.loads(manifest_bytes).get('complete') is True:
            raise ValueError('Completed exports must use --reuse-export, not --recover-export')
        dependencies[str(manifest_path)] = hashlib.sha256(manifest_bytes).hexdigest()
    reusable, shards, skipped = {}, {}, []
    for region in REGIONS:
        for year in YEARS:
            stem = f'{region}_{year}'
            counts_path = previous/'cohorts'/f'{stem}.counts.json'
            shard_path = previous/'cohorts'/f'{stem}.jsonl'
            if not counts_path.is_file() or not shard_path.is_file():
                skipped.append(dict(region=region, year=year, reason='missing_final_counts_or_jsonl'))
                continue
            counts_bytes = counts_path.read_bytes()
            record = json.loads(counts_bytes)
            if record.get('region') != region or str(record.get('year')) != year:
                raise ValueError(f'Recovery cohort metadata mismatch: {stem}')
            selected, eligible = record.get('selected_articles'), record.get('eligible_articles')
            linked = record.get('linked_rows')
            if (type(selected) is not int or type(eligible) is not int or type(linked) is not int
                    or not 0 <= selected <= eligible <= linked or selected > old_cap):
                raise ValueError(f'Recovery cohort count metadata invalid: {stem}')
            if record.get('conflicting_record_ids') != 0 or record.get('invalid_record_ids') != 0:
                raise ValueError(f'Recovery requires zero identity conflicts: {stem}')
            if selected != eligible or selected > cap:
                skipped.append(dict(region=region, year=year, reason='not_exhaustive_or_exceeds_new_cap'))
                continue
            reusable[(region, year)] = record
            shards[(region, year)] = shard_path
            dependencies[str(counts_path)] = hashlib.sha256(counts_bytes).hexdigest()
            dependencies[str(shard_path)] = file_sha(shard_path)
    plan = dict(previous_export=str(previous), partial_recovery=True,
                previous_run_complete=False, source_dependency_sha256=dependencies,
                reused_cohorts=[dict(region=r, year=y, articles=record['selected_articles'])
                                for (r, y), record in sorted(reusable.items())],
                skipped_cohorts=skipped, partial_files_read=False,
                source_assumption='same named immutable HDFS snapshot; current retained shard fingerprints and protocol, not a prior complete manifest')
    dump(out/'recovery_plan.json', plan)
    seen_ids = set()
    for (region, year), source in sorted(shards.items()):
        bounded, ranks = [], set()
        expected_count = reusable[(region, year)]['selected_articles']
        with source.open(encoding='utf8') as handle:
            for line in handle:
                row = json.loads(line)
                rid, rank = row.get('record_id'), row.get('sampling_rank')
                if not isinstance(rid, str) or not rid or rid in seen_ids:
                    raise ValueError(f'Recovery duplicate or invalid record ID: {rid!r}')
                if (row.get('region') != region or str(row.get('year')) != year or
                        str(row.get('published_date', ''))[:4] != year):
                    raise ValueError(f'Recovery row cohort/year mismatch: {rid}')
                if type(rank) is not int or not 1 <= rank <= expected_count or rank in ranks:
                    raise ValueError(f'Recovery invalid or duplicate sampling rank: {rid}')
                sample_hash = row.get('sampling_hash')
                if sample_hash != sample_key(rid):
                    raise ValueError(f'Recovery sampling hash mismatch: {rid}')
                if ('title' not in row or 'body' not in row or
                        not isinstance(row['title'], (str, type(None))) or
                        not isinstance(row['body'], (str, type(None)))):
                    raise ValueError(f'Recovery missing original text: {rid}')
                companies = row.get('companies')
                if not isinstance(companies, list) or not companies:
                    raise ValueError(f'Recovery missing company linkage: {rid}')
                for company in companies:
                    if (not isinstance(company, dict) or not isinstance(company.get('ticker'), str)
                            or not company['ticker'] or not isinstance(company.get('confidence'), (int, float))
                            or not .9 <= company['confidence']):
                        raise ValueError(f'Recovery invalid company linkage: {rid}')
                seen_ids.add(rid); ranks.add(rank)
                bounded.append((rank, rid, sample_hash, line))
                if len(bounded) > expected_count:
                    raise ValueError(f'Recovery row count exceeds metadata: {source}')
        if len(bounded) != expected_count or ranks != set(range(1, expected_count + 1)):
            raise ValueError(f'Recovery missing rows or ranks: {source}')
        bounded.sort()
        hash_order = [(sample_hash, rid) for _, rid, sample_hash, _ in bounded]
        if hash_order != sorted(hash_order):
            raise ValueError(f'Recovery rank/hash ordering mismatch: {source}')
        temporary = out/'cohorts'/f'{region}_{year}.jsonl.part'
        with temporary.open('x', encoding='utf8') as destination:
            for _, _, _, line in bounded:
                destination.write(line)
        temporary.rename(temporary.with_suffix(''))
    for path, digest in dependencies.items():
        if file_sha(path) != digest:
            raise ValueError(f'Recovery dependency changed during copy: {path}')
    audit = dict(plan, source_dependencies_unchanged=True,
                 recovered_output_sha256={f'{region}_{year}.jsonl': file_sha(out/'cohorts'/f'{region}_{year}.jsonl')
                                          for region, year in reusable})
    dump(out/'recovery_audit.json', audit)
    return reusable, audit


def merge_cohorts(paths, output, expected_counts, cap):
    """Merge bounded cohorts in frozen rank order, regardless of Spark order.

    At most one cohort's raw lines (normally <=25k) is retained at a time. Never
    cache the full 300k-article export. The sorted output also fixes the order in
    which downstream cleaning selects representatives of duplicate article text.
    """
    seen = set()
    counts = {}
    with Path(output).open('x', encoding='utf8') as destination:
        for path in sorted(paths, key=lambda value: Path(value).name):
            bounded = []
            ranks = set()
            shard_cohort = None
            with Path(path).open(encoding='utf8') as source:
                for line in source:
                    row = json.loads(line)
                    rid = row['record_id']
                    cohort = (row['region'], str(row['year']))
                    if not isinstance(rid, str) or not rid or rid in seen:
                        raise ValueError(f'Invalid or duplicate source ID: {rid!r}')
                    if cohort[0] not in REGIONS or cohort[1] not in YEARS:
                        raise ValueError(f'Outside training cohort: {cohort!r}')
                    # A conflicting source date must not enter train solely due
                    # to its partition key. The source values remain unmodified.
                    if str(row.get('published_date', ''))[:4] != cohort[1]:
                        raise ValueError(f'Partition/publication year mismatch: {rid}')
                    rank = row['sampling_rank']
                    if not isinstance(rank, int) or not 1 <= rank <= cap:
                        raise ValueError(f'Invalid sampling rank: {rid}')
                    if row['sampling_hash'] != sample_key(rid):
                        raise ValueError(f'Sampling hash mismatch: {rid}')
                    if shard_cohort is not None and cohort != shard_cohort:
                        raise ValueError(f'Mixed cohorts inside {path}')
                    shard_cohort = cohort
                    if rank in ranks:
                        raise ValueError(f'Duplicate sampling rank in {cohort!r}: {rank}')
                    ranks.add(rank)
                    seen.add(rid)
                    counts[cohort] = counts.get(cohort, 0) + 1
                    bounded.append((rank, rid, line))
                    if len(bounded) > cap:
                        raise ValueError(f'Cohort exceeds bounded sampling cap: {cohort!r}')
            if ranks != set(range(1, len(bounded) + 1)):
                raise ValueError(f'Noncontiguous sampling ranks in {path}')
            for _, _, line in sorted(bounded):
                destination.write(line)
    if {key: value for key, value in expected_counts.items() if value} != counts:
        raise ValueError(f'Export count mismatch: {counts!r}')
    return len(seen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--per-year-region', type=int, default=25000)
    parser.add_argument('--export-partitions', type=int, default=32)
    resume = parser.add_mutually_exclusive_group()
    resume.add_argument('--reuse-export', type=Path,
                        help='Completed previous export; reuse only fully exhausted cohorts')
    resume.add_argument('--recover-export', type=Path,
                        help='Incomplete previous export; validate and recover only final exhaustive shards')
    args = parser.parse_args()
    if args.per_year_region < 1 or args.export_partitions < 1:
        parser.error('Sampling cap and export partitions must be positive')
    if '://' in args.out:
        parser.error('--out must be a new local directory, never an HDFS URI')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'cohorts').mkdir()
    reusable, reuse_audit, recovery_audit = {}, None, None
    if args.reuse_export is not None:
        reusable, reuse_audit = reuse_exhaustive_cohorts(args.reuse_export, out, args.per_year_region)
    elif args.recover_export is not None:
        reusable, recovery_audit = recover_exhaustive_cohorts(args.recover_export, out, args.per_year_region)
    dump(out / 'protocol.json', {
        'news_source': NEWS, 'mentions_source': MENTIONS,
        'years': YEARS, 'regions': REGIONS,
        'per_year_region': args.per_year_region,
        'export_partitions': args.export_partitions,
        'sampling': 'SHA256 financial-quality-v1:record_id ascending per region/year; ID tie breaker',
        'keyword_regex': KEYWORDS, 'keyword_scope': 'title plus first 1200 body characters',
        'company_filter': 'confidence >= 0.9 and is_sports == false',
        'article_filter': 'text_eligible and is_body_representative',
        'timestamp_interpretation': 'raw provenance only; no timezone assumptions or label changes',
        'labels_used': False, 'human_reviewed': False,
        'script_sha256': file_sha(__file__),
        'reuse_export': reuse_audit,
        'partial_recovery': recovery_audit,
    })
    from pyspark import StorageLevel
    from pyspark.sql import SparkSession, Window, functions as F
    spark = (SparkSession.builder.appName('cosmos-news-scale-export')
             .config('spark.sql.sources.partitionColumnTypeInference.enabled', 'false')
             .config('spark.sql.session.timeZone', 'UTC')
             .config('spark.sql.shuffle.partitions', str(args.export_partitions))
             .getOrCreate())
    spark.sparkContext.setLogLevel('WARN')
    dump(out/'runtime.json', dict(spark_version=spark.version,
        master=spark.sparkContext.master,
        driver_memory=spark.sparkContext.getConf().get('spark.driver.memory', 'unspecified'),
        driver_max_result_size=spark.sparkContext.getConf().get('spark.driver.maxResultSize', 'unspecified'),
        sql_configuration={key: spark.conf.get(key) for key in (
            'spark.sql.parquet.columnarReaderBatchSize', 'spark.sql.files.maxPartitionBytes',
            'spark.sql.shuffle.partitions', 'spark.sql.adaptive.enabled',
            'spark.sql.autoBroadcastJoinThreshold')}))
    stats = []
    files = []
    expected = {}
    try:
        news = spark.read.option('basePath', NEWS).parquet(NEWS)
        dump(out / 'schema.json', news.schema.jsonValue())
        required = {'record_id', 'region', 'year', 'published_date', 'title', 'body',
                    'text_eligible', 'is_body_representative'}
        if required - set(news.columns):
            raise ValueError(f'Missing source columns: {sorted(required - set(news.columns))}')
        columns = [name for name in META if name in news.columns] + ['title', 'body']
        # These aggregate source ROWS; they are not counts of unique usable news.
        source_counts = news.groupBy('region', 'year').agg(
            F.count('*').alias('source_rows'),
            F.sum((F.col('text_eligible') & F.col('is_body_representative'))
                  .cast('long')).alias('eligible_representative_rows'))
        dump(out / 'source_counts.json', [row.asDict() for row in source_counts.collect()])
        for region in REGIONS:
            mention_path = f'{MENTIONS}/run_id=dict-v1.3-{region}-20260911-0655/data'
            mentions = spark.read.parquet(mention_path)
            for year in YEARS:
                cohort_name = f'{region}_{year}'
                if (region, year) in reusable:
                    record = dict(reusable[(region, year)], reused_exhaustive_cohort=True,
                                  recovered_from_incomplete_run=recovery_audit is not None)
                    dump(out/'cohorts'/f'{cohort_name}.counts.json', record)
                    files.append(out/'cohorts'/f'{cohort_name}.jsonl')
                    expected[(region, year)] = record['selected_articles']
                    stats.append(record)
                    print(json.dumps(dict(stage='reused_exhaustive_cohort', **record)), flush=True)
                    continue
                selected_mentions = mentions.filter(
                    (F.col('confidence') >= .9) & (~F.col('is_sports')) &
                    (F.col('year').cast('string') == year))
                linked = selected_mentions.groupBy('record_id').agg(
                    F.sort_array(F.collect_set(F.struct(
                        'ticker', 'n_mentions', 'first_pos', 'confidence', 'method')))
                    .alias('companies'))
                base = news.filter(
                    (F.col('region') == region) & (F.col('year').cast('string') == year) &
                    F.col('text_eligible') & F.col('is_body_representative'))
                # Hash all exported source fields before discarding full bodies
                # from the cache. Identical duplicate source rows are harmless;
                # conflicting content under one identity is explicitly rejected.
                base = base.select(*columns).withColumn(
                    '_source_row_sha256', F.sha2(F.to_json(F.struct(*columns)), 256))
                lite = (base.select('record_id', '_source_row_sha256', 'title',
                                    F.substring('body', 1, 1200).alias('_lead'))
                        .join(linked, 'record_id')
                        .dropDuplicates(['record_id', '_source_row_sha256'])
                        .persist(StorageLevel.MEMORY_AND_DISK))
                eligible = None
                chosen = None
                try:
                    linked_count = lite.count()
                    conflicting = (lite.groupBy('record_id').count()
                                   .filter(F.col('count') > 1).count())
                    null_ids = lite.filter(F.col('record_id').isNull() |
                                           (F.length('record_id') == 0)).count()
                    prelim = dict(region=region, year=year, linked_rows=linked_count,
                                  conflicting_record_ids=conflicting, invalid_record_ids=null_ids)
                    dump(out / 'cohorts' / f'{cohort_name}.identity_counts.json', prelim)
                    if conflicting or null_ids:
                        raise ValueError(f'Conflicting/invalid source identity in {cohort_name}')
                    eligible = lite.filter(F.concat_ws('\n', 'title', '_lead').rlike(KEYWORDS))
                    eligible = eligible.persist(StorageLevel.MEMORY_AND_DISK)
                    eligible_count = eligible.count()
                    hash_column = F.sha2(F.concat(F.lit(HASH_PREFIX), F.col('record_id')), 256)
                    window = Window.orderBy('sampling_hash', 'record_id')
                    chosen = (eligible.withColumn('sampling_hash', hash_column)
                              .withColumn('sampling_rank', F.row_number().over(window))
                              .filter(F.col('sampling_rank') <= args.per_year_region)
                              .select('record_id', '_source_row_sha256', 'companies',
                                      'sampling_hash', 'sampling_rank')
                              .persist(StorageLevel.MEMORY_AND_DISK))
                    chosen_count = chosen.count()
                    record = dict(prelim, eligible_articles=eligible_count,
                                  selected_articles=chosen_count)
                    dump(out / 'cohorts' / f'{cohort_name}.counts.json', record)
                    print(json.dumps(dict(stage='counts_before_body_export', **record)), flush=True)
                    # Do not broadcast potentially large company/ID selections.
                    full = (base.hint('merge').join(chosen.hint('merge'),
                            ['record_id', '_source_row_sha256'])
                            .dropDuplicates(['record_id'])
                            .select(*columns, 'companies', 'sampling_hash', 'sampling_rank')
                            .repartition(args.export_partitions, 'record_id'))
                    temporary = out / 'cohorts' / f'{cohort_name}.jsonl.part'
                    count = 0
                    with temporary.open('x', encoding='utf8') as handle:
                        for row in full.toLocalIterator():
                            handle.write(json.dumps(row.asDict(recursive=True),
                                                    ensure_ascii=False, default=str) + '\n')
                            count += 1
                    if count != chosen_count:
                        raise ValueError(f'{cohort_name}: selected {chosen_count}, exported {count}')
                    complete = temporary.with_suffix('')
                    temporary.rename(complete)
                    files.append(complete)
                    expected[(region, year)] = count
                    stats.append(record)
                    print(json.dumps(dict(stage='cohort_complete', **record)), flush=True)
                finally:
                    if chosen is not None:
                        chosen.unpersist(blocking=True)
                    if eligible is not None:
                        eligible.unpersist(blocking=True)
                    lite.unpersist(blocking=True)
        candidate_path = out / 'training_candidates.jsonl'
        total = merge_cohorts(files, candidate_path, expected, args.per_year_region)
        dump(out / 'manifest.json', {
            'complete': True, 'training_candidates': total,
            'training_candidates_sha256': file_sha(candidate_path),
            'per_year_region': args.per_year_region, 'eligible_candidates': stats,
            'source_count_unit': 'rows, not certified unique articles',
            'source_counts': 'source_counts.json', 'schema': 'schema.json',
            'protocol': 'protocol.json', 'runtime': 'runtime.json',
            'cohort_files': [str(path.relative_to(out)) for path in files],
            'labels_used': False, 'human_reviewed': False, 'hdfs_read_only': True,
        })
        print(json.dumps(dict(stage='complete', training_candidates=total)), flush=True)
    finally:
        spark.stop()


if __name__ == '__main__':
    main()
