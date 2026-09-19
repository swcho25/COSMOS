"""Resumable local GPU worker for source-pinned Spark NDJSON candidate shards.

The manifest schema is cosmos-kg-candidate-export-1, with input-only files
[{path,sha256,rows,bytes}] and expected_unique_pairs. Paths are relative to its
directory. .txt/.json/.jsonl and gzip variants contain one JSON object per line.
Every part's bytes/hash is checked on every invocation before loading a model.

One SQLite FULL/WAL transaction commits a chunk's exact results, input digests,
file cursors and batch attempts together. A crash retries only uncommitted work.
Duplicate pairs are reusable only with identical canonical seven-field input;
conflicting duplicates stop the run. Results retain local-LLM provenance and are
not human gold or approved service data. No network, HDFS, training or DB loader.

--max-pairs limits newly committed unique pairs in this invocation, never claims
full completion. Resume pins source inventory, code, prompts, model and runtime.
File EOF/row counts and the final distinct-pair denominator must all reconcile
before streaming final result/selection exports and publishing a receipt.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

import knowledge_graph_batch_inference as batch
import knowledge_graph_news_preview as preview
import knowledge_graph_relevance_policy as policy
import local_news_relevance as base
import run_knowledge_graph_relevance as serial

HERE = Path(__file__).resolve().parent
MANIFEST_SCHEMA = "cosmos-kg-candidate-export-1"


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf8", newline="\n") as stream:
        stream.write(base.canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class WorkerLock:
    """OS-owned exclusive byte lock, released on normal exit or process death."""
    def __init__(self, path):
        self.path, self.stream = path, None

    def __enter__(self):
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if not self.stream.tell():
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise RuntimeError("Another worker owns this run lock") from exc
        return self

    def __exit__(self, *args):
        if self.stream is not None:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


def inventory(manifest_path):
    manifest_sha = base.sha(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf8"),
        object_pairs_hook=policy.unique_object, parse_constant=policy.reject_constant)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("Unexpected candidate manifest schema")
    marker_path = manifest_path.parent / "_VERIFIED"
    marker = json.loads(marker_path.read_text(encoding="utf8"))
    if manifest.get("complete") is not True or marker.get("manifest_sha256") != manifest_sha:
        raise ValueError("Candidate export is incomplete or lacks a matching verification marker")
    dataset_protocol_path = manifest_path.parent / "protocol.json"
    dataset_protocol_sha = base.sha(dataset_protocol_path)
    if manifest.get("protocol_sha256") != dataset_protocol_sha:
        raise ValueError("Dataset protocol raw-file SHA differs")
    dataset_protocol = json.loads(dataset_protocol_path.read_text(encoding="utf8"),
        object_pairs_hook=policy.unique_object, parse_constant=policy.reject_constant)
    model_receipt_sha = base.sha(HERE / "local_models/relevance_model_receipt.json")
    if dataset_protocol.get("model_receipt_sha256") != model_receipt_sha:
        raise ValueError("Dataset pinned model receipt differs from local model receipt")
    dataset_code = dataset_protocol.get("code_sha256", {})
    pinned_code = {name: base.sha(HERE / name) for name in
                   ("knowledge_graph_relevance_policy.py", "relevance_sentence_evidence.py")}
    if any(dataset_code.get(name) != digest for name, digest in pinned_code.items()):
        raise ValueError("Dataset pinned policy/evidence code differs from local code")
    expected = manifest.get("expected_unique_pairs")
    if type(expected) is not int or expected < 1:
        raise ValueError("Positive expected_unique_pairs is required")
    parts, seen = [], set()
    for item in manifest.get("files", []):
        if not isinstance(item, dict) or not {"path", "sha256", "rows", "bytes"} <= set(item):
            raise ValueError("Manifest part contract differs")
        relative = Path(item["path"])
        root = manifest_path.parent.resolve()
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root) or path in seen:
            raise ValueError("Part path escapes input directory or is duplicated")
        suffix = "".join(path.suffixes).lower()
        if not any(suffix.endswith(x) for x in (".txt", ".json", ".jsonl", ".txt.gz", ".json.gz", ".jsonl.gz")):
            raise ValueError("Expected NDJSON input part")
        if any(type(item[key]) is not int or item[key] < 0 for key in ("rows", "bytes")):
            raise ValueError("Invalid part row/byte counts")
        if path.stat().st_size != item["bytes"] or base.sha(path) != item["sha256"]:
            raise ValueError("Candidate part bytes/hash differ: " + item["path"])
        seen.add(path)
        parts.append({key: item[key] for key in ("path", "sha256", "rows", "bytes")})
    if not parts or expected > sum(x["rows"] for x in parts):
        raise ValueError("Unique-pair denominator exceeds input row count")
    if base.sha(manifest_path) != manifest_sha:
        raise ValueError("Manifest changed while checking inventory")
    return {"manifest_sha256": manifest_sha, "verification_marker_sha256": base.sha(marker_path),
            "dataset_protocol_sha256": dataset_protocol_sha, "dataset_model_receipt_sha256": model_receipt_sha,
            "dataset_pinned_code_sha256": pinned_code,
            "files": parts, "expected_unique_pairs": expected}


def frozen_protocol(input_inventory, chunk_size, batch_size, max_batch_tokens):
    list(batch.schedule([], batch_size, max_batch_tokens))
    if type(chunk_size) is not int or not 1 <= chunk_size <= 256:
        raise ValueError("Chunk size must be between 1 and 256")
    receipt_path = HERE / "local_models/relevance_model_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf8"))
    return {"schema": "cosmos-kg-reselection-worker-1", "inventory": input_inventory,
        "chunk_size": chunk_size, "batch_size": batch_size, "max_batch_tokens": max_batch_tokens,
        "max_input_tokens": batch.MAX_INPUT_TOKENS, "max_new_tokens": batch.MAX_NEW_TOKENS,
        "policy_version": policy.VERSION, "prompt_sha256": batch.prompt_sha256(),
        "model_repo": receipt["repo"], "model_revision": receipt["revision"],
        "model_receipt_sha256": base.sha(receipt_path),
        "code_hashes": {**batch.code_hashes(), Path(__file__).name: base.sha(Path(__file__))},
        "reuse_rule": "identical article_id,ticker,canonical seven-field digest and frozen protocol",
        "weight_training_performed": False, "human_gold": False, "service_published": False}


def connect(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0,
            rows_consumed INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS pairs (article_id TEXT NOT NULL, ticker TEXT NOT NULL,
            input_sha256 TEXT NOT NULL, result_json TEXT NOT NULL, decision TEXT NOT NULL,
            chunk_id INTEGER NOT NULL, PRIMARY KEY(article_id,ticker));
        CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, pairs INTEGER NOT NULL,
            inference_wall_seconds REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS batch_events (chunk_id INTEGER NOT NULL, event_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL, PRIMARY KEY(chunk_id,event_id));
    """)
    return db


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def meta(db, key):
    value = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if value is None else value[0]


