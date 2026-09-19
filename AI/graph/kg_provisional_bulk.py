"""Resumable prototype classification with the existing, quality-gate-failed B.

This runner is deliberately separate from validated cascade release/routing.
It never reads a release as permission, promotes a failed gate, calls an LLM,
trains weights, publishes a graph, or deletes source data. Include/exclude are
PROVISIONAL candidates from three-score argmax, not validated automatic labels.
Ties, review winners, invalid scores and unsupported/failed encodings are queued
as needs_review. Model/source identity errors stop rather than becoming labels.

Source inventory, complete B checkpoint, code, device and processing settings
are frozen on first invocation. SQLite FULL/WAL commits results and source
cursors in one transaction. A restart reuses only committed, checksum-verified
rows under the identical frozen protocol; an interrupted chunk is recomputed.
Gzip offsets refer to the uncompressed byte stream. All raw predictions and
canonical original-input SHA256 values are retained with exact source refs.

There is no automatic time budget. --max-pairs limits newly committed unique
pairs in one invocation for bounded runs/resume checks; omit it for the source.
--stop-file requests a pause after committing already read pending rows. Final
exports are streaming, separate provisional include/exclude/review shards.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import importlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
import uuid

import kg_cascade_bulk as shared
import kg_cascade_routing as routing
import knowledge_graph_news_preview as preview
import knowledge_graph_reselection_worker as durable
import transfer_kg_reselection as transfer

HERE = Path(__file__).resolve().parent
SCHEMA = "cosmos-kg-provisional-bulk-1"
STUDENT_MODULE = "kg_cascade_student_v2b"
MODEL_VERSION = "kg-cascade-student-v2-B-pairfix-1"
DECISIONS = ("include", "exclude", "needs_review")
FLAGS = {"classification_scope": "provisional_candidate_only",
         "validated_auto_selection": False, "production_approved": False,
         "graph_ready": False, "graph_materialization_ready": False,
         "human_gold": False, "fully_adjudicated": False}
atomic_json, read, require = shared.atomic_json, shared.read, shared.require


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(preview.canonical(value).encode("utf8")).hexdigest()


def inventory(manifest):
    return durable.inventory(Path(manifest))


def code_hashes(module=None):
    module = module or importlib.import_module(STUDENT_MODULE)
    return {**module.code_hashes(), **{name: preview.file_sha256(HERE / name) for name in (
        "kg_provisional_bulk.py", "kg_cascade_bulk.py", "kg_cascade_routing.py",
        "knowledge_graph_news_preview.py", "knowledge_graph_reselection_worker.py", "transfer_kg_reselection.py")}}


def model_contract(checkpoint):
    """Verify the actual completed B manifest and bytes without loading a GPU."""
    module = importlib.import_module(STUDENT_MODULE)
    checkpoint = Path(checkpoint).resolve()
    path = checkpoint / "training_manifest.json"
    initial_sha = preview.file_sha256(path)
    manifest = read(path)
    require(manifest.get("schema") == "kg-cascade-student-training-2"
            and manifest.get("complete") is True, "Incomplete or unknown B checkpoint")
    config = module.configuration(manifest["config"])
    require(config["candidate"] == "B" and module.model_version(config) == MODEL_VERSION
            and manifest.get("model_version") == MODEL_VERSION, "Only the completed pair-repair B model is supported")
    require(manifest.get("label_policy") == "kg_semantic_teacher_v1", "B teacher label policy differs")
    require(manifest.get("feature_protocol") == module.FEATURE_PROTOCOL, "B feature protocol differs")
    require(manifest.get("code_hashes") == module.code_hashes(), "B training code changed")
    require(module._core._artifact_hashes(checkpoint / "checkpoint") == manifest.get("checkpoint_files"),
            "B checkpoint bytes changed")
    require(preview.file_sha256(path) == initial_sha, "B manifest changed during verification")
    return {"checkpoint": str(checkpoint), "student_manifest_sha256": initial_sha,
            "model_version": MODEL_VERSION, "label_policy": manifest["label_policy"],
            "checkpoint_files": manifest["checkpoint_files"], "config": config,
            "validation_gate_status": "not_passed_not_overridden_by_this_prototype"}


def _safe(value):
    """Preserve invalid floating-point values explicitly, never normalize them."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_nonfinite_number": str(value)}
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def interpret(row, prediction, student_sha):
    require(isinstance(prediction, dict), "Student prediction must be an object")
    require(preview.pair_key(prediction) == preview.pair_key(row), "Student returned a different identity")
    require(prediction.get("input_sha256") == preview.canonical_input_sha256(row), "Student source digest differs")
    require(prediction.get("student_manifest_sha256") == student_sha, "Student checkpoint binding differs")
    require(prediction.get("model_version") == MODEL_VERSION, "Student model version differs")
    require(prediction.get("label_policy") == "kg_semantic_teacher_v1", "Student label policy differs")
    reasons = prediction.get("fallback_reasons")
    require(isinstance(reasons, list) and all(isinstance(item, str) for item in reasons), "Invalid fallback reasons")
    reasons = list(reasons)
    coverage = prediction.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {}
    if (prediction.get("requires_fallback") is not False or coverage.get("full_coverage") is not True
            or coverage.get("encoding_completed") is not True):
        reasons.append("student_not_fully_encoded")
    if coverage.get("truncated") is not False:
        reasons.append("student_truncation_unverified")
    tokens = coverage.get("total_content_tokens")
    if (type(tokens) is not int or tokens <= 0 or type(coverage.get("covered_content_tokens")) is not int
            or coverage.get("covered_content_tokens") != tokens):
        reasons.append("student_token_coverage_unverified")
    scores = prediction.get("probabilities")
    problem = routing._score_error(scores)
    if problem:
        reasons.append(problem)
    winner = None if problem else routing._winning_label(scores)
    if not problem and winner is None:
        reasons.append("score_tie")
    if winner == "review":
        reasons.append("student_review")
    decision = winner if not reasons and winner in ("include", "exclude") else "needs_review"
    return {"decision": decision, "provisional_decision": True, **FLAGS,
            "decision_rule": "three_class_argmax_no_confidence_cutoff",
            "review_reasons": list(dict.fromkeys(reasons)), "raw_prediction": _safe(prediction),
            "evidence_status": "not_extracted", "teacher_called": False}


