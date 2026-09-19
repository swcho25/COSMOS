"""Bounded, restartable analysis of finalized global news representatives.

SQLite is read only in the main process. Payloads are independent zlib JSON blobs
addressed by byte offset. Completed output shards are immutable; interrupted
attempts are retained separately. This stage never publishes HDFS, trains models,
loads sentiment models, or applies a current ownership list to historical news.
"""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
import csv
import hashlib
import itertools
import json
import multiprocessing
import os
from pathlib import Path, PurePosixPath
import sys
import time
import uuid
import zlib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "ner"))
from lake_news_normalize import analyze_article
import matcher as matcher_module
from matcher import CompanyMatcher
from relationship_score_components import timestamp

VERSION = "lake-backfill-analysis-v1"
NEWS_DATASETS = ("cleaned_news", "news_mentions", "news_relationships", "sentiment_inputs")
KEYS = {
    "cleaned_news": ["document_id"],
    "news_mentions": ["document_id", "ticker"],
    "news_relationships": ["document_id", "src_ticker", "dst_ticker", "rel_type",
                           "sentence_start", "source_start", "target_start"],
    "sentiment_inputs": ["document_id"],
    "document_id_map": ["identity_key"],
}
ROOTS = {
    "cleaned_news": "/data-lake/cleaned/news",
    "news_mentions": "/data-lake/analyzed/news/company-mentions",
    "news_relationships": "/data-lake/analyzed/news/relationships",
    "document_id_map": "/data-lake/metadata/id-maps/news",
    "sentiment_inputs": None,
}
LONG_FIELDS = {"duplicate_count", "version_count", "n_mentions", "start", "end",
               "sentence_start", "sentence_end", "source_start", "source_end", "target_start", "target_end"}