def save_meta(db, key, value):
    db.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))


def status(db, protocol, state, started, *, error=None):
    progress = json.loads(meta(db, "progress") or '{}')
    counts = progress.get("decisions", {})
    processed = sum(counts.values())
    inference = progress.get("inference_wall_seconds", 0.)
    rate = processed / inference if inference else None
    remaining = max(0, protocol["inventory"]["expected_unique_pairs"] - processed)
    return {"state": state, "complete": state == "complete", "processed_unique_pairs": processed,
        "expected_unique_pairs": protocol["inventory"]["expected_unique_pairs"],
        "consumed_source_rows": progress.get("consumed_source_rows", 0),
        "expected_source_rows": sum(x["rows"] for x in protocol["inventory"]["files"]),
        "completed_files": progress.get("completed_files", 0),
        "expected_files": len(protocol["inventory"]["files"]), "decisions": counts,
        "review_pairs": counts.get("review", 0), "committed_inference_wall_seconds": inference,
        "pairs_per_inference_second": rate, "estimated_remaining_inference_seconds": remaining / rate if rate else None,
        "eta_scope": "committed inference throughput only; excludes hashing/loading/export and may change with article length",
        "invocation_wall_seconds": time.perf_counter() - started,
        "updated_at": datetime.now(timezone.utc).isoformat(), "error": error,
        "human_gold": False, "service_published": False}


