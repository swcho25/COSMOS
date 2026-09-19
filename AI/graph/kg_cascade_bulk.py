"""Durable full-source STUDENT routing; ambiguous inputs remain pending.

Only a release bound to a passed, label-free validation report can run. No Qwen
or teacher is instantiated here. The output is a complete fast-pass audit and a
separate pending-review queue, not grounded graph edges or fully adjudicated
labels. The pending manifest deliberately has a different schema from a teacher
worker's input; starting expensive adjudication requires a separate bounded plan.

Full source/policy/model/code hashes are pinned. One SQLite FULL/WAL transaction
commits a bounded prediction chunk and source cursors together. Resume reuses
only identical source identities/digests/settings. The predictor is loaded once.
Output publication streams bounded shards; no whole-corpus list is materialized.
Preflight and observed-throughput budgets pause costly runs, never lower gates.
Model setup is paid once per predictor load; only inference is extrapolated per
pair. Validation timing comes from the same SHA-bound aggregate as the gates.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import importlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

import kg_cascade_routing as routing
import knowledge_graph_news_preview as preview
import knowledge_graph_reselection_worker as durable
import transfer_kg_reselection as transfer

HERE = Path(__file__).resolve().parent
SCHEMA = "cosmos-kg-cascade-bulk-1"
RELEASE_SCHEMA = "cosmos-kg-cascade-release-1"
PENDING_SCHEMA = "cosmos-kg-pending-review-1"
APPROVED_MODULES = {"kg_cascade_student", "kg_cascade_student_v2"}
DECISIONS = ("include", "exclude", "needs_review")


def read(path):
    return transfer.strict_json(Path(path).read_text(encoding="utf8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _seconds(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def atomic_json(path, value):
    """Retry a short Windows sharing violation; never truncate the live receipt."""
    for attempt in range(6):
        try:
            return durable.atomic_json(path, value)
        except PermissionError:
            if os.name != "nt" or attempt == 5:
                raise
            time.sleep(.02 * (attempt + 1))


def release_contract(manifest, checkpoint, policy_path, receipt_path, student_module):
    """Inspect hashes/gates only; never consume calibration/test row labels."""
    require(student_module in APPROVED_MODULES, "Unsupported student module")
    source = durable.inventory(manifest)
    original = read(manifest)
    require(original.get("scope") != "parent_shard_subset", "Full pass requires a full source manifest")
    checkpoint, receipt_path = Path(checkpoint).resolve(), Path(receipt_path).resolve()
    model_sha = preview.file_sha256(checkpoint / "training_manifest.json")
    policy, receipt = read(policy_path), read(receipt_path)
    routing._validate_policy(policy)
    require(any(rule["enabled"] for rule in policy["directions"].values()), "No validated automatic direction")
    expected = {"schema": RELEASE_SCHEMA, "complete": True, "full_fast_pass_allowed": True,
                "source_manifest_sha256": source["manifest_sha256"], "student_module": student_module,
                "student_manifest_sha256": model_sha,
                "routing_policy_file_sha256": preview.file_sha256(policy_path),
                "routing_policy_sha256": policy["policy_sha256"]}
    require(all(type(receipt.get(key)) is type(value) and receipt.get(key) == value for key, value in expected.items()),
            "Release is not bound to this verified source/student/policy")
    report_ref = receipt.get("validation_report", {})
    relative = Path(report_ref.get("path", ""))
    report_path = (receipt_path.parent / relative).resolve()
    require(not relative.is_absolute() and report_path.is_relative_to(receipt_path.parent)
            and report_path != receipt_path and report_path.is_file(), "Unsafe validation report path")
    require(preview.file_sha256(report_path) == report_ref.get("sha256"), "Validation report hash differs")
    report = read(report_path)
    require("rows" not in report and "labels" not in report, "Use a label-free aggregate validation report")
    gates = report.get("pilot_gates", {})
    require(all(gates.get(key) is True for key in ("all_pass", "decision_parity_pass", "coverage_pass", "measured_speed_pass")),
            "Validation gates did not all pass; full routing remains disabled")
    require(report.get("student_manifest_sha256") == model_sha and report.get("policy_sha256") == policy["policy_sha256"],
            "Validation report does not bind this student and policy")
    n = report.get("total_pairs")
    timing = report.get("student_bulk_timing", {})
    require(isinstance(timing, dict), "Student bulk timing must be an object")
    seconds, setup = timing.get("inference_seconds"), timing.get("setup_seconds")
    require(type(n) is int and n > 0 and type(timing.get("pairs")) is int and timing["pairs"] == n
            and _seconds(seconds) and seconds > 0 and _seconds(setup),
            "Measured student setup/inference timing and matching validation denominator required")
    return source, policy, {**expected, "receipt_sha256": preview.file_sha256(receipt_path),
                           "validation_report_sha256": report_ref["sha256"],
                           "validation_student_inference_seconds_per_pair": seconds / n,
                           "validation_student_setup_seconds": setup,
                           "expected_unique_articles": original.get("counts", {}).get("mention_unique_articles")}


def code_hashes(module):
    return {**module.code_hashes(), **{name: preview.file_sha256(HERE / name) for name in
        ("kg_cascade_bulk.py", "kg_cascade_routing.py", "knowledge_graph_reselection_worker.py",
         "transfer_kg_reselection.py")}}


def interpret(row, prediction, policy, student_sha):
    require(isinstance(prediction, dict), "Student prediction must be an object")
    for key in ("article_id", "ticker"):
        require(prediction.get(key) == row[key], "Student returned a different identity")
    require(prediction.get("input_sha256") == preview.canonical_input_sha256(row), "Student source digest differs")
    require(prediction.get("student_manifest_sha256") == student_sha, "Student checkpoint binding differs")
    reasons = prediction.get("fallback_reasons")
    require(isinstance(reasons, list) and all(isinstance(x, str) for x in reasons), "Invalid fallback reasons")
    errors = list(reasons)
    coverage = prediction.get("coverage", {})
    if prediction.get("requires_fallback") is not False or coverage.get("full_coverage") is not True or coverage.get("encoding_completed") is not True:
        errors.append("student_not_fully_encoded")
    if coverage.get("truncated") is not False:
        errors.append("student_truncation_unverified")
    decision = routing.route(prediction.get("probabilities"), policy, errors=errors, ood=False)
    # Invalid numeric scores route to review, and must remain serializable in the
    # audit instead of aborting JSON publication or being silently rewritten valid.
    def safe(value):
        if isinstance(value, float) and not math.isfinite(value):
            return {"invalid_nonfinite_number": str(value)}
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [safe(item) for item in value]
        return value
    return {"decision": "needs_review" if decision["needs_teacher"] else decision["decision"],
            "route": decision, "prediction": safe(prediction),
            "evidence_status": "not_extracted", "graph_materialization_ready": False,
            "human_gold": False, "production_approved": False}


def connect(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,offset INTEGER NOT NULL DEFAULT 0,
            rows_consumed INTEGER NOT NULL DEFAULT 0,complete INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS pairs(article_id TEXT NOT NULL,ticker TEXT NOT NULL,input_sha256 TEXT NOT NULL,
            source_path TEXT NOT NULL,source_offset INTEGER NOT NULL,line_bytes INTEGER NOT NULL,
            decision TEXT NOT NULL,result_json TEXT NOT NULL,PRIMARY KEY(article_id,ticker));
    """)
    return db