DOUBLE_FIELDS = {"confidence", "sentiment_score", "score"}
_MATCHER = None


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _json_new(path, value):
    with Path(path).open("xb") as stream:
        stream.write(encoded(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _install_json(path, value):
    """Install complete metadata under the exclusive run lock; retain failed attempts."""
    temporary = path.with_name("." + path.name + ".attempt-" + uuid.uuid4().hex)
    _json_new(temporary, value)
    if path.exists():
        raise FileExistsError(path)
    temporary.rename(path)


def _progress(out, **values):
    path = out / (".progress-" + uuid.uuid4().hex + ".json")
    _json_new(path, {"schema_version": VERSION, **values})
    for attempt in range(6):
        try:
            os.replace(path, out / "progress.json")
            return
        except PermissionError:
            # Windows readers/antivirus can briefly deny atomic replacement.
            # Never fall back to truncating a progress file that another reader uses.
            if os.name != "nt" or attempt == 5:
                raise
            time.sleep(.02 * (attempt + 1))


@contextmanager
def _exclusive_run(out):
    """An OS lock is released on process death, so restarts need no stale PID edits."""
    with (out / ".analysis.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class PayloadReader:
    """At most max_open files and one bounded decoded article in this reader."""
    def __init__(self, root, max_open=16, max_payload_bytes=64 * 1024 * 1024):
        if not 1 <= max_open <= 16:
            raise ValueError("max_open must be between 1 and 16")
        self.root = Path(root).resolve()
        self.max_open, self.max_payload_bytes = max_open, max_payload_bytes
        self.handles = OrderedDict()

    def close(self):
        for stream in self.handles.values():
            stream.close()
        self.handles.clear()

    def read(self, reference):
        name, offset, length = reference["shard"], reference["offset"], reference["length"]
        relative = PurePosixPath(name)
        if (not isinstance(name, str) or not name or "\\" in name or relative.is_absolute()
                or ".." in relative.parts or ":" in name):
            raise ValueError("Payload shard must be a relative path under normalized_dir")
        path = (self.root / Path(*relative.parts)).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("Payload reference escapes normalized_dir")
        if (type(offset) is not int or offset < 0 or type(length) is not int or length <= 0
                or length > self.max_payload_bytes):
            raise ValueError("Invalid or oversized payload byte range")
        stream = self.handles.pop(path, None)
        if stream is None:
            stream = path.open("rb")
        self.handles[path] = stream
        while len(self.handles) > self.max_open:
            self.handles.popitem(last=False)[1].close()
        if offset + length > os.fstat(stream.fileno()).st_size:
            raise ValueError("Payload byte range extends past file")
        stream.seek(offset)
        compressed = stream.read(length)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, self.max_payload_bytes + 1)
        if (len(raw) > self.max_payload_bytes or decoder.unconsumed_tail
                or not decoder.eof or decoder.unused_data):
            raise ValueError("Oversized, truncated, or non-single-zlib payload")
        row = json.loads(raw)
        if not isinstance(row, dict):
            raise ValueError("Payload must be a JSON object")
        return row


def _merge(left, right):
    if left is None:
        return right
    if right is None or left == right:
        return left
    if isinstance(left, str) and isinstance(right, str) and {left, right} <= {"long", "double"}:
        return "double"
    if isinstance(left, dict) and isinstance(right, dict):
        if "struct" in left and "struct" in right:
            a, b = left["struct"], right["struct"]
            return {"struct": {key: _merge(a.get(key), b.get(key)) for key in a.keys() | b.keys()}}
        if "array" in left and "array" in right:
            return {"array": _merge(left["array"], right["array"])}
    raise ValueError(f"Incompatible JSON types: {left!r} / {right!r}")


def _observed(value, name=""):
    if value is None:
        result = None
    elif isinstance(value, bool):
        result = "boolean"
    elif isinstance(value, int):
        result = "long"
    elif isinstance(value, float):
        result = "double"
    elif isinstance(value, str):
        result = "string"
    elif isinstance(value, list):
        child = None
        for item in value:
            child = _merge(child, _observed(item))
        result = {"array": child}
    elif isinstance(value, dict):
        result = {"struct": {key: _observed(item, key) for key, item in value.items()}}
    else:
        raise ValueError("Unsupported JSON value type")
    fixed = "double" if name in DOUBLE_FIELDS else "long" if name in LONG_FIELDS else None
    return _merge(fixed, result)


def _base_schema(name):
    strings = {
        "cleaned_news": ["document_id", "record_id", "source_dataset", "source_path", "title", "body",
                         "url", "canonical_url", "publisher", "region", "language", "published_at",
                         "published_date", "published_at_precision", "published_at_source", "collected_at",
                         "content_hash", "identity_candidate", "relationship_language_support"],
        "news_mentions": ["document_id", "record_id", "published_at", "published_date", "ticker",
                          "name_official", "first_pos", "method", "offset_basis"],
        "news_relationships": ["document_id", "record_id", "source_record_id", "published_at", "published_date",
                               "src_ticker", "dst_ticker", "rel_type", "pattern", "sentence", "offset_basis", "source"],
        "sentiment_inputs": ["document_id", "title", "body", "region", "language"],
        "document_id_map": ["identity_key", "document_id", "representative_record_id",
                            "representative_source_dataset", "available_at"],
    }
    fields = {key: "string" for key in strings[name]}
    if name == "cleaned_news":
        fields.update(duplicate_count="long", version_count="long", quality_flags={"array": "string"},
                      analysis_flags={"array": "string"})
    elif name == "news_mentions":
        fields.update(n_mentions="long", confidence="double", is_sports="boolean",
                      aliases={"array": "string"}, spans={"array": {"struct": {
                          "alias": "string", "start": "long", "end": "long", "quote": "string",
                          "method": "string", "confidence": "double"}}})
    elif name == "news_relationships":
        fields.update(confidence="double", **{key: "long" for key in (
            "sentence_start", "sentence_end", "source_start", "source_end", "target_start", "target_end")})
    return {"struct": fields}


def _spark_schema(tree):
    if tree is None:
        return "string"
    if isinstance(tree, str):
        return tree
    if "array" in tree:
        return {"type": "array", "elementType": _spark_schema(tree["array"]), "containsNull": True}
    if not tree["struct"]:
        raise ValueError("Empty nested structs cannot be stored in Parquet")
    return {"type": "struct", "fields": [
        {"name": key, "type": _spark_schema(value), "nullable": True, "metadata": {}}
        for key, value in sorted(tree["struct"].items())]}


class _Writer:
    def __init__(self, path, name):
        self.path, self.name = path, name
        self.stream = path.open("xb")
        self.hash, self.count, self.bytes = hashlib.sha256(), 0, 0
        self.tree = _base_schema(name)

    def add(self, row):
        if any(row.get(key) is None for key in KEYS[self.name]):
            raise ValueError("Null publication key in " + self.name)
        block = encoded(row) + b"\n"
        self.tree = _merge(self.tree, _observed(row))
        self.stream.write(block)
        self.hash.update(block)
        self.count += 1
        self.bytes += len(block)

    def close(self):
        if not self.stream.closed:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()

    def manifest(self):
        return {"name": self.name, "local_file": self.path.name, "row_count": self.count,
                "bytes": self.bytes, "sha256": self.hash.hexdigest(), "key_columns": KEYS[self.name],
                "observed_schema": self.tree, "schema": _spark_schema(self.tree)}


def _initialize_worker(aliases):
    global _MATCHER
    _MATCHER = CompanyMatcher.from_csv(aliases)


def _worker(article):
    # Historical affiliate evidence is unavailable here. Never apply current
    # ownership to old news and thereby erase historical COMPETE evidence.
    return analyze_article(article, _MATCHER, affiliate_pairs=())


def _article(metadata, reader, semantic_hasher):
    row = reader.read(metadata["payload_ref"])
    for key in ("record_id", "source_dataset"):
        if row.get(key) != metadata[key]:
            raise ValueError("Representative payload identity mismatch: " + key)
    if timestamp(row["published_at"]) != timestamp(metadata["published_at"]):
        raise ValueError("Representative publication time mismatch")
    if semantic_hasher(row) != metadata["semantic_hash"]:
        raise ValueError("Representative payload semantic hash mismatch")
    uuid.UUID(metadata["document_id"])
    if row.get("document_id") not in (None, metadata["document_id"]):
        raise ValueError("Payload contains a conflicting prior document_id")
    row.update(document_id=metadata["document_id"], duplicate_count=metadata["duplicate_count"],
               version_count=metadata.get("version_count", 1))
    if not isinstance(row.get("title"), str) or not isinstance(row.get("body"), str):
        raise ValueError("Normalized title/body must be strings")
    return row


def _write_analysis(writers, article, result, tickers, flags):
    text = article["title"] + "\n" + article["body"]
    article.update(analysis_flags=result["analysis_flags"],
                   relationship_language_support=result["relationship_language_support"])
    writers["cleaned_news"].add(article)
    flags.update(result["analysis_flags"])
    seen = set()
    for mention in result["mentions"]:
        if mention["ticker"] not in tickers or mention["ticker"] in seen:
            raise ValueError("Unknown or repeated company mention")
        seen.add(mention["ticker"])
        for span in mention["spans"]:
            if text[span["start"]:span["end"]] != span["quote"]:
                raise ValueError("Mention quote offset mismatch")
        writers["news_mentions"].add({**mention, "document_id": article["document_id"]})
    relation_keys = set()
    for relation in result["relationships"]:
        if (relation["src_ticker"] not in seen or relation["dst_ticker"] not in seen
                or relation["src_ticker"] == relation["dst_ticker"]):
            raise ValueError("Invalid relationship company reference")
        if text[relation["sentence_start"]:relation["sentence_end"]] != relation["sentence"]:
            raise ValueError("Relationship quote offset mismatch")
        relation = {**relation, "document_id": article["document_id"], "record_id": article["document_id"],
                    "source_record_id": article["record_id"], "source": "NEWS"}
        key = tuple(relation[key] for key in KEYS["news_relationships"])
        if key in relation_keys:
            raise ValueError("Duplicate relationship evidence key")
        relation_keys.add(key)
        writers["news_relationships"].add(relation)
    if result["mentions"]:
        writers["sentiment_inputs"].add({"document_id": article["document_id"], "title": article["title"],
                                         "body": article["body"][:200], "region": article.get("region"),
                                         "language": article.get("language")})


def _batches(references, reader, semantic_hasher):
    # A large individual article is allowed up to PayloadReader's explicit cap;
    # no whole 10,000-article shard is decoded into memory.
    batch, size = [], 0
    for reference in references:
        article = _article(reference, reader, semantic_hasher)
        batch.append(article)
        size += len(article["title"].encode("utf-8")) + len(article["body"].encode("utf-8"))
        if len(batch) >= 8 or size >= 4 * 1024 * 1024:
            yield batch
            batch, size = [], 0
    if batch:
        yield batch


def _check_completed(directory, expected, names):
    manifest = json.loads((directory / "chunk_manifest.json").read_text(encoding="utf-8"))
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("Completed shard belongs to different inputs/configuration: " + str(directory))
    if {item["name"] for item in manifest["files"]} != set(names) or len(manifest["files"]) != len(names):
        raise ValueError("Incomplete completed-shard file inventory")
    for item in manifest["files"]:
        name = item["name"]
        if item["local_file"] != name + ".jsonl" or item["key_columns"] != KEYS[name]:
            raise ValueError("Completed shard filename/key contract mismatch")
        path = directory / item["local_file"]
        tree, count, size, checksum = _base_schema(name), 0, 0, hashlib.sha256()
        with path.open("rb") as stream:
            for block in stream:
                if not block.strip():
                    raise ValueError("Unexpected blank output line")
                row = json.loads(block)
                if any(row.get(key) is None for key in KEYS[name]):
                    raise ValueError("Null key in completed shard")
                tree = _merge(tree, _observed(row))
                count += 1
                size += len(block)
                checksum.update(block)
        if ((count, size, checksum.hexdigest()) != (item["row_count"], item["bytes"], item["sha256"])
                or tree != item["observed_schema"] or _spark_schema(tree) != item["schema"]):
            raise ValueError("Completed shard checksum/count/schema changed: " + str(path))
    return manifest


def _chunk(out, kind, index, records, contract_hash, tickers, reader, semantic_hasher,
           executor, matcher, on_article):
    names = NEWS_DATASETS if kind == "news" else ("document_id_map",)
    expected = {"schema_version": VERSION, "kind": kind, "index": index,
                "input_count": len(records), "input_sha256": hashlib.sha256(encoded(records)).hexdigest(),
                "contract_sha256": contract_hash}
    destination = out / kind / f"shard-{index:06d}"
    if destination.exists():
        completed = _check_completed(destination, expected, names)
        if kind == "news":
            on_article(len(records), reused=True)
        return destination, completed
    attempt = out / "attempts" / (f"{kind}-{index:06d}-" + uuid.uuid4().hex)
    attempt.mkdir(parents=True, exist_ok=False)
    writers = {name: _Writer(attempt / (name + ".jsonl"), name) for name in names}
    flags = Counter()
    try:
        if kind == "news":
            ids = [row["document_id"] for row in records]
            if len(set(ids)) != len(ids):
                raise ValueError("Duplicate representative document_id within shard")
            for articles in _batches(records, reader, semantic_hasher):
                results = (executor.map(_worker, articles) if executor is not None else
                           (analyze_article(row, matcher, affiliate_pairs=()) for row in articles))
                for article, result in zip(articles, results):
                    _write_analysis(writers, article, result, tickers, flags)
                    on_article(1, reused=False)
        else:
            for row in records:
                writers["document_id_map"].add(row)
        for writer in writers.values():
            writer.close()
        completed = {**expected, "analysis_flags": dict(flags), "files": [writer.manifest() for writer in writers.values()]}
        _json_new(attempt / "chunk_manifest.json", completed)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError("Output shard appeared concurrently")
        attempt.rename(destination)
        return destination, completed
    finally:
        for writer in writers.values():
            writer.close()


def run_analysis(state, *, normalized_dir, out_dir, aliases_path, companies_path,
                 expected_aliases_sha256, expected_companies_sha256, state_fingerprint=None,
                 shard_size=10000, id_map_shard_size=100000, workers=1, semantic_hasher=None):
    """Analyze a finalized BackfillState; return the immutable analysis manifest."""
    if type(shard_size) is not int or not 1 <= shard_size <= 10000:
        raise ValueError("shard_size must be 1..10000")
    if type(id_map_shard_size) is not int or not 1 <= id_map_shard_size <= 100000:
        raise ValueError("id_map_shard_size must be 1..100000")
    if type(workers) is not int or workers not in (1, 2):
        raise ValueError("workers must be 1 or 2")
    summary = state.summary()
    if summary.get("finalized") is not True or not summary.get("fingerprint"):
        raise ValueError("Global identity state must be finalized before analysis")
    if state_fingerprint is not None and summary["fingerprint"] != state_fingerprint:
        raise ValueError("Frozen state fingerprint mismatch")
    if semantic_hasher is None:
        from lake_backfill_state import semantic_hash
        semantic_hasher = semantic_hash
    aliases, companies, out = Path(aliases_path), Path(companies_path), Path(out_dir)
    for path, expected in ((aliases, expected_aliases_sha256), (companies, expected_companies_sha256)):
        if digest(path) != expected:
            raise ValueError("Immutable company/alias input hash mismatch: " + str(path))
    with companies.open(encoding="utf-8-sig", newline="") as stream:
        company_rows = list(csv.DictReader(stream))
    tickers = {row["ticker"] for row in company_rows}
    if not tickers or len(tickers) != len(company_rows) or "" in tickers:
        raise ValueError("Company universe is empty or has duplicate/blank tickers")
    code_files = [Path(__file__), HERE / "lake_news_normalize.py", HERE / "extract_relations.py",
                  HERE / "news_content.py", Path(matcher_module.__file__).resolve()]
    contract = {"schema_version": VERSION, "state_fingerprint": summary["fingerprint"],
                "normalized_dir": str(Path(normalized_dir).resolve()), "aliases_sha256": expected_aliases_sha256,
                "companies_sha256": expected_companies_sha256, "shard_size": shard_size,
                "id_map_shard_size": id_map_shard_size,
                "implementation_sha256": {path.name: digest(path) for path in code_files},
                "affiliate_policy": "unavailable_historically_no_current_ownership_filter"}
    contract_hash = hashlib.sha256(encoded(contract)).hexdigest()
    out.mkdir(parents=True, exist_ok=True)
    with _exclusive_run(out):
        contract_path = out / "analysis_contract.json"
        if contract_path.exists():
            if json.loads(contract_path.read_text(encoding="utf-8")) != contract:
                raise ValueError("Cannot resume with changed input/code/shard contract; choose a new output directory")
        else:
            _install_json(contract_path, contract)
        reader = PayloadReader(normalized_dir)
        executor = None
        totals, descriptors, schemas = Counter(), {name: [] for name in KEYS}, {name: _base_schema(name) for name in KEYS}
        flags, processed, news_chunks, map_chunks = Counter(), 0, 0, 0
        def progress(count, *, reused=False):
            nonlocal processed
            previous = processed
            processed += count
            if processed // 1000 != previous // 1000:
                _progress(out, status="ANALYZING", articles_processed=processed,
                          completed_news_shards=news_chunks, completed_id_map_shards=map_chunks)
        def collect(directory, completed):
            flags.update(completed["analysis_flags"])
            for item in completed["files"]:
                name = item["name"]
                totals[name] += item["row_count"]
                schemas[name] = _merge(schemas[name], item["observed_schema"])
                descriptors[name].append({key: value for key, value in item.items()
                                          if key not in ("name", "observed_schema", "schema")})
                descriptors[name][-1]["local_file"] = (directory / item["local_file"]).relative_to(out).as_posix()
        try:
            _progress(out, status="ANALYZING", articles_processed=0, completed_news_shards=0)
            if workers == 2:
                executor = ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn"),
                                               initializer=_initialize_worker, initargs=(str(aliases.resolve()),))
                matcher = None
            else:
                matcher = CompanyMatcher.from_csv(aliases)
            refs = iter(state.iter_representatives())
            while records := list(itertools.islice(refs, shard_size)):
                destination, completed = _chunk(out, "news", news_chunks, records, contract_hash,
                                                tickers, reader, semantic_hasher, executor, matcher, progress)
                collect(destination, completed)
                news_chunks += 1
                _progress(out, status="ANALYZING", articles_processed=processed, completed_news_shards=news_chunks)
            identities = iter(state.iter_identity_map())
            last_key = None
            while records := list(itertools.islice(identities, id_map_shard_size)):
                for row in records:
                    key = row["identity_key"]
                    if not isinstance(key, str) or (last_key is not None and key <= last_key):
                        raise ValueError("Identity map must be globally sorted with unique keys")
                    last_key = key
                destination, completed = _chunk(out, "id_map", map_chunks, records, contract_hash,
                                                tickers, reader, semantic_hasher, executor, matcher, progress)
                collect(destination, completed)
                map_chunks += 1
                _progress(out, status="WRITING_ID_MAP", articles_processed=processed,
                          completed_news_shards=news_chunks, completed_id_map_shards=map_chunks)
            for kind, count in (("news", news_chunks), ("id_map", map_chunks)):
                parent = out / kind
                actual = {path.name for path in parent.glob("shard-*")} if parent.exists() else set()
                if actual != {f"shard-{index:06d}" for index in range(count)}:
                    raise ValueError("Unexpected completed shards outside current input stream")
            if state.summary().get("fingerprint") != summary["fingerprint"]:
                raise ValueError("Identity state changed during analysis")
            for path, expected in ((aliases, expected_aliases_sha256), (companies, expected_companies_sha256)):
                if digest(path) != expected:
                    raise ValueError("Company/alias file changed during analysis")
            datasets = []
            for name in KEYS:
                schema = _spark_schema(schemas[name])
                files = [{**item, "schema": schema} for item in descriptors[name]]
                datasets.append({"name": name, "target_root": ROOTS[name], "key_columns": KEYS[name],
                                 "schema": schema, "row_count": totals[name], "files": files})
            manifest = {"schema_version": VERSION, "status": "LOCAL_ANALYSIS_COMPLETE_NOT_PUBLISHED",
                        "contract": contract, "contract_sha256": contract_hash, "state_summary": summary,
                        "news_shards": news_chunks, "id_map_shards": map_chunks,
                        "datasets": datasets, "analysis_flags": dict(flags), "limitations": [
                            "All supplied historical representatives are analyzed; no 90-day article filter.",
                            "Relations use existing Korean rules; English relationship extraction is unsupported.",
                            "Historical affiliate facts are unavailable; current ownership never removes past COMPETE evidence.",
                            "Sentiment inputs retain explicit language/null and title plus first 200 body characters; no inference here.",
                            "Latest-window disclosures/prices are external auxiliary inputs, not historical news filters.",
                            "Global document uniqueness is supplied by the finalized SQLite identity state.",
                        ]}
            final = out / "analysis_manifest.json"
            if final.exists():
                if json.loads(final.read_text(encoding="utf-8")) != manifest:
                    raise ValueError("Existing complete analysis manifest differs")
            else:
                _install_json(final, manifest)
            _progress(out, status=manifest["status"], articles_processed=processed,
                      completed_news_shards=news_chunks, completed_id_map_shards=map_chunks,
                      dataset_rows=dict(totals))
            return manifest
        except Exception as error:
            _progress(out, status="FAILED", articles_processed=processed, completed_news_shards=news_chunks,
                      completed_id_map_shards=map_chunks, error_type=type(error).__name__, error=str(error))
            raise
        finally:
            reader.close()
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--normalized", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--companies", type=Path, required=True)
    parser.add_argument("--aliases", type=Path, required=True)
    parser.add_argument("--companies-sha256", required=True)
    parser.add_argument("--aliases-sha256", required=True)
    parser.add_argument("--state-fingerprint")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    parser.add_argument("--shard-size", type=int, default=10000)
    parser.add_argument("--id-map-shard-size", type=int, default=100000)
    args = parser.parse_args()
    from lake_backfill_state import BackfillState
    with BackfillState(args.state, read_only=True) as state:
        manifest = run_analysis(state, normalized_dir=args.normalized, out_dir=args.out,
                                aliases_path=args.aliases, companies_path=args.companies,
                                expected_aliases_sha256=args.aliases_sha256,
                                expected_companies_sha256=args.companies_sha256,
                                state_fingerprint=args.state_fingerprint, workers=args.workers,
                                shard_size=args.shard_size, id_map_shard_size=args.id_map_shard_size)
    print(json.dumps({"status": manifest["status"], "news_shards": manifest["news_shards"],
                      "dataset_rows": {row["name"]: row["row_count"] for row in manifest["datasets"]}}), flush=True)


if __name__ == "__main__":
    main()
