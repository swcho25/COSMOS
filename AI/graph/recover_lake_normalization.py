"""Resume a stopped lake normalizer in a NEW run, using bounded Parquet batches.

This is normalization/global identity recovery only. It never performs company
analysis, sentiment inference, relationship extraction, HDFS publication or SQL
loading. The old supervisor, plan, code and run remain intact. ``prepare`` takes
the old supervisor lock, refuses an active status, verifies its frozen inputs,
backs up committed SQLite state, and copies immutable referenced payloads.

``normalize`` reads one frozen source file at a time with PyArrow iter_batches.
WebHDFS GETFILESTATUS/OPEN are read-only; no Hadoop/Spark JVM or libhdfs is needed.
Each source still owns one SQLite transaction. Its payload/receipt are fsynced
before the SQLite checkpoint commits. Failed partial payloads are never reused.
Batch size bounds the number of decoded rows, not the size of one pathological
row or Parquet dictionary. No row is truncated to fit a memory target.

Hive partition columns and Spark's UTC-naive timestamp serialization are restored
before the EXISTING map_news/normalize_article functions run. The critical old
normalizer/state files must match exactly; reader conversion is separately tested.
The prepared recovery code/runtime/input hashes are checked again on every resume.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import time
from urllib.parse import quote, unquote, urlsplit
import uuid
import zlib

from lake_backfill_state import BackfillState
from lake_news_normalize import normalize_article
from prepare_lake_recent import map_news
from run_lake_backfill import RunLock, atomic_json, digest_file


SCHEMA = "lake-normalization-recovery-1"
KINDS = {"news_prepared", "news_live", "news_realtime"}
COUNTS = ("source_rows", "accepted_rows", "invalid_rows", "outside_as_of_rows")
KEEP = ("record_id", "source_dataset", "source_path", "title", "body", "url",
        "canonical_url", "publisher", "organization", "provider", "region", "language",
        "published_at", "published_date", "published_at_precision", "published_at_source",
        "collected_at", "collected_at_source", "content_hash", "identity_candidate", "quality_flags")
CRITICAL = ("lake_backfill_state.py", "lake_document_ids.py", "lake_news_normalize.py",
            "prepare_lake_recent.py", "news_content.py", "run_lake_backfill.py")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def stamp(value):
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def fingerprint(source):
    # Must remain byte-for-byte compatible with spark/backfill_lake_sources.py.
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError("Artifact path escapes its run")
    return path


def verify_file(path, expected):
    if digest_file(path) != expected:
        raise ValueError("Frozen file changed: " + str(path))


def check_space(root, minimum_bytes, extra=0):
    if shutil.disk_usage(root).free < minimum_bytes + extra:
        raise RuntimeError("DISK_FREE_BELOW_LIMIT")


def copy_verified(source, target, expected=None):
    expected = expected or digest_file(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with Path(source).open("rb") as incoming, target.open("xb") as output:
        shutil.copyfileobj(incoming, output, length=1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
    verify_file(target, expected)
    return {"sha256": expected, "bytes": target.stat().st_size}


def validate_manifest(manifest):
    if manifest.get("schema_version") != "lake-backfill-sources-1":
        raise ValueError("Unsupported frozen source manifest")
    if stamp(manifest["as_of"]).utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    seen = set()
    for source in manifest["files"]:
        key = source["id"]
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{24}", key) or key in seen:
            raise ValueError("Invalid/duplicate source id")
        seen.add(key)
        if key != hashlib.sha256(source["path"].encode()).hexdigest()[:24]:
            raise ValueError("Source path/id mismatch")
        if source["kind"] not in KINDS:
            raise ValueError("Unexpected source kind")
        path = source["path"].replace("\\", "/")
        base = manifest["roots"][source["kind"]].replace("\\", "/").rstrip("/")
        if not path.startswith(base + "/") or ".." in PurePosixPath(path).parts:
            raise ValueError("Source escapes its frozen root")
        if any(type(source[k]) is not int or source[k] < 0 for k in ("bytes", "modified_ms")):
            raise ValueError("Invalid source metadata")
    if manifest.get("source_file_count") != len(seen):
        raise ValueError("Source count mismatch")


def prepare(old_run, run_dir, *, batch_size=512, minimum_free_gb=20):
    """Clone only committed state/referenced payloads; never edit the old plan."""
    import pyarrow
    old, root = Path(old_run).resolve(), Path(run_dir).resolve()
    if root.exists() or root == old or root.is_relative_to(old) or old.is_relative_to(root):
        raise ValueError("Recovery requires a new, disjoint run directory")
    if type(batch_size) is not int or not 1 <= batch_size <= 4096:
        raise ValueError("batch_size must be between 1 and 4096")
    if type(minimum_free_gb) is not int or minimum_free_gb < 0:
        raise ValueError("minimum_free_gb must be nonnegative")
    if not (old / ".runner.lock").is_file():
        raise ValueError("Original supervisor lock is missing")
    with RunLock(old / ".runner.lock"):
        status = read_json(old / "status.json")
        if status.get("state") not in {"TERMINATED", "FAILED", "COMPLETED"}:
            raise ValueError("Stop the original supervisor and its children first")
        for key in ("runner_pid", "child_pid"):
            pid = status.get(key)
            cmdline = Path("/proc") / str(pid) / "cmdline"
            if pid and cmdline.is_file() and str(old).encode() in cmdline.read_bytes():
                raise ValueError("An original run process is still alive")
        plan = read_json(old / "plan.locked.json")
        pinned = plan["code_fingerprints"] + plan["source_fingerprints"]
        for entry in pinned:
            verify_file(entry["path"], entry["sha256"])
        code_dir = Path(__file__).resolve().parent
        old_pins = {Path(p["path"]).name: p["sha256"] for p in plan["code_fingerprints"]}
        code_pins = {}
        for name in CRITICAL:
            if name not in old_pins:
                raise ValueError("Original dependency is not pinned: " + name)
            verify_file(code_dir / name, old_pins[name])
            code_pins[name] = old_pins[name]
        code_pins[Path(__file__).name] = digest_file(__file__)
        manifest = read_json(old / "source_manifest.json")
        validate_manifest(manifest)
        check_space(old, minimum_free_gb * 1024 ** 3)
        root.mkdir(parents=True)
        check_space(root, minimum_free_gb * 1024 ** 3)
        immutable = {}
        for relative, src in (("source_manifest.json", old / "source_manifest.json"),
                              ("inputs/document_id_map.jsonl", old / "inputs/document_id_map.jsonl"),
                              ("provenance/original_plan.locked.json", old / "plan.locked.json"),
                              ("provenance/original_status.json", old / "status.json")):
            check_space(root, minimum_free_gb * 1024 ** 3, src.stat().st_size)
            immutable[relative] = copy_verified(src, root / relative)
        source = sqlite3.connect((old / "identity.sqlite").as_uri() + "?mode=ro", uri=True)
        try:
            db_bytes = source.execute("PRAGMA page_count").fetchone()[0] * source.execute("PRAGMA page_size").fetchone()[0]
            check_space(root, minimum_free_gb * 1024 ** 3, db_bytes)
            destination = sqlite3.connect(root / "identity.sqlite")
            try:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("Copied SQLite state failed quick_check")
            finally:
                destination.close()
        finally:
            source.close()
        sources = {s["id"]: s for s in manifest["files"]}
        with BackfillState(root / "identity.sqlite", read_only=True) as state:
            prior = state.get_checkpoint("prior_id_map")
            if not prior or prior["sha256"] != immutable["inputs/document_id_map.jsonl"]["sha256"]:
                raise ValueError("Prior identity map checkpoint mismatch")
            referenced = set()
            for row in state.db.execute("SELECT payload_ref FROM versions UNION ALL SELECT payload_ref FROM conflicts WHERE payload_ref IS NOT NULL"):
                ref = json.loads(row[0])
                shard = ref["shard"]
                if Path(shard).name != shard or "/" in shard or "\\" in shard:
                    raise ValueError("Unsafe inherited payload reference")
                payload = old / "normalized" / shard
                if (type(ref["offset"]) is not int or type(ref["length"]) is not int
                        or ref["offset"] < 0 or ref["length"] <= 0
                        or ref["offset"] + ref["length"] > payload.stat().st_size):
                    raise ValueError("Inherited payload reference is outside file")
                referenced.add(shard)
            checkpoints = [dict(r) for r in state.db.execute("SELECT * FROM checkpoints WHERE source_key != 'prior_id_map'")]
            for checkpoint in checkpoints:
                key = checkpoint["source_key"]
                if key not in sources or checkpoint["source_hash"] != fingerprint(sources[key]):
                    raise ValueError("Inherited source checkpoint mismatch")
                relative = "source_receipts/" + key + ".json"
                receipt = read_json(old / relative)
                if receipt["source"] != sources[key] or receipt["source_rows"] != checkpoint["row_count"]:
                    raise ValueError("Inherited receipt/checkpoint mismatch")
                payload = inside(old, receipt["payload_file"])
                if payload.parent != old / "normalized":
                    raise ValueError("Inherited payload is outside normalized directory")
                verify_file(payload, receipt["payload_sha256"])
                referenced.add(payload.name)
                immutable[relative] = copy_verified(old / relative, root / relative)
                bad = receipt["quarantine_file"]
                rejected = inside(old, bad)
                check_space(root, minimum_free_gb * 1024 ** 3, rejected.stat().st_size)
                immutable[bad] = copy_verified(rejected, inside(root, bad))
            for shard in sorted(referenced):
                relative = "normalized/" + shard
                src = old / relative
                check_space(root, minimum_free_gb * 1024 ** 3, src.stat().st_size)
                immutable[relative] = copy_verified(src, root / relative)
            inherited_summary = state.summary()
        receipt = {"schema_version": SCHEMA, "original_run": str(old), "prepared_at": now(),
                   "scope": "normalization_and_global_ids_only", "publish_enabled": False,
                   "batch_size": batch_size, "minimum_free_gb": minimum_free_gb,
                   "pyarrow_version": pyarrow.__version__, "code_sha256": code_pins,
                   "immutable_inputs": immutable, "inherited_files": len(checkpoints),
                   "inherited_identity": inherited_summary,
                   "initial_sqlite_sha256": digest_file(root / "identity.sqlite")}
        atomic_json(root / "recovery_prepared.json", receipt)
        return receipt


def spark_value(value):
    """Reproduce Row.asDict + json(default=str), with Spark session timezone UTC."""
    if isinstance(value, dt.datetime) and value.utcoffset() is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    if isinstance(value, dict):
        return {key: spark_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [spark_value(item) for item in value]
    if isinstance(value, bytes):
        return bytearray(value)  # PySpark BinaryType returns bytearray.
    return value


def parquet_rows(path, source, prepared_root, batch_size=512):
    """Decode batches; never materialize the complete file or Spark partition."""
    import pyarrow.parquet as pq
    hive = {}
    if source["kind"] == "news_prepared":
        text = source["path"].replace("\\", "/")
        base = prepared_root.replace("\\", "/").rstrip("/") + "/"
        if not text.startswith(base):
            raise ValueError("Prepared source is outside basePath")
        for part in text[len(base):].split("/")[:-1]:
            key, separator, value = part.partition("=")
            if not separator or key not in {"region", "year", "month"} or key in hive:
                raise ValueError("Unexpected prepared Hive partition")
            hive[key] = None if value == "__HIVE_DEFAULT_PARTITION__" else unquote(value)
        if set(hive) != {"region", "year", "month"}:
            raise ValueError("Prepared region/year/month partitions are required")
        for key in ("year", "month"):
            if hive[key] is not None:
                hive[key] = int(hive[key])
    with pq.ParquetFile(path, pre_buffer=False) as parquet:
        for batch in parquet.iter_batches(batch_size=batch_size, use_threads=False):
            for raw in batch.to_pylist():
                for key, value in hive.items():
                    if key in raw and raw[key] != value:
                        raise ValueError("In-file/Hive partition conflict: " + key)
                    raw[key] = value
                raw["source_path"] = source["path"]
                yield json.loads(json.dumps(spark_value(raw), default=str, ensure_ascii=False))


class LocalFiles:
    """Read-only local source transport, primarily for reproducible tests."""
    def check(self, source):
        stat = Path(source["path"]).stat()
        if (stat.st_size, stat.st_mtime_ns // 1_000_000) != (source["bytes"], source["modified_ms"]):
            raise ValueError("Frozen local source changed: " + source["path"])

    @contextlib.contextmanager
    def materialize(self, source, cache):
        self.check(source)
        yield Path(source["path"])
        self.check(source)


class WebHDFS:
    """Only GETFILESTATUS/OPEN; one bounded on-disk source cache at a time."""
    def __init__(self, endpoint, user=None):
        import requests
        uri = urlsplit(endpoint)
        if uri.scheme not in {"http", "https"} or not uri.netloc or uri.username or uri.password or uri.query or uri.fragment or uri.path not in {"", "/"}:
            raise ValueError("Expected a NameNode HTTP(S) origin")
        self.endpoint = endpoint.rstrip("/") + "/webhdfs/v1"
        self.user = user
        self.session = requests.Session()
        self.session.trust_env = False

    def get(self, path, operation, **kwargs):
        if not path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise ValueError("Expected an absolute HDFS path")
        params = {"op": operation}
        if self.user:
            params["user.name"] = self.user
        response = self.session.get(self.endpoint + quote(path, safe="/"), params=params,
                                    timeout=(10, 60), **kwargs)
        response.raise_for_status()
        return response

    def check(self, source):
        with self.get(source["path"], "GETFILESTATUS") as response:
            stat = response.json()["FileStatus"]
        if stat["type"] != "FILE" or (stat["length"], stat["modificationTime"]) != (source["bytes"], source["modified_ms"]):
            raise ValueError("Frozen HDFS source changed: " + source["path"])

    @contextlib.contextmanager
    def materialize(self, source, cache):
        self.check(source)
        path = cache / (uuid.uuid4().hex + ".parquet")
        try:
            with self.get(source["path"], "OPEN", stream=True) as response, path.open("xb") as output:
                received = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    received += len(chunk)
                    if received > source["bytes"]:
                        raise ValueError("HDFS source grew beyond its frozen length")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if path.stat().st_size != source["bytes"]:
                raise ValueError("Incomplete HDFS source download")
            self.check(source)
            yield path
            self.check(source)
        finally:
            # Only this invocation's UUID-named cache file; never a raw source.
            path.unlink(missing_ok=True)


def normalized_record(raw, source, end):
    mapped = map_news(raw, source["kind"])
    clean = normalize_article(mapped)
    reason = None
    if clean is None:
        reason = "invalid_text_or_date"
    elif not clean.get("record_id") or not clean.get("source_dataset"):
        reason = "missing_source_identity"
    elif stamp(clean["published_at"]) >= end:
        reason = "outside_as_of"
    if not reason:
        clean = {key: clean.get(key) for key in KEEP}
        clean["record_id"] = str(clean["record_id"])
    return clean, reason, mapped.get("record_id")


def compressed(clean):
    return zlib.compress(json.dumps(clean, ensure_ascii=False, allow_nan=False,
                                   separators=(",", ":")).encode("utf8"), level=3)


def validate_prepared(root):
    import pyarrow
    prepared = read_json(root / "recovery_prepared.json")
    if prepared.get("schema_version") != SCHEMA or prepared.get("publish_enabled") is not False:
        raise ValueError("Invalid normalization-only recovery contract")
    if prepared["pyarrow_version"] != pyarrow.__version__:
        raise ValueError("Recovery PyArrow version changed")
    for name, expected in prepared["code_sha256"].items():
        verify_file(Path(__file__).resolve().parent / name, expected)
    for relative, item in prepared["immutable_inputs"].items():
        verify_file(inside(root, relative), item["sha256"])
    return prepared


def probe(run_dir, transport, source_id):
    """Read/normalize one source without ingesting; compare a committed payload."""
    root = Path(run_dir).resolve()
    with RunLock(root / ".runner.lock"):
        prepared = validate_prepared(root)
        manifest = read_json(root / "source_manifest.json")
        validate_manifest(manifest)
        source = next((s for s in manifest["files"] if s["id"] == source_id), None)
        if source is None:
            raise ValueError("Unknown frozen source id")
        check_space(root, prepared["minimum_free_gb"] * 1024 ** 3, source["bytes"])
        (root / "cache").mkdir(exist_ok=True)
        expected = None
        with BackfillState(root / "identity.sqlite", read_only=True) as state:
            checkpoint = state.get_checkpoint(source_id)
            if checkpoint:
                expected = read_json(root / "source_receipts" / (source_id + ".json"))
                if checkpoint["sha256"] != fingerprint(source) or expected["source"] != source:
                    raise ValueError("Probe checkpoint/receipt mismatch")
        counts, digest = dict.fromkeys(COUNTS, 0), hashlib.sha256()
        start = time.monotonic()
        with transport.materialize(source, root / "cache") as local:
            for raw in parquet_rows(local, source, manifest["roots"]["news_prepared"], prepared["batch_size"]):
                counts["source_rows"] += 1
                clean, reason, unused = normalized_record(raw, source, stamp(manifest["as_of"]))
                if reason:
                    counts["outside_as_of_rows" if reason == "outside_as_of" else "invalid_rows"] += 1
                else:
                    counts["accepted_rows"] += 1
                    digest.update(compressed(clean))
        elapsed = time.monotonic() - start
        matched = None if expected is None else (digest.hexdigest() == expected["payload_sha256"]
                                                 and all(counts[k] == expected[k] for k in COUNTS))
        result = {"schema_version": SCHEMA, "source": source, "counts": counts,
                  "payload_sha256": digest.hexdigest(), "elapsed_seconds": elapsed,
                  "rows_per_second": counts["source_rows"] / elapsed if elapsed else None,
                  "committed_payload_comparison": matched, "identity_state_modified": False,
                  "publish_enabled": False}
        (root / "probes").mkdir(exist_ok=True)
        atomic_json(root / "probes" / (source_id + "-" + uuid.uuid4().hex + ".json"), result)
        if matched is False:
            raise ValueError("Probe differs from the committed Spark payload; inspect probe receipt")
        return result


def normalize(run_dir, transport, *, max_files=None, row_reader=parquet_rows):
    """Resume source checkpoints. A file limit pauses safely; no automatic publish."""
    root = Path(run_dir).resolve()
    with RunLock(root / ".runner.lock"):
        prepared = validate_prepared(root)
        if max_files is not None and (type(max_files) is not int or max_files < 0):
            raise ValueError("max_files must be nonnegative")
        manifest = read_json(root / "source_manifest.json")
        validate_manifest(manifest)
        end = stamp(manifest["as_of"])
        minimum = prepared["minimum_free_gb"] * 1024 ** 3
        for folder in ("normalized", "quarantine", "source_receipts", "cache"):
            (root / folder).mkdir(exist_ok=True)
        totals = dict.fromkeys(COUNTS, 0) | {"completed_files": 0}
        completed_now = 0

        def progress(state, **extra):
            atomic_json(root / "recovery_status.json", {"state": state, "pid": os.getpid(),
                        "updated_at": now(), "counts": dict(totals), "publish_enabled": False, **extra})

        progress("RUNNING")
        try:
            for control in manifest["controls"]:
                transport.check(control)
            with BackfillState(root / "identity.sqlite") as state:
                for source in manifest["files"]:
                    receipt_path = root / "source_receipts" / (source["id"] + ".json")
                    checkpoint = state.get_checkpoint(source["id"])
                    if checkpoint:
                        receipt = read_json(receipt_path)
                        if checkpoint["sha256"] != fingerprint(source) or receipt["source"] != source or receipt["source_rows"] != checkpoint["row_count"]:
                            raise ValueError("Completed source checkpoint/receipt mismatch")
                        verify_file(inside(root, receipt["payload_file"]), receipt["payload_sha256"])
                        for key in COUNTS:
                            totals[key] += receipt[key]
                        totals["completed_files"] += 1
                        continue
                    if max_files is not None and completed_now >= max_files:
                        progress("PAUSED", message="Explicit file limit reached")
                        return {"state": "PAUSED", "counts": totals}
                    check_space(root, minimum, source["bytes"])
                    counts = dict.fromkeys(COUNTS, 0)
                    stem = source["id"] + "-" + uuid.uuid4().hex
                    payload = root / "normalized" / (stem + ".bin")
                    quarantine = root / "quarantine" / (stem + ".jsonl")
                    with transport.materialize(source, root / "cache") as local:
                        with payload.open("xb") as output, quarantine.open("x", encoding="utf8") as rejected:
                            for raw in row_reader(local, source, manifest["roots"]["news_prepared"], prepared["batch_size"]):
                                counts["source_rows"] += 1
                                clean, reason, record_id = normalized_record(raw, source, end)
                                if reason:
                                    counts["outside_as_of_rows" if reason == "outside_as_of" else "invalid_rows"] += 1
                                    rejected.write(json.dumps({"source_file": source["path"], "row": counts["source_rows"],
                                                   "source_record_id": record_id, "reason": reason}, ensure_ascii=False) + "\n")
                                else:
                                    data = compressed(clean)
                                    offset = output.tell()
                                    output.write(data)
                                    state.ingest(clean, {"shard": payload.name, "offset": offset, "length": len(data)})
                                    counts["accepted_rows"] += 1
                                if counts["source_rows"] % 1000 == 0:
                                    check_space(root, minimum)
                                    progress("RUNNING", active_source=source["path"], active_counts=dict(counts))
                            output.flush()
                            os.fsync(output.fileno())
                            rejected.flush()
                            os.fsync(rejected.fileno())
                    receipt = counts | {"source": source, "payload_file": str(payload.relative_to(root)).replace("\\", "/"),
                                        "payload_sha256": digest_file(payload), "quarantine_file": str(quarantine.relative_to(root)).replace("\\", "/")}
                    atomic_json(receipt_path, receipt)
                    state.set_checkpoint(source["id"], fingerprint(source), counts["source_rows"])
                    state.commit()
                    for key in COUNTS:
                        totals[key] += counts[key]
                    totals["completed_files"] += 1
                    completed_now += 1
                    progress("RUNNING", completed_source=source["path"])
                for control in manifest["controls"]:
                    transport.check(control)
                state.finalize()
                state.commit()
                summary = state.summary()
            done = {"scope": "normalization_and_global_ids_only", "counts": totals, "identity": summary,
                    "source_manifest_sha256": digest_file(root / "source_manifest.json"), "publish_enabled": False}
            atomic_json(root / "identity_summary.json", done)
            atomic_json(root / "ingest.done.json", done)
            progress("COMPLETED")
            return {"state": "COMPLETED", **done}
        except BaseException as error:
            progress("TERMINATED" if isinstance(error, KeyboardInterrupt) else "FAILED",
                     error_type=type(error).__name__, error=str(error))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    create = sub.add_parser("prepare")
    create.add_argument("--resume-run", required=True, type=Path)
    create.add_argument("--run-dir", required=True, type=Path)
    create.add_argument("--batch-size", type=int, default=512)
    create.add_argument("--minimum-free-gb", type=int, default=20)
    for mode in ("normalize", "probe"):
        execute = sub.add_parser(mode)
        execute.add_argument("--run-dir", required=True, type=Path)
        transport = execute.add_mutually_exclusive_group(required=True)
        transport.add_argument("--webhdfs-url")
        transport.add_argument("--local-files", action="store_true")
        execute.add_argument("--hdfs-user")
        if mode == "normalize":
            execute.add_argument("--max-files", type=int)
        else:
            execute.add_argument("--source-id", required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        result = prepare(args.resume_run, args.run_dir, batch_size=args.batch_size, minimum_free_gb=args.minimum_free_gb)
    else:
        def stop(signum, frame):
            raise KeyboardInterrupt("Termination requested")
        signal.signal(signal.SIGTERM, stop)
        client = LocalFiles() if args.local_files else WebHDFS(args.webhdfs_url, args.hdfs_user)
        result = (probe(args.run_dir, client, args.source_id) if args.mode == "probe"
                  else normalize(args.run_dir, client, max_files=args.max_files))
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