def status(db, protocol, state):
    progress = json.loads(durable.meta(db, "progress"))
    counts = progress["decisions"]
    done = sum(counts.values())
    seconds, setup = progress["student_inference_seconds"], progress["student_setup_seconds"]
    remaining = max(0, protocol["inventory"]["expected_unique_pairs"] - done)
    return {"schema": SCHEMA, "state": state, "fast_pass_complete": state == "fast_pass_complete",
            "fully_adjudicated": False, "processed_unique_pairs": done,
            "expected_unique_pairs": protocol["inventory"]["expected_unique_pairs"],
            "decisions": counts, "pending_review_pairs": counts["needs_review"],
            "consumed_source_rows": progress["consumed_source_rows"], "completed_files": progress["completed_files"],
            "student_wall_seconds": seconds + setup,
            "student_inference_seconds": seconds, "student_setup_seconds": setup,
            "projected_total_student_seconds": setup + seconds / done * (done + remaining) if done and seconds else None,
            "eta_is_guaranteed": False, "teacher_calls": 0, "graph_materialization_ready": False,
            "human_gold": False, "production_approved": False, "updated_at": datetime.now(timezone.utc).isoformat()}


class Shards:
    def __init__(self, root, prefix, size):
        self.root, self.prefix, self.size = root, prefix, size
        self.handle, self.path, self.rows, self.files = None, None, 0, []

    def add(self, value):
        if self.handle is None:
            self.path = self.root / self.prefix / f"part-{len(self.files):06d}.jsonl"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open("x", encoding="utf8", newline="\n")
            self.rows = 0
        self.handle.write(preview.canonical(value) + "\n")
        self.rows += 1
        if self.rows == self.size:
            self.close()

    def close(self):
        if self.handle is not None:
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.close()
            self.files.append({"path": self.path.relative_to(self.root).as_posix(), "rows": self.rows,
                               "bytes": self.path.stat().st_size, "sha256": preview.file_sha256(self.path)})
            self.handle = None