def connect(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,offset INTEGER NOT NULL DEFAULT 0,
            rows_consumed INTEGER NOT NULL DEFAULT 0,complete INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY,pairs INTEGER NOT NULL,
            inference_seconds REAL NOT NULL,setup_seconds REAL NOT NULL,results_sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pairs(article_id TEXT NOT NULL,ticker TEXT NOT NULL,input_sha256 TEXT NOT NULL,
            source_path TEXT NOT NULL,source_offset INTEGER NOT NULL,line_bytes INTEGER NOT NULL,
            decision TEXT NOT NULL,result_json TEXT NOT NULL,result_sha256 TEXT NOT NULL,chunk_id INTEGER NOT NULL,
            PRIMARY KEY(article_id,ticker));
        CREATE INDEX IF NOT EXISTS pairs_chunk ON pairs(chunk_id);
    """)
    return db


def _state_digest(db):
    return digest({"progress": read_progress(db),
                   "files": list(db.execute("SELECT path,offset,rows_consumed,complete FROM files ORDER BY path"))})


def read_progress(db):
    value = durable.meta(db, "progress")
    return {} if value is None else transfer.strict_json(value)


def validate_state(db, protocol):
    """Stream checksums on restart; do not silently repair durable corruption."""
    require(db.execute("PRAGMA quick_check").fetchall() == [("ok",)], "SQLite integrity check failed")
    require(durable.meta(db, "state_sha256") == _state_digest(db), "Durable counters/cursors checksum differs")
    progress = read_progress(db)
    counts = dict.fromkeys(DECISIONS, 0)
    counts.update(dict(db.execute("SELECT decision,COUNT(*) FROM pairs GROUP BY decision")))
    require(counts == progress["decisions"], "Durable decision counters differ")
    parts = {part["path"]: part for part in protocol["inventory"]["files"]}
    files = {path: (offset, consumed, complete) for path, offset, consumed, complete in
             db.execute("SELECT path,offset,rows_consumed,complete FROM files")}
    require(set(files) == set(parts), "Durable source file inventory differs")
    for path, (offset, consumed, complete) in files.items():
        require(offset >= 0 and 0 <= consumed <= parts[path]["rows"] and complete in (0, 1), "Invalid durable source cursor")
        require(not complete or consumed == parts[path]["rows"], "Completed source row count differs")
    require(sum(value[1] for value in files.values()) == progress["consumed_source_rows"], "Source counters differ")
    require(sum(value[2] for value in files.values()) == progress["completed_files"], "Completed-file counters differ")
    require(db.execute("SELECT COALESCE(SUM(pairs),0) FROM chunks").fetchone()[0] == sum(counts.values()),
            "Durable chunk counters differ")
    checked = 0
    for chunk_id, expected, expected_sha in db.execute("SELECT id,pairs,results_sha256 FROM chunks ORDER BY id"):
        members = []
        for article, ticker, input_sha, source_path, offset, length, decision, payload, result_sha in db.execute(
                "SELECT article_id,ticker,input_sha256,source_path,source_offset,line_bytes,decision,result_json,result_sha256 "
                "FROM pairs WHERE chunk_id=? ORDER BY article_id,ticker", (chunk_id,)):
            require(hashlib.sha256(payload.encode("utf8")).hexdigest() == result_sha, "Durable result checksum differs")
            value = transfer.strict_json(payload)
            require((value["article_id"], value["ticker"], value["input_sha256"], value["decision"])
                    == (article, ticker, input_sha, decision), "Durable result identity differs")
            require(all(value.get(key) == item and type(value.get(key)) is type(item) for key, item in FLAGS.items()),
                    "Durable result lost provisional flags")
            require(source_path in files and offset >= 0 and length > 0 and offset + length <= files[source_path][0],
                    "Durable source reference extends beyond committed cursor")
            require(value["source_ref"] == source_ref(protocol, source_path, offset, length), "Durable source reference differs")
            members.append([article, ticker, input_sha, result_sha])
            checked += 1
        require(len(members) == expected and digest(members) == expected_sha, "Durable chunk result checksum differs")
    require(checked == sum(counts.values()), "Orphaned durable result rows")


def source_ref(protocol, path, offset, length):
    return {"manifest_sha256": protocol["inventory"]["manifest_sha256"], "path": path,
            "part_sha256": protocol["source_part_sha256"][path], "byte_offset": offset, "line_bytes": length,
            "offset_encoding": "uncompressed_utf8_bytes"}


def status(db, protocol, state, *, error=None):
    progress = read_progress(db)
    counts = progress.get("decisions", dict.fromkeys(DECISIONS, 0))
    done = sum(counts.values())
    seconds = progress.get("student_inference_seconds", 0.)
    remaining = max(0, protocol["inventory"]["expected_unique_pairs"] - done)
    return {"schema": SCHEMA, "state": state, "complete": state == "provisional_complete", **FLAGS,
        "processed_unique_pairs": done, "expected_unique_pairs": protocol["inventory"]["expected_unique_pairs"],
        "decisions": counts, "consumed_source_rows": progress.get("consumed_source_rows", 0),
        "completed_files": progress.get("completed_files", 0), "expected_files": len(protocol["inventory"]["files"]),
        "student_inference_seconds": seconds, "student_setup_seconds": progress.get("student_setup_seconds", 0.),
        "measured_pairs_per_inference_second": done / seconds if seconds else None,
        "estimated_remaining_inference_seconds": remaining * seconds / done if done and seconds else None,
        "eta_scope": "committed inference only; excludes verification, loading, export; not guaranteed",
        "teacher_calls": 0, "updated_at": now(), "error": error}


def export_outputs(db, out, protocol, shard_size):
    root = out / "exports" / ("attempt_" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    writers = {decision: shared.Shards(root, decision, shard_size) for decision in DECISIONS}
    try:
        for decision, payload, expected_sha in db.execute("SELECT decision,result_json,result_sha256 FROM pairs ORDER BY rowid"):
            require(hashlib.sha256(payload.encode("utf8")).hexdigest() == expected_sha, "Export result checksum differs")
            writers[decision].add(transfer.strict_json(payload))
    finally:
        for writer in writers.values():
            writer.close()
    counts = read_progress(db)["decisions"]
    for decision, writer in writers.items():
        require(sum(item["rows"] for item in writer.files) == counts[decision], "Export decision coverage differs")
    require(sum(counts.values()) == protocol["inventory"]["expected_unique_pairs"], "Export total coverage differs")
    value = {"schema": "cosmos-provisional-candidate-export-1", "complete": True, **FLAGS,
             "protocol_sha256": digest(protocol), "decisions": counts,
             "files": {decision: writer.files for decision, writer in writers.items()}}
    atomic_json(root / "manifest.json", value)
    return {"root": root.relative_to(out).as_posix(), "manifest_sha256": preview.file_sha256(root / "manifest.json"),
            "manifest": (root / "manifest.json").relative_to(out).as_posix(), "files": value["files"]}


def verify_export(out, outputs, counts, protocol):
    root = (out / outputs["root"]).resolve()
    require(root.is_relative_to(out / "exports"), "Unsafe export root")
    manifest_path = (out / outputs["manifest"]).resolve()
    require(manifest_path == root / "manifest.json" and preview.file_sha256(manifest_path) == outputs["manifest_sha256"],
            "Published export manifest changed")
    manifest = read(manifest_path)
    require(manifest.get("schema") == "cosmos-provisional-candidate-export-1" and manifest.get("complete") is True
            and manifest.get("protocol_sha256") == digest(protocol), "Export protocol/schema differs")
    require(all(manifest.get(key) == value and type(manifest.get(key)) is type(value) for key, value in FLAGS.items()),
            "Export lost provisional flags")
    require(manifest["files"] == outputs["files"] and manifest["decisions"] == counts, "Export manifest differs")
    require(set(outputs["files"]) == set(DECISIONS), "Export partitions differ")
    for decision, items in outputs["files"].items():
        require(sum(item["rows"] for item in items) == counts[decision], "Export partition count differs")
        for item in items:
            path = (root / item["path"]).resolve()
            require(path.is_relative_to(root) and path.stat().st_size == item["bytes"]
                    and preview.file_sha256(path) == item["sha256"], "Published export bytes changed")


def run_bulk(manifest, checkpoint, out, *, device="cuda", resume=False, max_pairs=None,
             chunk_size=32, shard_size=1000, predictor_factory=None, stop_file=None):
    require(type(chunk_size) is int and 1 <= chunk_size <= 128, "Invalid chunk size")
    require(type(shard_size) is int and 1 <= shard_size <= 10000, "Invalid shard size")
    require(max_pairs is None or type(max_pairs) is int and max_pairs > 0, "Positive max_pairs required")
    require(device in ("cpu", "cuda"), "Unsupported device")
    manifest, checkpoint, out = Path(manifest).resolve(), Path(checkpoint).resolve(), Path(out).resolve()
    stop_file = Path(stop_file).resolve() if stop_file is not None else None
    require(not out.is_relative_to(manifest.parent) and not manifest.parent.is_relative_to(out), "Output/source directories must be separate")
    require(not out.is_relative_to(checkpoint) and not checkpoint.is_relative_to(out), "Output/checkpoint directories must be separate")
    empty_existing = out.is_dir() and next(out.iterdir(), None) is None
    require(not out.exists() or resume or empty_existing, "Existing nonempty output requires --resume; if no protocol was frozen, use a fresh output directory")
    require(out.exists() or not resume, "Cannot resume nonexistent output")
    out.mkdir(parents=True, exist_ok=resume or empty_existing)
    invocation = uuid.uuid4().hex
    with durable.WorkerLock(out / "provisional.lock"):
        db, predictor, protocol = None, None, None
        try:
            # A prior live-looking status can be stale after a power loss. The OS
            # lock above, not a PID/age guess, determines sole run ownership.
            atomic_json(out / "invocation.json", {"id": invocation, "started_at": now(), "resume": resume,
                "manifest": str(manifest), "checkpoint": str(checkpoint), "max_pairs": max_pairs,
                "stop_file": str(stop_file) if stop_file else None, "state": "verifying_sources", **FLAGS})
            source, model = inventory(manifest), model_contract(checkpoint)
            source_manifest = read(manifest)
            expected_articles = source_manifest.get("counts", {}).get("mention_unique_articles")
            if source_manifest.get("scope") == "parent_shard_subset":
                expected_articles = None
            require(expected_articles is None or type(expected_articles) is int and expected_articles > 0, "Invalid article denominator")
            protocol = {"schema": SCHEMA, **FLAGS, "inventory": source, "manifest_path": str(manifest),
                "source_part_sha256": {part["path"]: part["sha256"] for part in source["files"]},
                "model": model, "student_module": STUDENT_MODULE, "code_hashes": code_hashes(),
                "device": device, "chunk_size": chunk_size, "shard_size": shard_size,
                "expected_unique_articles": expected_articles,
                "policy": "provisional_candidate_only", "decision_rule": "three_class_argmax_no_confidence_cutoff",
                "teacher_execution": "never", "automatic_time_budget": None,
                "labels_seen": False, "predictions_used_for_tuning": False,
                "input_scope": "unlabeled_frozen_source_may_include_former_evaluation_articles",
                "original_holdout_labels_read": False,
                "release_validation_bypassed": False, "validated_release_not_used": True}
            protocol_path = out / "protocol.json"
            if protocol_path.exists():
                require(read(protocol_path) == protocol, "Frozen provisional protocol differs")
            else:
                require(not resume, "Cannot resume without frozen provisional protocol")
                atomic_json(protocol_path, protocol)
            protocol_sha = preview.file_sha256(protocol_path)
            db = connect(out / "state.sqlite3")
            with durable.transaction(db):
                if durable.meta(db, "protocol_sha256") is None:
                    require(db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0] == 0, "Results lack protocol metadata")
                    require(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0, "Chunks lack protocol metadata")
                    durable.save_meta(db, "protocol_sha256", protocol_sha)
                    durable.save_meta(db, "progress", preview.canonical({"decisions": dict.fromkeys(DECISIONS, 0),
                        "student_setup_seconds": 0., "student_inference_seconds": 0.,
                        "consumed_source_rows": 0, "completed_files": 0}))
                    for part in source["files"]:
                        db.execute("INSERT INTO files(path) VALUES (?)", (part["path"],))
                    durable.save_meta(db, "state_sha256", _state_digest(db))
                require(durable.meta(db, "protocol_sha256") == protocol_sha, "Database protocol differs")
            validate_state(db, protocol)
            atomic_json(out / "status.json", status(db, protocol, "running"))
            pending, refs, cursors, pending_keys = [], [], {}, {}
            newly_committed, staged_rows, stop_reason = 0, 0, None

            def flush():
                nonlocal predictor, newly_committed, staged_rows
                if not pending and not cursors:
                    return
                setup_seconds, seconds, outputs = 0., 0., []
                if pending:
                    if predictor is None:
                        started = time.perf_counter()
                        factory = predictor_factory or importlib.import_module(STUDENT_MODULE).StudentPredictor
                        predictor = factory(checkpoint, device=device)
                        require(predictor.manifest_sha256 == model["student_manifest_sha256"], "Loaded B manifest differs")
                        setup_seconds = time.perf_counter() - started
                    started = time.perf_counter()
                    predictions = predictor.predict(pending)
                    require(len(predictions) == len(pending), "Student omitted input pairs")
                    outputs = [interpret(row, pred, model["student_manifest_sha256"]) for row, pred in zip(pending, predictions)]
                    seconds = time.perf_counter() - started
                require(all(type(value) in (int, float) and math.isfinite(value) and value >= 0
                            for value in (seconds, setup_seconds)), "Invalid measured runtime")
                with durable.transaction(db):
                    progress = read_progress(db)
                    chunk_id = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM chunks").fetchone()[0]
                    members = []
                    for row, ref, result in zip(pending, refs, outputs):
                        article, ticker = preview.pair_key(row)
                        input_sha = preview.canonical_input_sha256(row)
                        value = {"article_id": article, "ticker": ticker, "input_sha256": input_sha,
                                 "source_ref": source_ref(protocol, *ref), **result}
                        payload = preview.canonical(value)
                        result_sha = hashlib.sha256(payload.encode("utf8")).hexdigest()
                        db.execute("INSERT INTO pairs VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (article, ticker, input_sha, *ref, result["decision"], payload, result_sha, chunk_id))
                        members.append([article, ticker, input_sha, result_sha])
                        progress["decisions"][result["decision"]] += 1
                    for path, cursor in cursors.items():
                        old = db.execute("SELECT rows_consumed,complete FROM files WHERE path=?", (path,)).fetchone()
                        progress["consumed_source_rows"] += cursor[1] - old[0]
                        progress["completed_files"] += cursor[2] - old[1]
                        db.execute("UPDATE files SET offset=?,rows_consumed=?,complete=? WHERE path=?", (*cursor, path))
                    progress["student_inference_seconds"] += seconds
                    progress["student_setup_seconds"] += setup_seconds
                    db.execute("INSERT INTO chunks VALUES (?,?,?,?,?)", (chunk_id, len(members), seconds, setup_seconds, digest(sorted(members))))
                    durable.save_meta(db, "progress", preview.canonical(progress))
                    durable.save_meta(db, "state_sha256", _state_digest(db))
                newly_committed += len(pending)
                pending.clear(); refs.clear(); cursors.clear(); pending_keys.clear()
                staged_rows = 0
                atomic_json(out / "status.json", status(db, protocol, "running"))

            for part in source["files"]:
                offset, consumed, complete = db.execute("SELECT offset,rows_consumed,complete FROM files WHERE path=?", (part["path"],)).fetchone()
                if complete:
                    continue
                path = manifest.parent / part["path"]
                with (gzip.open if path.suffix == ".gz" else open)(path, "rb") as stream:
                    stream.seek(offset)
                    while True:
                        if stop_file is not None and stop_file.exists():
                            flush()
                            stop_reason = "paused_stop_file"
                            break
                        start = stream.tell()
                        raw = stream.readline()
                        if not raw:
                            require(consumed == part["rows"], "Input part row count differs")
                            cursors[part["path"]] = (stream.tell(), consumed, 1)
                            flush()
                            break
                        require(raw.strip(), "Blank input row is not silently discarded")
                        original = transfer.strict_json(raw.decode("utf8"))
                        row = preview.validate_input(original)
                        require(original.get("company_context") is None, "Unsupported external company context")
                        key, input_sha = preview.pair_key(row), preview.canonical_input_sha256(row)
                        saved = db.execute("SELECT input_sha256 FROM pairs WHERE article_id=? AND ticker=?", key).fetchone()
                        previous = saved[0] if saved else pending_keys.get(key)
                        require(previous in (None, input_sha), "Conflicting duplicate article/company pair")
                        if previous is None:
                            pending.append(row); refs.append((part["path"], start, len(raw))); pending_keys[key] = input_sha
                        consumed += 1
                        staged_rows += 1
                        require(consumed <= part["rows"], "Input part contains extra rows")
                        cursors[part["path"]] = (stream.tell(), consumed, 0)
                        at_limit = max_pairs is not None and newly_committed + len(pending) >= max_pairs
                        if len(pending) >= chunk_size or staged_rows >= chunk_size or at_limit:
                            flush()
                        if at_limit:
                            stop_reason = "paused_limit"
                            break
                if stop_reason:
                    break
            if predictor is not None:
                closing = predictor
                predictor = None
                closing.close()  # Student loader checks full checkpoint bytes again.
            require(code_hashes() == protocol["code_hashes"], "Code changed during provisional processing")
            require(model_contract(checkpoint) == model, "B checkpoint changed during provisional processing")
            finished = db.execute("SELECT COUNT(*) FROM files WHERE complete=0").fetchone()[0] == 0
            if finished:
                count, articles = db.execute("SELECT COUNT(*),COUNT(DISTINCT article_id) FROM pairs").fetchone()
                require(count == source["expected_unique_pairs"], "Final unique-pair coverage differs")
                require(expected_articles is None or articles == expected_articles, "Final unique-article coverage differs")
                validate_state(db, protocol)
                receipt_path = out / "provisional_receipt.json"
                if receipt_path.exists():
                    final = read(receipt_path)
                    require(final.get("complete") is True and final.get("protocol_sha256") == protocol_sha,
                            "Existing completion receipt differs")
                    require(all(final.get(key) == value and type(final.get(key)) is type(value)
                                for key, value in FLAGS.items()), "Completion receipt lost provisional flags")
                    require(final.get("schema") == SCHEMA and final.get("state") == "provisional_complete"
                            and final.get("processed_unique_pairs") == count
                            and final.get("expected_unique_pairs") == source["expected_unique_pairs"]
                            and final.get("observed_unique_articles") == articles
                            and final.get("decisions") == read_progress(db)["decisions"]
                            and type(final.get("teacher_calls")) is int and final["teacher_calls"] == 0,
                            "Completion receipt counters/scope differ")
                    recorded = durable.meta(db, "provisional_receipt_sha256")
                    require(recorded is None or recorded == preview.file_sha256(receipt_path), "Completion receipt checksum differs")
                    verify_export(out, final["outputs"], read_progress(db)["decisions"], protocol)
                    if recorded is None:  # Recover a crash between receipt fsync and meta commit.
                        with durable.transaction(db):
                            durable.save_meta(db, "provisional_receipt_sha256", preview.file_sha256(receipt_path))
                else:
                    outputs = export_outputs(db, out, protocol, shard_size)
                    require(inventory(manifest) == source, "Source changed during provisional processing or export")
                    final = {**status(db, protocol, "provisional_complete"), "protocol_sha256": protocol_sha,
                             "observed_unique_articles": articles, "outputs": outputs,
                             "completion_scope": "provisional_predictions_only_not_validated_selection"}
                    atomic_json(receipt_path, final)
                    with durable.transaction(db):
                        durable.save_meta(db, "provisional_receipt_sha256", preview.file_sha256(receipt_path))
            else:
                final = status(db, protocol, stop_reason or "paused")
            atomic_json(out / "status.json", final)
            atomic_json(out / ("invocation_" + invocation + ".json"),
                        {"id": invocation, "finished_at": now(), "newly_committed_pairs": newly_committed,
                         "protocol_sha256": protocol_sha, **final})
            return final
        except BaseException as error:
            error_info = {"type": type(error).__name__, "message": str(error), "at": now(), "invocation": invocation}
            state = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
            try:
                failure = status(db, protocol, state, error=error_info) if db is not None and protocol is not None else {
                    "schema": SCHEMA, "state": state, "complete": False, **FLAGS, "error": error_info}
                atomic_json(out / "status.json", failure)
                atomic_json(out / ("failure_" + invocation + ".json"), failure)
            except Exception:
                # Preserve the original error even when corrupt SQLite or a disk
                # failure prevents reading counters or writing its status file.
                try:
                    atomic_json(out / ("failure_" + invocation + ".json"),
                                {"schema": SCHEMA, "state": state, "complete": False, **FLAGS, "error": error_info})
                except Exception:
                    pass
            raise
        finally:
            active_error = sys.exc_info()[0] is not None
            try:
                if predictor is not None:
                    try:
                        predictor.close()
                    except Exception as cleanup_error:
                        try:
                            atomic_json(out / ("cleanup_failure_" + invocation + ".json"),
                                {"type": type(cleanup_error).__name__, "message": str(cleanup_error),
                                 "original_failure_preserved": active_error, "at": now(), **FLAGS})
                        except Exception:
                            pass
                        if not active_error:
                            raise
            finally:
                if db is not None:
                    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "checkpoint", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-pairs", type=int)
    parser.add_argument("--chunk-size", type=int, default=32)
    parser.add_argument("--shard-size", type=int, default=1000)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args()
    print(preview.canonical(run_bulk(args.manifest, args.checkpoint, args.out, device=args.device,
        resume=args.resume, max_pairs=args.max_pairs, chunk_size=args.chunk_size,
        shard_size=args.shard_size, stop_file=args.stop_file)), flush=True)
