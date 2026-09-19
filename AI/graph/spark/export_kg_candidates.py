"""Export ALL existing dict-v1.3 article/company candidates as bounded JSONL.

Spark performs joins/normalization/sharded writes; no LLM is loaded. No date,
confidence, sport, article representative, sampling or fixed-company restriction
is added to the existing completed NER candidate universe. This does not discover
companies missed by that NER or include subsequent realtime/news snapshots.

Only a NEW owner sandbox trial directory can be written. Inputs are explicit
inventoried Parquet files and completed NER controls, checked before and after.
Missing joins, ambiguous source identities/names and empty text fail closed with
counts/failure metadata; they are not silently dropped. Successful manifest plus
_VERIFIED is written LAST. Partial exports have neither and cannot be consumed.

Article bodies never use collect/toLocalIterator. DISK_ONLY Spark intermediates
bound RAM. Distributed text output has maxRecordsPerFile; the driver streams
each completed file entirely inside Java for SHA/line count (64KiB reader), so
it receives only small metadata, never entire bodies. A giant single input row
is not truncated. Resume inference by row SHA; failed export requires a NEW run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from kg_candidate_export import (SCHEMA, NEWS, RUNS, SOURCE_FIELDS, candidate_map,
                                 source_map, canonical, digest, inventory_fingerprint,
                                 validate_output_path, assert_counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--partitions", type=int, default=64)
    parser.add_argument("--max-records-per-file", type=int, default=1000)
    parser.add_argument("--model-receipt-sha256", required=True)
    args = parser.parse_args()
    out = validate_output_path(args.out)
    if not 2 <= args.partitions <= 1024 or not 1 <= args.max_records_per_file <= 10000:
        raise ValueError("Invalid partition/shard size")
    if len(args.model_receipt_sha256) != 64 or any(c not in "0123456789abcdef" for c in args.model_receipt_sha256):
        raise ValueError("Pinned model receipt SHA256 required")
    from pyspark import StorageLevel
    from pyspark.sql import SparkSession, functions as F, types as T
    spark = (SparkSession.builder.appName("cosmos-kg-full-candidate-export")
             .config("spark.sql.sources.partitionColumnTypeInference.enabled", "false")
             .config("spark.sql.session.timeZone", "UTC")
             .config("spark.sql.autoBroadcastJoinThreshold", "-1")
             .config("spark.sql.shuffle.partitions", str(args.partitions))
             .config("spark.sql.files.maxPartitionBytes", "33554432")
             .config("spark.sql.parquet.columnarReaderBatchSize", "512")
             .config("spark.sql.adaptive.enabled", "false")
             .config("spark.speculation", "false").getOrCreate())
    spark.sparkContext.setLogLevel("WARN")
    jvm = spark._jvm
    fs = jvm.org.apache.hadoop.fs.FileSystem.get(spark._jsc.hadoopConfiguration())
    JP = jvm.org.apache.hadoop.fs.Path
    if fs.exists(JP(out)):
        raise FileExistsError("Use a new trial output: " + out)
    if not fs.mkdirs(JP(out)):
        raise OSError("Cannot create trial output")
    kept = []

    def save(name, payload):
        stream = fs.create(JP(out + "/" + name), False)
        try:
            stream.write(bytearray((canonical(payload) + "\n").encode("utf-8")))
            stream.hflush()
        finally:
            stream.close()

    def status(path):
        stat = fs.getFileStatus(JP(path))
        checksum = fs.getFileChecksum(JP(path))
        if checksum is None:
            raise ValueError("HDFS checksum unavailable: " + path)
        return dict(path=path, bytes=int(stat.getLen()), modified_ms=int(stat.getModificationTime()),
                    checksum=str(checksum))

    def inventory(root, suffix):
        entries = []
        iterator = fs.listFiles(JP(root), True)
        while iterator.hasNext():
            path = str(iterator.next().getPath().toUri().getPath())
            if path.endswith(suffix):
                entries.append(status(path))
        if not entries:
            raise ValueError("No files: " + root)
        return sorted(entries, key=lambda item: item["path"])

    def read_control(path):
        entry = status(path)
        if entry["bytes"] > 4 * 1024 * 1024:
            raise ValueError("Unexpectedly large completion control")
        stream = fs.open(JP(path))
        try:
            raw = bytes(stream.readAllBytes())
        finally:
            stream.close()
        return entry, hashlib.sha256(raw).hexdigest(), json.loads(raw) if raw.strip() else None

    def persist(df):
        df.persist(StorageLevel.DISK_ONLY)
        kept.append(df)
        return df

    def require_columns(df, fields, name):
        missing = set(fields) - set(df.columns)
        if missing:
            raise ValueError(f"{name}: missing fields {sorted(missing)}")

    def output_receipts(name):
        """Java streams bytes; only count/digest cross the Py4J boundary."""
        receipts = []
        iterator = fs.listFiles(JP(out + "/" + name), True)
        while iterator.hasNext():
            stat = iterator.next()
            path = str(stat.getPath().toUri().getPath())
            if not stat.getPath().getName().startswith("part-"):
                continue
            md = jvm.java.security.MessageDigest.getInstance("SHA-256")
            stream = jvm.java.security.DigestInputStream(fs.open(JP(path)), md)
            reader = jvm.java.io.BufferedReader(jvm.java.io.InputStreamReader(stream, "UTF-8"), 65536)
            try:
                rows = int(reader.lines().count())
            finally:
                reader.close()
            receipts.append(dict(path=path[len(out) + 1:], sha256=bytes(md.digest()).hex(),
                                 rows=rows, bytes=int(stat.getLen())))
        return sorted(receipts, key=lambda item: item["path"])

    def write_text(df, column, name):
        df.select(F.col(column).alias("value")).write.mode("errorifexists").option(
            "maxRecordsPerFile", args.max_records_per_file).text(out + "/" + name)
        return output_receipts(name)

    counts = {}
    try:
        sources = {"news": inventory(NEWS, ".parquet")}
        controls, manifests = {}, {}
        for region, root in RUNS.items():
            sources[region] = inventory(root + "/data", ".parquet")
            for name in ("manifest.json", "_VERIFIED"):
                entry, sha, value = read_control(root + "/" + name)
                controls[region + "/" + name] = dict(entry=entry, sha256=sha)
                if name == "manifest.json":
                    if (value.get("model_version") != "dict-v1.3" or
                            value.get("input_dataset_version") != "20260908-prepared" or
                            value.get("input_filter", {}).get("region") != region or
                            value.get("input_filter", {}).get("limit") not in (None, 0)):
                        raise ValueError("Wrong/incomplete NER manifest: " + region)
                    manifests[region] = value
        all_entries = [entry for group in sources.values() for entry in group] + [v["entry"] for v in controls.values()]
        fingerprint = inventory_fingerprint(all_entries)
        code_names = ("kg_candidate_export.py", "news_content.py", "knowledge_graph_news_preview.py",
                      "knowledge_graph_relevance_policy.py", "relevance_sentence_evidence.py")
        code = {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in code_names}
        code["spark/export_kg_candidates.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        code["../ner/matcher.py"] = hashlib.sha256((HERE.parent / "ner/matcher.py").read_bytes()).hexdigest()
        protocol = dict(schema=SCHEMA, started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                        source_inventory_fingerprint=fingerprint, source_inventory=sources,
                        completion_controls=controls, ner_manifests=manifests, code_sha256=code,
                        model_receipt_sha256=args.model_receipt_sha256,
                        scope="all_existing_pairs_in_two_completed_dict_v1.3_runs_all_years",
                        filters_added=[], truncation=False, source_split_filter=False,
                        canonical_input_fields=["article_id", "ticker", "company_name", "published_at", "title", "body", "source_url"],
                        output=args.out, partitions=args.partitions, max_records_per_file=args.max_records_per_file,
                        production_approved=False, human_gold=False, ready_for_gnn_training=False,
                        spark_version=spark.version, spark_master=spark.sparkContext.master)
        save("protocol.json", protocol)
        news = spark.read.option("basePath", NEWS).parquet(*[v["path"] for v in sources["news"]])
        require_columns(news, ("record_id", "title", "body"), "news")
        if not any(x in news.columns for x in ("published_at_raw", "published_at", "published_date")):
            raise ValueError("Source publication schema needs explicit mapping")
        counts["all_source_rows"] = news.count()
        mentions = None
        for region in RUNS:
            frame = spark.read.option("basePath", RUNS[region] + "/data").parquet(*[v["path"] for v in sources[region]])
            require_columns(frame, ("record_id", "ticker", "name_official"), "mentions")
            n = frame.count()
            counts[region + "_mention_rows"] = n
            if n != manifests[region]["counts"]["data_rows"]:
                raise ValueError("NER data count differs from completion manifest")
            frame = frame.withColumn("mention_source_path", F.input_file_name()).withColumn("mention_run", F.lit(region))
            mentions = frame if mentions is None else mentions.unionByName(frame, allowMissingColumns=True)
        mentions = persist(mentions)
        counts["all_mention_rows"] = mentions.count()
        blank = lambda key: F.col(key).isNull() | (F.length(F.trim(F.col(key))) == 0)
        counts["invalid_mention_rows"] = mentions.filter(blank("record_id") | blank("ticker")).count()
        counts["unresolved_company_name_rows"] = mentions.filter(blank("name_official")).count()
        pair_names = persist(mentions.select("record_id", "ticker", "name_official").distinct())
        counts["ambiguous_pair_names"] = pair_names.groupBy("record_id", "ticker").count().filter("count > 1").count()
        counts["mention_unique_pairs"] = pair_names.select("record_id", "ticker").distinct().count()
        keys = pair_names.select("record_id").distinct()
        counts["mention_unique_articles"] = keys.count()
        counts["missing_source_article_ids"] = keys.join(news.select("record_id").distinct(), "record_id", "left_anti").count()
        # Fail before any missing/invalid candidate can be silently dropped.
        if any(counts[key] for key in ("invalid_mention_rows", "unresolved_company_name_rows", "ambiguous_pair_names", "missing_source_article_ids")):
            raise ValueError("Candidate identity/join failures; see failure.json counts")
        selected = news.withColumn("source_path", F.input_file_name()).join(keys, "record_id", "left_semi")
        selections = []
        for name in (*SOURCE_FIELDS, "source_path"):
            if name in selected.columns:
                selections.append(F.col(name).cast("string").alias(name) if name not in ("text_eligible", "is_body_representative") else F.col(name))
        source_schema = "record_id string, canonical_article_sha256 string, article_json string, source_provenance_json string, empty_text boolean"
        normalized = persist(spark.createDataFrame(selected.select(*selections).rdd.mapPartitions(source_map), source_schema))
        counts["matched_source_rows"] = normalized.count()
        unique_articles = persist(normalized.select("record_id", "canonical_article_sha256", "article_json", "empty_text").distinct())
        counts["ambiguous_source_article_ids"] = unique_articles.groupBy("record_id").count().filter("count > 1").count()
        counts["empty_cleaned_articles"] = unique_articles.filter("empty_text").count()
        counts["exact_duplicate_source_rows"] = counts["matched_source_rows"] - unique_articles.count()
        if counts["ambiguous_source_article_ids"] or counts["empty_cleaned_articles"]:
            raise ValueError("Ambiguous/empty source articles; see failure.json counts")
        candidate_schema = "article_id string, ticker string, input_sha256 string, input_json string, provenance_json string"
        joined = unique_articles.join(pair_names, "record_id")
        candidates = persist(spark.createDataFrame(joined.rdd.mapPartitions(candidate_map), candidate_schema))
        counts["exported_unique_pairs"] = candidates.count()
        assert_counts(counts)
        save("counts.json", counts)
        # Repartition by the stable input hash; Spark filenames themselves are run-specific.
        candidates = persist(candidates.repartition(args.partitions, "input_sha256").sortWithinPartitions("input_sha256"))
        files = write_text(candidates, "input_json", "inputs")
        provenance = write_text(candidates, "provenance_json", "provenance")
        source_provenance = write_text(normalized, "source_provenance_json", "source_provenance")
        mention_provenance = write_text(mentions.select(F.to_json(F.struct(*mentions.columns), {"ignoreNullFields": "false"}).alias("json")), "json", "mention_provenance")
        if sum(v["rows"] for v in files) != counts["exported_unique_pairs"]:
            raise ValueError("Written input denominator mismatch")
        if sum(v["rows"] for v in provenance) != counts["exported_unique_pairs"] or sum(v["rows"] for v in source_provenance) != counts["matched_source_rows"] or sum(v["rows"] for v in mention_provenance) != counts["all_mention_rows"]:
            raise ValueError("Written provenance denominator mismatch")
        # Re-inventory, detecting new/missing files as well as byte/status mutations.
        after = inventory(NEWS, ".parquet")
        for root in RUNS.values():
            after.extend(inventory(root + "/data", ".parquet"))
            after.extend(status(root + "/" + name) for name in ("manifest.json", "_VERIFIED"))
        if inventory_fingerprint(after) != fingerprint:
            raise ValueError("Source inventory changed during export")
        manifest = dict(schema=SCHEMA, complete=True, counts=counts, files=files,
                        provenance_files=provenance, source_provenance_files=source_provenance,
                        mention_provenance_files=mention_provenance,
                        expected_unique_pairs=counts["exported_unique_pairs"],
                        source_inventory_fingerprint=fingerprint,
                        protocol_sha256=hashlib.sha256((canonical(protocol) + "\n").encode()).hexdigest(),
                        human_gold=False, production_approved=False, ready_for_gnn_training=False,
                        finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        save("manifest.json", manifest)
        save("_VERIFIED", {"manifest_sha256": hashlib.sha256((canonical(manifest) + "\n").encode()).hexdigest()})
        print(canonical({"complete": True, "out": out, "counts": counts, "input_parts": len(files)}), flush=True)
    except Exception as error:
        save("failure.json", dict(complete=False, counts=counts, error_type=type(error).__name__, error=str(error)))
        raise
    finally:
        for frame in reversed(kept):
            frame.unpersist()
        spark.stop()


if __name__ == "__main__":
    main()