def export_outputs(db, manifest, out, protocol, shard_size):
    """Retain incomplete attempts, publish an exhaustive receipt only after checks."""
    root = out / "exports" / ("attempt_" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    all_rows, pending = Shards(root, "decisions", shard_size), Shards(root, "pending/inputs", shard_size)
    evidence = Shards(root, "include_candidates_without_evidence", shard_size)
    handles = {}
    try:
        for article, ticker, sha, file, offset, length, decision, payload in db.execute(
                "SELECT article_id,ticker,input_sha256,source_path,source_offset,line_bytes,decision,result_json FROM pairs ORDER BY source_path,source_offset"):
            value = {"article_id": article, "ticker": ticker, "input_sha256": sha, **json.loads(payload),
                     "source_ref": {"path": file, "byte_offset": offset, "line_bytes": length}}
            all_rows.add(value)
            if decision == "include":
                evidence.add({key: value[key] for key in ("article_id", "ticker", "input_sha256", "source_ref", "evidence_status")})
            if decision != "needs_review":
                continue
            if file not in handles:
                for handle in handles.values():
                    handle.close()
                handles.clear()
                source_path = manifest.parent / file
                handles[file] = (gzip.open if source_path.suffix == ".gz" else open)(source_path, "rb")
            stream = handles[file]
            stream.seek(offset)
            raw = stream.read(length)
            row = preview.validate_input(transfer.strict_json(raw.decode("utf8")))
            require(preview.pair_key(row) == (article, ticker) and preview.canonical_input_sha256(row) == sha,
                    "Pending source bytes/identity changed")
            pending.add(row)
    finally:
        for handle in handles.values():
            handle.close()
        for writer in (all_rows, pending, evidence):
            writer.close()
    counts = dict(db.execute("SELECT decision,COUNT(*) FROM pairs GROUP BY decision"))
    require(sum(x["rows"] for x in all_rows.files) == protocol["inventory"]["expected_unique_pairs"], "Export coverage differs")
    require(sum(x["rows"] for x in pending.files) == counts.get("needs_review", 0), "Pending denominator differs")
    pending_manifest = {"schema": PENDING_SCHEMA, "complete": True, "completion_scope": "pending_input_queue_only",
        "files": [{**item, "path": item["path"].removeprefix("pending/")} for item in pending.files],
        "expected_unique_pairs": counts.get("needs_review", 0),
        "parent_source_manifest_sha256": protocol["inventory"]["manifest_sha256"],
        "bulk_protocol_sha256": preview.file_sha256(out / "protocol.json"),
        "execution_requires_explicit_bounded_review_plan": True, "teacher_inference_started": False,
        "fully_adjudicated": False, "graph_materialization_ready": False}
    (root / "pending").mkdir(exist_ok=True)
    atomic_json(root / "pending/manifest.json", pending_manifest)
    atomic_json(root / "pending/_VERIFIED", {"manifest_sha256": preview.file_sha256(root / "pending/manifest.json")})
    return {"root": root.relative_to(out).as_posix(), "decisions": all_rows.files,
            "pending_manifest_sha256": preview.file_sha256(root / "pending/manifest.json"),
            "pending_manifest": (root / "pending/manifest.json").relative_to(out).as_posix(),
            "include_without_evidence": evidence.files}


def run_bulk(manifest, checkpoint, policy_path, receipt_path, out, *, student_module="kg_cascade_student_v2",
             device="cuda", resume=False, max_pairs=None, chunk_size=32, shard_size=1000,
             max_fast_pass_hours=24., predictor_factory=None):
    require(type(chunk_size) is int and 1 <= chunk_size <= 128, "Invalid chunk size")
    require(type(shard_size) is int and 1 <= shard_size <= 10000, "Invalid shard size")
    require(max_pairs is None or type(max_pairs) is int and max_pairs > 0, "Positive max_pairs required")
    require(_seconds(max_fast_pass_hours) and max_fast_pass_hours > 0, "Positive fast-pass time budget required")
    manifest, out = Path(manifest).resolve(), Path(out).resolve()
    source, policy, release = release_contract(manifest, checkpoint, policy_path, receipt_path, student_module)
    module = importlib.import_module(student_module)
    protocol = {"schema": SCHEMA, "inventory": source, "release": release,
        "student_module": student_module, "code_hashes": code_hashes(module), "device": device,
        "chunk_size": chunk_size, "shard_size": shard_size, "max_fast_pass_hours": max_fast_pass_hours,
        "fallback_execution": "defer_only_no_teacher", "require_grounded_selection": False,
        "distribution_scope": "frozen_source_only_no_unmeasured_ood_guarantee", "human_gold": False,
        "production_approved": False, "graph_materialization_ready": False}
    require(not out.is_relative_to(manifest.parent), "Output must not modify frozen source directory")
    require(not out.exists() or resume, "Existing output requires --resume")
    require(out.exists() or not resume, "Cannot resume nonexistent output")
    out.mkdir(parents=True, exist_ok=resume)
    with durable.WorkerLock(out / "bulk.lock"):
        if (out / "protocol.json").exists():
            require(read(out / "protocol.json") == protocol, "Frozen bulk protocol differs")
        else:
            require(not resume, "Cannot resume without frozen bulk protocol")
            atomic_json(out / "protocol.json", protocol)
        estimate = release["validation_student_setup_seconds"] + source["expected_unique_pairs"] * release["validation_student_inference_seconds_per_pair"]
        preflight = {"schema": "cosmos-kg-fast-pass-preflight-1", "estimated_fast_pass_seconds": estimate,
                     "estimate_scope": "one model setup plus validation inference seconds per pair; not promised completion time",
                     "validation_student_setup_seconds": release["validation_student_setup_seconds"],
                     "validation_student_inference_seconds_per_pair": release["validation_student_inference_seconds_per_pair"],
                     "max_fast_pass_hours": max_fast_pass_hours, "teacher_calls_planned": 0,
                     "within_budget": estimate <= max_fast_pass_hours * 3600}
        atomic_json(out / "preflight.json", preflight)
        if not preflight["within_budget"]:
            value = {"state": "paused_preflight_budget", "fast_pass_complete": False, "fully_adjudicated": False, **preflight}
            atomic_json(out / "status.json", value)
            return value
        db = connect(out / "state.sqlite3")
        predictor, newly_committed, stopped = None, 0, False
        try:
            with durable.transaction(db):
                if durable.meta(db, "protocol_sha256") is None:
                    require(db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0] == 0, "Results lack protocol metadata")
                    durable.save_meta(db, "protocol_sha256", preview.file_sha256(out / "protocol.json"))
                    durable.save_meta(db, "progress", preview.canonical({"decisions": dict.fromkeys(DECISIONS, 0),
                        "student_setup_seconds": 0., "student_inference_seconds": 0.,
                        "consumed_source_rows": 0, "completed_files": 0}))
                require(durable.meta(db, "protocol_sha256") == preview.file_sha256(out / "protocol.json"), "Database protocol differs")
                for part in source["files"]:
                    db.execute("INSERT OR IGNORE INTO files(path) VALUES (?)", (part["path"],))
            progress = json.loads(durable.meta(db, "progress"))
            require(sum(progress["decisions"].values()) == db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0], "Durable counters differ")
            existing = status(db, protocol, "paused_observed_budget")
            if existing["processed_unique_pairs"] >= 64 and existing["projected_total_student_seconds"] > max_fast_pass_hours * 3600:
                atomic_json(out / "status.json", existing)
                return existing
            pending, refs, cursors, pending_keys = [], [], {}, {}

            def flush():
                nonlocal predictor, newly_committed, stopped
                if not pending and not cursors:
                    return
                setup_seconds, seconds, outputs = 0., 0., []
                if pending:
                    if predictor is None:
                        setup_start = time.perf_counter()
                        predictor = (predictor_factory or module.StudentPredictor)(checkpoint, device=device)
                        require(predictor.manifest_sha256 == release["student_manifest_sha256"], "Loaded student manifest differs")
                        setup_seconds = time.perf_counter() - setup_start
                    start = time.perf_counter()
                    predictions = predictor.predict(pending)
                    require(len(predictions) == len(pending), "Student omitted input pairs")
                    outputs = [interpret(row, prediction, policy, release["student_manifest_sha256"])
                               for row, prediction in zip(pending, predictions)]
                    seconds = time.perf_counter() - start
                require(_seconds(seconds) and _seconds(setup_seconds), "Invalid measured runtime timing")
                with durable.transaction(db):
                    value = json.loads(durable.meta(db, "progress"))
                    for row, ref, result in zip(pending, refs, outputs):
                        db.execute("INSERT INTO pairs VALUES (?,?,?,?,?,?,?,?)", (*preview.pair_key(row),
                            preview.canonical_input_sha256(row), *ref, result["decision"], preview.canonical(result)))
                        value["decisions"][result["decision"]] += 1
                    for path, cursor in cursors.items():
                        old = db.execute("SELECT rows_consumed,complete FROM files WHERE path=?", (path,)).fetchone()
                        value["consumed_source_rows"] += cursor[1] - old[0]
                        value["completed_files"] += cursor[2] - old[1]
                        db.execute("UPDATE files SET offset=?,rows_consumed=?,complete=? WHERE path=?", (*cursor, path))
                    value["student_inference_seconds"] += seconds
                    value["student_setup_seconds"] += setup_seconds
                    durable.save_meta(db, "progress", preview.canonical(value))
                newly_committed += len(pending)
                pending.clear(); refs.clear(); cursors.clear(); pending_keys.clear()
                current = status(db, protocol, "in_progress")
                if current["processed_unique_pairs"] >= 64 and current["projected_total_student_seconds"] > max_fast_pass_hours * 3600:
                    stopped = True
                    current["state"] = "paused_observed_budget"
                atomic_json(out / "status.json", current)

            for part in source["files"]:
                offset, consumed, complete = db.execute("SELECT offset,rows_consumed,complete FROM files WHERE path=?", (part["path"],)).fetchone()
                if complete:
                    continue
                path = manifest.parent / part["path"]
                with (gzip.open if path.suffix == ".gz" else open)(path, "rb") as stream:
                    stream.seek(offset)
                    while not stopped:
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
                        require(original.get("company_context") is None, "Unsupported external context")
                        key, input_sha = preview.pair_key(row), preview.canonical_input_sha256(row)
                        saved = db.execute("SELECT input_sha256 FROM pairs WHERE article_id=? AND ticker=?", key).fetchone()
                        previous = saved[0] if saved else pending_keys.get(key)
                        require(previous in (None, input_sha), "Conflicting duplicate article/company pair")
                        if previous is None:
                            pending.append(row); refs.append((part["path"], start, len(raw))); pending_keys[key] = input_sha
                        consumed += 1
                        require(consumed <= part["rows"], "Input part contains extra rows")
                        cursors[part["path"]] = (stream.tell(), consumed, 0)
                        limit = max_pairs is not None and newly_committed + len(pending) >= max_pairs
                        if len(pending) >= chunk_size or limit:
                            flush()
                        if limit:
                            stopped = True
                if stopped:
                    break
            finished = db.execute("SELECT COUNT(*) FROM files WHERE complete=0").fetchone()[0] == 0
            if finished:
                if predictor is not None:
                    predictor.close()  # Validate frozen checkpoint before publishing completion.
                    predictor = None
                count, articles = db.execute("SELECT COUNT(*),COUNT(DISTINCT article_id) FROM pairs").fetchone()
                require(count == source["expected_unique_pairs"], "Final unique-pair coverage differs")
                expected_articles = release["expected_unique_articles"]
                require(expected_articles is None or articles == expected_articles, "Final unique-article coverage differs")
                require(code_hashes(module) == protocol["code_hashes"], "Code changed")
                exported = export_outputs(db, manifest, out, protocol, shard_size)
                final_source, _, final_release = release_contract(manifest, checkpoint, policy_path, receipt_path, student_module)
                require(final_source == source and final_release == release, "Source/release changed during processing or export")
                final = {**status(db, protocol, "fast_pass_complete"), "observed_unique_articles": articles,
                         "completion_scope": "student_fast_pass_only", "outputs": exported,
                         "protocol_sha256": preview.file_sha256(out / "protocol.json")}
                atomic_json(out / "fast_pass_receipt.json", final)
            else:
                prior = read(out / "status.json") if (out / "status.json").exists() else {}
                state = "paused_observed_budget" if prior.get("state") == "paused_observed_budget" else "paused_limit"
                final = status(db, protocol, state)
            atomic_json(out / "status.json", final)
            return final
        except BaseException as error:
            if durable.meta(db, "progress") is not None:
                atomic_json(out / "status.json", {**status(db, protocol, "failed"), "error": str(error)})
            raise
        finally:
            try:
                if predictor is not None:
                    predictor.close()
            finally:
                db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "checkpoint", "policy", "validation-receipt", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--student-module", choices=sorted(APPROVED_MODULES), default="kg_cascade_student_v2")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-pairs", type=int)
    parser.add_argument("--chunk-size", type=int, default=32)
    parser.add_argument("--shard-size", type=int, default=1000)
    parser.add_argument("--max-fast-pass-hours", type=float, default=24.)
    args = parser.parse_args()
    answer = run_bulk(args.manifest, args.checkpoint, args.policy, args.validation_receipt, args.out,
        student_module=args.student_module, device=args.device, resume=args.resume, max_pairs=args.max_pairs,
        chunk_size=args.chunk_size, shard_size=args.shard_size, max_fast_pass_hours=args.max_fast_pass_hours)
    print(preview.canonical(answer), flush=True)