def export_final(db, out):
    files = {}
    for name, selection_only in (("results.jsonl", False), ("selections.jsonl", True)):
        target, temporary = out / name, out / (name + ".tmp")
        with temporary.open("w", encoding="utf8", newline="\n") as stream:
            for (payload,) in db.execute("SELECT result_json FROM pairs ORDER BY article_id,ticker"):
                stream.write((base.canonical(json.loads(payload)["selection"]) if selection_only else payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        files[name] = base.sha(target)
    return files


def run_worker(manifest_path, out, *, resume=False, max_pairs=None, chunk_size=32,
               batch_size=2, max_batch_tokens=8192, classifier_factory=batch.LocalBatchClassifier):
    started = time.perf_counter()
    manifest_path, out = Path(manifest_path).resolve(), Path(out).resolve()
    if max_pairs is not None and (type(max_pairs) is not int or max_pairs < 1):
        raise ValueError("max_pairs is a positive new-pair limit per invocation")
    source = inventory(manifest_path)
    protocol = frozen_protocol(source, chunk_size, batch_size, max_batch_tokens)
    if out.exists() and not resume:
        raise ValueError("Run exists; use --resume with identical frozen inputs/settings")
    if not out.exists() and resume:
        raise ValueError("Cannot resume a nonexistent run")
    out.mkdir(parents=True, exist_ok=resume)
    with WorkerLock(out / "worker.lock"):
        protocol_path = out / "protocol.json"
        if protocol_path.exists():
            if json.loads(protocol_path.read_text(encoding="utf8")) != protocol:
                raise ValueError("Frozen source/code/model/protocol differs; create a new run")
            for name, digest in protocol["code_hashes"].items():
                if base.sha(out / "sources" / name) != digest:
                    raise ValueError("Frozen code snapshot differs")
        else:
            if resume:
                raise ValueError("Cannot resume without the original frozen protocol")
            (out / "sources").mkdir(exist_ok=True)
            for name, digest in protocol["code_hashes"].items():
                shutil.copyfile(HERE / name, out / "sources" / name)
                if base.sha(out / "sources" / name) != digest:
                    raise ValueError("Code changed while taking snapshot")
            atomic_json(protocol_path, protocol)
        protocol_sha = base.sha(protocol_path)
        db = connect(out / "state.sqlite3")
        try:
            previous_sha = meta(db, "protocol_sha256")
            if previous_sha not in (None, protocol_sha):
                raise ValueError("Database protocol digest differs")
            with transaction(db):
                save_meta(db, "protocol_sha256", protocol_sha)
                if meta(db, "progress") is None:
                    if db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]:
                        raise ValueError("Existing results lack durable progress counters")
                    save_meta(db, "progress", base.canonical({"decisions": {}, "inference_wall_seconds": 0.,
                        "consumed_source_rows": 0, "completed_files": 0}))
                for part in source["files"]:
                    db.execute("INSERT OR IGNORE INTO files(path) VALUES (?)", (part["path"],))
            if resume:
                progress = json.loads(meta(db, "progress"))
                if sum(progress["decisions"].values()) != db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]:
                    raise ValueError("Durable progress counters differ from committed results")
            current = status(db, protocol, "pending", started)
            atomic_json(out / "status.json", current)
            classifier = None
            pending, pending_hashes, cursors = [], {}, {}
            newly_committed = 0
            provenance = {"type": "local_llm_inference", "model_repo": protocol["model_repo"],
                "model_revision": protocol["model_revision"], "protocol_sha256": protocol_sha}

            def flush():
                nonlocal classifier, newly_committed
                results, inference_seconds, events = [], 0., []
                chunk_id = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM chunks").fetchone()[0]
                if pending:
                    atomic_json(out / "status.json", status(db, protocol, "in_progress", started))
                    if classifier is None:
                        classifier = classifier_factory(batch_size=batch_size, max_batch_tokens=max_batch_tokens)
                        runtime = classifier.protocol
                        if runtime["code_hashes"] != batch.code_hashes() or any(runtime.get(key) != protocol[key]
                                for key in ("model_repo", "model_revision", "model_receipt_sha256", "prompt_sha256")):
                            raise ValueError("Loaded runtime differs from frozen protocol")
                        runtime_json = base.canonical(runtime)
                        if meta(db, "runtime_protocol") not in (None, runtime_json):
                            raise ValueError("Runtime/GPU/library versions differ on resume")
                        save_meta(db, "runtime_protocol", runtime_json)
                        atomic_json(out / "runtime.json", runtime)
                    infer_started = time.perf_counter()
                    results = classifier.classify(pending, provenance=provenance)
                    inference_seconds = time.perf_counter() - infer_started
                    if len(results) != len(pending):
                        raise ValueError("Classifier dropped input pairs")
                    for row, result in zip(pending, results):
                        serial.validate_result(result, row, classifier.protocol)
                        if result["selection"]["model_provenance"] != provenance:
                            raise ValueError("Classifier provenance differs")
                        result["worker_chunk_id"] = chunk_id
                    events = classifier.batch_events
                with transaction(db):
                    progress = json.loads(meta(db, "progress"))
                    if pending:
                        db.execute("INSERT INTO chunks VALUES (?,?,?)", (chunk_id, len(pending), inference_seconds))
                    for row, result in zip(pending, results):
                        db.execute("INSERT INTO pairs VALUES (?,?,?,?,?,?)", (*preview.pair_key(row),
                            preview.canonical_input_sha256(row), base.canonical(result),
                            result["selection"]["decision"], chunk_id))
                        decision = result["selection"]["decision"]
                        progress["decisions"][decision] = progress["decisions"].get(decision, 0) + 1
                    for number, event in enumerate(events):
                        db.execute("INSERT INTO batch_events VALUES (?,?,?)", (chunk_id, number,
                            base.canonical({**event, "worker_chunk_id": chunk_id})))
                    for name, cursor in cursors.items():
                        old_rows, old_complete = db.execute(
                            "SELECT rows_consumed,complete FROM files WHERE path=?", (name,)).fetchone()
                        progress["consumed_source_rows"] += cursor[1] - old_rows
                        progress["completed_files"] += cursor[2] - old_complete
                        db.execute("UPDATE files SET offset=?,rows_consumed=?,complete=? WHERE path=?", (*cursor, name))
                    progress["inference_wall_seconds"] += inference_seconds
                    save_meta(db, "progress", base.canonical(progress))
                newly_committed += len(pending)
                pending.clear()
                pending_hashes.clear()
                cursors.clear()
                if classifier is not None:
                    classifier.batch_events.clear()  # Never retain millions of events in RAM.
                update = status(db, protocol, "in_progress", started)
                atomic_json(out / "status.json", update)
                print(base.canonical(update), flush=True)

            stopped = False
            for part in source["files"]:
                offset, consumed, complete = db.execute(
                    "SELECT offset,rows_consumed,complete FROM files WHERE path=?", (part["path"],)).fetchone()
                if complete:
                    continue
                path = manifest_path.parent / part["path"]
                opener = gzip.open if path.suffix.lower() == ".gz" else open
                with opener(path, "rb") as stream:
                    stream.seek(offset)
                    while True:
                        line = stream.readline()
                        if not line:
                            if consumed != part["rows"]:
                                raise ValueError("Part row count differs: " + part["path"])
                            cursors[part["path"]] = (stream.tell(), consumed, 1)
                            flush()
                            break
                        if not line.strip():
                            cursors[part["path"]] = (stream.tell(), consumed, 0)
                            continue
                        original = json.loads(line.decode("utf8"), object_pairs_hook=policy.unique_object,
                                              parse_constant=policy.reject_constant)
                        row = preview.validate_input(original)
                        if original.get("company_context") is not None:
                            raise ValueError("Non-null company context is unsupported")
                        consumed += 1
                        if consumed > part["rows"]:
                            raise ValueError("Part contains more rows than manifest")
                        digest, key = preview.canonical_input_sha256(row), preview.pair_key(row)
                        saved = db.execute("SELECT input_sha256 FROM pairs WHERE article_id=? AND ticker=?", key).fetchone()
                        prior = saved[0] if saved else pending_hashes.get(key)
                        if prior is not None and prior != digest:
                            raise ValueError("Conflicting article/company input: " + repr(key))
                        if prior is None:
                            pending.append(row)
                            pending_hashes[key] = digest
                        cursors[part["path"]] = (stream.tell(), consumed, 0)
                        limit_reached = max_pairs is not None and newly_committed + len(pending) >= max_pairs
                        if len(pending) >= chunk_size or limit_reached:
                            flush()
                        if limit_reached:
                            stopped = True
                            break
                if stopped:
                    break
            all_files = db.execute("SELECT COUNT(*) FROM files WHERE complete=0").fetchone()[0] == 0
            if all_files:
                processed = db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
                if processed != source["expected_unique_pairs"]:
                    raise ValueError("Final unique-pair denominator differs from manifest")
                # Hashes were checked before reading; recheck before completion.
                if inventory(manifest_path) != source:
                    raise ValueError("Source changed while processing")
                files = export_final(db, out)
                final = status(db, protocol, "complete", started)
                atomic_json(out / "completion_receipt.json", {**final, "files": files, "protocol_sha256": protocol_sha})
            else:
                final = status(db, protocol, "paused_limit", started)
            atomic_json(out / "status.json", final)
            return final
        except KeyboardInterrupt:
            atomic_json(out / "status.json", status(db, protocol, "interrupted", started))
            raise
        except BaseException as exc:
            atomic_json(out / "status.json", status(db, protocol, "failed", started, error=str(exc)))
            raise
        finally:
            db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-pairs", type=int)
    parser.add_argument("--chunk-size", type=int, default=32)
    parser.add_argument("--batch-size", type=int, choices=(1, 2), default=2)
    parser.add_argument("--max-batch-tokens", type=int, default=8192)
    args = parser.parse_args()
    answer = run_worker(args.manifest, args.out, resume=args.resume, max_pairs=args.max_pairs,
        chunk_size=args.chunk_size, batch_size=args.batch_size, max_batch_tokens=args.max_batch_tokens)
    print(base.canonical(answer), flush=True)
