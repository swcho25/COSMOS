"""Publish a verified partial B dataset without pretending the population is done.

Inputs are the immutable split base, an explicitly paused pc1, and a completed
pc2. No inference, model loading, labels, source modification or worker resume
occurs. The pc1 OS lock is held while its read-only, checkpointed SQLite state
is verified; the STOP marker is preserved. The full merger remains unchanged.

Only the exact committed prefixes attested by SQLite source cursors are merged.
Every row's original parent text, input SHA, decision replay, result/chunk SHA,
pair uniqueness and source-position uniqueness must agree. Remaining parent
suffixes are recorded as explicit byte/row ranges, never invented as labels.

The new SQLite pairs table retains all seven source fields for downstream
distribution and sample audits without another original-source scan. Exports
contain provisional include/exclude/needs_review rows and an include_with_source
dataset. artifact_complete describes this bounded snapshot; complete and
full_population_complete remain false. Failed attempts have no artifact receipt
and must be retried in a new directory. Output/metadata writes are local only.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import hashlib
from pathlib import Path
import sqlite3

import kg_provisional_bulk as bulk
import merge_kg_provisional_partitions as full

HERE = Path(__file__).resolve().parent
SCHEMA = "cosmos-kg-provisional-partial-merge-1"
read, require, sha = full.read, full.require, full.sha
preview, durable = bulk.preview, bulk.durable
PARTIAL_FLAGS = {**bulk.FLAGS, "complete": False, "full_population_complete": False,
                 "selection_quality_validated": False}


def code_hashes():
    return {**bulk.code_hashes(), "merge_kg_provisional_partitions.py": sha(Path(full.__file__)),
            "merge_kg_provisional_partial.py": sha(Path(__file__))}


def db_rows(db):
    for payload, expected in db.execute("SELECT result_json,result_sha256 FROM pairs ORDER BY source_path,source_offset"):
        require(hashlib.sha256(payload.encode("utf8")).hexdigest() == expected, "Committed result checksum differs")
        yield bulk.transfer.strict_json(payload), expected


def committed_cursors(db, protocol):
    """Verify a sequential committed input prefix, not arbitrary missing holes."""
    cursors = {path: (offset, rows, complete) for path, offset, rows, complete in
               db.execute("SELECT path,offset,rows_consumed,complete FROM files")}
    observed = {path: (count, distinct) for path, count, distinct in db.execute(
        "SELECT source_path,COUNT(*),COUNT(DISTINCT source_offset) FROM pairs GROUP BY source_path")}
    pending = False
    for part in protocol["inventory"]["files"]:
        offset, consumed, complete = cursors[part["path"]]
        require(observed.get(part["path"], (0, 0)) == (consumed, consumed), "Committed source rows differ from unique results")
        require(not pending or consumed == 0, "Committed worker input has non-prefix holes")
        if consumed < part["rows"]:
            pending = True
        require(consumed > 0 or offset == 0, "Unprocessed file has a nonzero byte cursor")
    return cursors


def verify_paused(out, protocol, db):
    value = read(out / "status.json")
    full.check_flags(value)
    require(value.get("schema") == bulk.SCHEMA and value.get("state") in ("paused_stop_file", "paused_limit")
            and value.get("complete") is False and type(value.get("teacher_calls")) is int and value["teacher_calls"] == 0,
            "pc1 must be explicitly paused, not running, failed or complete")
    require(not (out / "provisional_receipt.json").exists()
            and durable.meta(db, "provisional_receipt_sha256") is None, "Paused worker has a contradictory completed receipt")
    require(durable.meta(db, "protocol_sha256") == sha(out / "protocol.json"), "Paused SQLite protocol differs")
    bulk.validate_state(db, protocol)
    computed = bulk.status(db, protocol, value["state"])
    for key in ("processed_unique_pairs", "expected_unique_pairs", "decisions", "consumed_source_rows",
                "completed_files", "expected_files", "student_inference_seconds", "student_setup_seconds"):
        require(value.get(key) == computed[key], "Paused status and SQLite counters differ: " + key)
    require(0 <= computed["processed_unique_pairs"] < computed["expected_unique_pairs"], "pc1 must have an incomplete prefix")
    return computed["processed_unique_pairs"], committed_cursors(db, protocol)


def merge_partial(plan_path, worker_outputs, out, *, parent_manifest=None, shard_size=1000,
                  expected_covered=None, expected_unprocessed=None, expected_include=None):
    require(set(worker_outputs) == {"pc1", "pc2"}, "pc1 paused output and pc2 complete output are required")
    require(type(shard_size) is int and 1 <= shard_size <= 10000, "Invalid shard size")
    for value in (expected_covered, expected_unprocessed, expected_include):
        require(value is None or type(value) is int and value >= 0, "Expected counts must be nonnegative integers")
    plan_path, out = Path(plan_path).resolve(), Path(out).resolve()
    plan = full.check_plan(plan_path)
    root, plan_sha = plan_path.parent, sha(plan_path)
    parent_manifest = Path(parent_manifest or plan["parent"]["manifest_path"]).resolve()
    workers = {name: Path(path).resolve() for name, path in worker_outputs.items()}
    protected = [root, parent_manifest.parent, *workers.values()]
    require(not out.exists() and all(not out.is_relative_to(p) and not p.is_relative_to(out) for p in protected),
            "Use a fresh partial output outside all source/split/worker directories")
    out.mkdir(parents=True)
    counts, processed, components = dict.fromkeys(bulk.DECISIONS, 0), 0, {}
    coverage, target, reader = None, None, None
    before_code = code_hashes()
    def status(stage, **extra):
        bulk.atomic_json(out / "status.json", {"schema": SCHEMA, **PARTIAL_FLAGS, "artifact_complete": False,
            "stage": stage, "processed_unique_pairs": processed, "decisions": dict(counts),
            "parent_expected_unique_pairs": plan["parent"]["expected_unique_pairs"],
            "teacher_calls": 0, "updated_at": bulk.now(), **extra})
    with ExitStack() as stack:
        try:
            status("verifying_parent_source")
            require(sha(parent_manifest) == plan["parent"]["manifest_sha256"], "Parent manifest changed")
            parent = bulk.inventory(parent_manifest)
            require(parent["expected_unique_pairs"] == plan["parent"]["expected_unique_pairs"], "Parent denominator differs")
            parent_parts = {part["path"]: part for part in parent["files"]}
            base = plan["base"]
            for prefix in ("db", "protocol", "status", "model_binding"):
                require(sha(full.bound_path(root, base[prefix + "_path"])) == base[prefix + "_sha256"], "Base snapshot binding differs")
            base_protocol = read(full.bound_path(root, base["protocol_path"]))
            require(read(full.bound_path(root, base["model_binding_path"])) == base_protocol["model"], "Base model binding differs")
            require(base_protocol["code_hashes"] == bulk.code_hashes(), "Original frozen provisional code differs")
            full.validate_protocol(base_protocol, base_protocol, parent)
            status("verifying_base_snapshot")
            base_db = stack.enter_context(closing(full.readonly_db(full.bound_path(root, base["db_path"]))))
            require(durable.meta(base_db, "protocol_sha256") == base["protocol_sha256"], "Base SQLite protocol differs")
            bulk.validate_state(base_db, base_protocol)
            base_count = base_db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
            require(base_count == base["committed_unique_pairs"], "Base committed count differs")
            base_cursors = committed_cursors(base_db, base_protocol)
            components["base"] = {"protocol_sha256": base["protocol_sha256"], "db_sha256": base["db_sha256"],
                                  "committed_pairs": base_count, "state": "immutable_split_base"}
            source_cursors = {"base": base_cursors}
            jobs = [("base", base_protocol, base_db, None)]
            maps, mapped_parent = {}, {}
            for partition in plan["partitions"]:
                name, worker = partition["id"], workers[partition["id"]]
                lock = worker / "provisional.lock"
                # An existing nonempty file is essential: WorkerLock must not
                # create or initialize anything in the immutable input folder.
                if name == "pc1" or lock.exists():
                    require(lock.is_file() and lock.stat().st_size >= 1, "Existing worker OS lock is required")
                    stack.enter_context(durable.WorkerLock(lock))
                status("verifying_" + name)
                manifest_path = full.bound_path(root, partition["input_manifest"])
                source = bulk.inventory(manifest_path)
                child = read(manifest_path)
                require(source["manifest_sha256"] == partition["manifest_sha256"]
                        and child.get("scope") == "parent_shard_subset"
                        and child.get("parent_manifest_sha256") == parent["manifest_sha256"], "Worker partition source differs")
                protocol = read(worker / "protocol.json")
                full.validate_protocol(protocol, base_protocol, source)
                db = stack.enter_context(closing(full.readonly_db(worker / "state.sqlite3")))
                if name == "pc1":
                    committed, cursors = verify_paused(worker, protocol, db)
                else:
                    # Full worker_rows performs receipt/export verification when
                    # consumed; validate durable state now to derive safe ranges.
                    require(durable.meta(db, "protocol_sha256") == sha(worker / "protocol.json"), "pc2 SQLite protocol differs")
                    bulk.validate_state(db, protocol)
                    committed = db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
                    require(committed == partition["expected_unique_pairs"]
                            and db.execute("SELECT COUNT(*) FROM files WHERE complete=0").fetchone()[0] == 0,
                            "pc2 must be complete")
                    cursors = committed_cursors(db, protocol)
                source_cursors[name] = cursors
                components[name] = {"protocol_sha256": sha(worker / "protocol.json"), "db_sha256": sha(worker / "state.sqlite3"),
                    "status_sha256": sha(worker / "status.json"), "committed_pairs": committed,
                    "partition_expected_pairs": partition["expected_unique_pairs"],
                    "state": read(worker / "status.json")["state"],
                    "stop_sha256": sha(worker / "STOP") if (worker / "STOP").is_file() else None,
                    "receipt_sha256": sha(worker / "provisional_receipt.json") if name == "pc2" else None}
                maps[name] = {item["child_path"]: item for item in partition["mappings"]}
                for item in partition["mappings"]:
                    path = item["parent_path"]
                    require(path in parent_parts and path not in mapped_parent, "Duplicate or unknown mapped parent part")
                    offset, consumed, _ = base_cursors[path]
                    require((item["parent_start_byte"], item["parent_start_row"]) == (offset, consumed)
                            and item["rows"] == parent_parts[path]["rows"] - consumed
                            and item["parent_sha256"] == parent_parts[path]["sha256"], "Mapping differs from immutable base cursor")
                    mapped_parent[path] = (name, item)
                jobs.append((name, protocol, db, partition))
            require(set(mapped_parent) == {path for path, (_, rows, _) in base_cursors.items() if rows < parent_parts[path]["rows"]},
                    "Split mappings omit remaining parent parts")
            wanted = sum(component["committed_pairs"] for component in components.values())
            missing = parent["expected_unique_pairs"] - wanted
            require(0 < missing < parent["expected_unique_pairs"], "Partial merge requires some processed and some unprocessed pairs")
            require(expected_covered is None or expected_covered == wanted, "Committed coverage differs from expected_covered")
            require(expected_unprocessed is None or expected_unprocessed == missing, "Unprocessed count differs from expected_unprocessed")
            reader = full.ParentReader(parent_manifest)
            expected_ranges, missing_ranges = {}, []
            for path, part in parent_parts.items():
                end, rows, _ = base_cursors[path]
                component, child_path = "base", None
                if path in mapped_parent:
                    component, mapping = mapped_parent[path]
                    child_path = mapping["child_path"]
                    offset, consumed, _ = source_cursors[component][child_path]
                    end, rows = mapping["parent_start_byte"] + offset, mapping["parent_start_row"] + consumed
                require(0 <= rows <= part["rows"], "Committed parent row coverage is invalid")
                expected_ranges[path] = {"covered_rows": rows, "covered_end_byte": end}
                if rows < part["rows"]:
                    require(component == "pc1", "Only paused pc1 may account for missing parent rows")
                    stream = reader.seek(path, end)
                    if end:
                        stream.seek(end - 1)
                        require(stream.read(1) == b"\n", "Missing-range start is not an original line boundary")
                    require(stream.read(1), "Unprocessed rows cannot start at EOF")
                    total_bytes = stream.seek(0, 2)
                    missing_ranges.append({"parent_path": path, "parent_part_sha256": part["sha256"],
                        "row_start": rows, "row_end": part["rows"], "unprocessed_rows": part["rows"] - rows,
                        "byte_start": end, "byte_end": total_bytes, "offset_encoding": "uncompressed_utf8_bytes",
                        "component": component, "child_path": child_path,
                        "reason": "not_committed_in_paused_worker_not_a_model_decision"})
            require(sum(x["unprocessed_rows"] for x in missing_ranges) == missing, "Missing ranges differ from committed denominator")
            protocol = {"schema": SCHEMA, **PARTIAL_FLAGS, "split_plan_sha256": plan_sha,
                "parent_inventory": parent, "model": base_protocol["model"], "code_hashes": before_code,
                "components": components, "covered_unique_pairs": wanted, "unprocessed_unique_pairs": missing,
                "teacher_calls": 0, "labels_seen": False, "predictions_used_for_tuning": False,
                "input_scope": "committed_unlabeled_provisional_predictions_may_include_former_evaluation_articles"}
            bulk.atomic_json(out / "protocol.json", protocol)
            target = stack.enter_context(closing(sqlite3.connect(out / "state.sqlite3")))
            target.execute("PRAGMA journal_mode=WAL")
            target.execute("PRAGMA synchronous=FULL")
            target.executescript("""CREATE TABLE pairs(article_id TEXT,ticker TEXT,input_sha256 TEXT,
                source_path TEXT,source_offset INTEGER,line_bytes INTEGER,decision TEXT,result_json TEXT,result_sha256 TEXT,
                component TEXT,input_json TEXT,
                PRIMARY KEY(article_id,ticker),UNIQUE(source_path,source_offset));""")
            for name, component_protocol, db, partition in jobs:
                status("merging", component=name, covered_snapshot_pairs=wanted, unprocessed_pairs=missing)
                rows = full.worker_rows(workers[name], component_protocol, db, partition["expected_unique_pairs"]) if name == "pc2" else db_rows(db)
                component_count = 0
                for value, result_sha in rows:
                    require(bulk.digest(value) == result_sha, "Component result checksum differs")
                    original = value["source_ref"]
                    require(original == bulk.source_ref(component_protocol, original["path"], original["byte_offset"], original["line_bytes"]),
                            "Component source reference differs")
                    if name == "base":
                        ref = dict(original)
                    else:
                        mapping = maps[name][original["path"]]
                        require(mapping["child_path"].endswith(".gz") or original["byte_offset"] + original["line_bytes"] <= mapping["bytes"],
                                "Component source exceeds mapped part")
                        ref = bulk.source_ref(base_protocol, mapping["parent_path"], mapping["parent_start_byte"] + original["byte_offset"], original["line_bytes"])
                    require(ref["byte_offset"] + ref["line_bytes"] <= expected_ranges[ref["path"]]["covered_end_byte"],
                            "Result extends into uncommitted parent range")
                    row = reader.row(ref)
                    require(preview.pair_key(row) == preview.pair_key(value)
                            and preview.canonical_input_sha256(row) == value["input_sha256"], "Parent original input identity differs")
                    replay = bulk.interpret(row, full._safe_float_restore(value["raw_prediction"]), base_protocol["model"]["student_manifest_sha256"])
                    require(value == {"article_id": row["article_id"], "ticker": row["ticker"], "input_sha256": value["input_sha256"],
                                      "source_ref": original, **replay}, "Stored provisional interpretation differs")
                    merged = {**value, "source_ref": ref, "component_ref": {"component": name,
                        "protocol_sha256": components[name]["protocol_sha256"], "result_sha256": result_sha, "source_ref": original}}
                    try:
                        target.execute("INSERT INTO pairs VALUES (?,?,?,?,?,?,?,?,?,?,?)", (*preview.pair_key(row), value["input_sha256"],
                            ref["path"], ref["byte_offset"], ref["line_bytes"], value["decision"], preview.canonical(merged), bulk.digest(merged), name,
                            preview.canonical(row)))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("Duplicate pair or original parent source position") from exc
                    processed += 1
                    component_count += 1
                    counts[value["decision"]] += 1
                    if processed % 10000 == 0:
                        target.commit()
                        status("merging", component=name, covered_snapshot_pairs=wanted, unprocessed_pairs=missing)
                require(component_count == components[name]["committed_pairs"], "Component committed row coverage differs")
                target.commit()
            require(processed == wanted and (expected_include is None or counts["include"] == expected_include),
                    "Merged committed/include denominator differs")
            status("checking_exact_coverage")
            for path, expected in expected_ranges.items():
                cursor, rows = 0, 0
                for offset, length in target.execute("SELECT source_offset,line_bytes FROM pairs WHERE source_path=? ORDER BY source_offset", (path,)):
                    require(offset == cursor, "Missing or overlapping committed parent byte range")
                    cursor += length
                    rows += 1
                require(rows == expected["covered_rows"] and cursor == expected["covered_end_byte"], "Committed parent coverage differs from cursor")
                if rows == parent_parts[path]["rows"]:
                    require(reader.seek(path, cursor).read(1) == b"", "Fully covered parent part is not at EOF")
            observed_articles = target.execute("SELECT COUNT(DISTINCT article_id) FROM pairs").fetchone()[0]
            coverage = {"schema": "cosmos-provisional-partial-coverage-1", **PARTIAL_FLAGS,
                "artifact_complete": True, "parent_expected_unique_pairs": parent["expected_unique_pairs"],
                "covered_unique_pairs": wanted, "unprocessed_unique_pairs": missing,
                "coverage_fraction": wanted / parent["expected_unique_pairs"], "observed_unique_articles": observed_articles,
                "parent_expected_unique_articles": read(parent_manifest).get("counts", {}).get("mention_unique_articles"),
                "unprocessed_ranges": missing_ranges, "covered_ranges_by_parent_part": expected_ranges,
                "range_semantics": "zero_based_half_open_row_and_uncompressed_byte_intervals"}
            bulk.atomic_json(out / "coverage.json", coverage)
            status("building_query_indexes")
            target.execute("CREATE INDEX pairs_decision ON pairs(decision)")
            target.execute("CREATE INDEX pairs_ticker_decision ON pairs(ticker,decision)")
            target.commit()
            status("exporting")
            export = out / "exports"
            export.mkdir()
            writers = {decision: bulk.shared.Shards(export, decision, shard_size) for decision in bulk.DECISIONS}
            accepted = bulk.shared.Shards(export, "include_with_source", shard_size)
            exported = 0
            try:
                for input_sha, decision, payload, expected_sha, input_json in target.execute(
                        "SELECT input_sha256,decision,result_json,result_sha256,input_json FROM pairs ORDER BY source_path,source_offset"):
                    require(hashlib.sha256(payload.encode("utf8")).hexdigest() == expected_sha, "Merged export checksum differs")
                    value = bulk.transfer.strict_json(payload)
                    writers[decision].add(value)
                    if decision == "include":
                        source = bulk.transfer.strict_json(input_json)
                        require(preview.canonical_input_sha256(source) == input_sha, "Stored original source fields changed")
                        accepted.add({**source, "input_sha256": input_sha, "provisional_selection": value,
                                      **PARTIAL_FLAGS, "inference_or_training_ready": False})
                    exported += 1
                    if exported % 10000 == 0:
                        status("exporting", exported_pairs=exported, covered_snapshot_pairs=wanted, unprocessed_pairs=missing)
            finally:
                for writer in [*writers.values(), accepted]: writer.close()
            require(exported == wanted and all(sum(f["rows"] for f in writers[d].files) == counts[d] for d in bulk.DECISIONS)
                    and sum(f["rows"] for f in accepted.files) == counts["include"], "Partial export counts differ")
            status("final_source_binding_check")
            require(sha(plan_path) == plan_sha and code_hashes() == before_code and bulk.inventory(parent_manifest) == parent,
                    "Plan/source/code changed during partial merge")
            for prefix in ("db", "protocol", "status", "model_binding"):
                require(sha(full.bound_path(root, base[prefix + "_path"])) == base[prefix + "_sha256"], "Base snapshot changed")
            for name, worker in workers.items():
                for file, key in (("protocol.json", "protocol_sha256"), ("state.sqlite3", "db_sha256"), ("status.json", "status_sha256")):
                    require(sha(worker / file) == components[name][key], "Worker snapshot changed: " + name + "/" + file)
                require((sha(worker / "STOP") if (worker / "STOP").is_file() else None) == components[name]["stop_sha256"], "Worker STOP marker changed")
                if name == "pc2":
                    require(sha(worker / "provisional_receipt.json") == components[name]["receipt_sha256"], "Completed worker receipt changed")
            manifest = {"schema": "cosmos-provisional-partial-export-1", **PARTIAL_FLAGS, "artifact_complete": True,
                "covered_unique_pairs": wanted, "unprocessed_unique_pairs": missing, "decisions": counts,
                "protocol_sha256": sha(out / "protocol.json"), "coverage_sha256": sha(out / "coverage.json"),
                "files": {decision: writer.files for decision, writer in writers.items()}, "include_with_source": accepted.files}
            bulk.atomic_json(export / "manifest.json", manifest)
            target.commit()
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            receipt = {"schema": SCHEMA, **PARTIAL_FLAGS, "artifact_complete": True, "state": "partial_artifact_complete",
                "completion_scope": "all_verified_committed_rows_only_not_all_candidates",
                "covered_unique_pairs": wanted, "parent_expected_unique_pairs": parent["expected_unique_pairs"],
                "unprocessed_unique_pairs": missing, "coverage_fraction": coverage["coverage_fraction"],
                "observed_unique_articles": observed_articles, "decisions": counts, "teacher_calls": 0,
                "protocol_sha256": sha(out / "protocol.json"), "split_plan_sha256": plan_sha,
                "state_sqlite_sha256": sha(out / "state.sqlite3"), "coverage_sha256": sha(out / "coverage.json"),
                "export_manifest": "exports/manifest.json", "export_manifest_sha256": sha(export / "manifest.json"),
                "finished_at": bulk.now()}
            bulk.atomic_json(out / "partial_receipt.json", receipt)
            status("partial_artifact_complete", artifact_complete=True, covered_unique_pairs=wanted,
                   unprocessed_unique_pairs=missing, coverage_fraction=coverage["coverage_fraction"])
            return receipt
        except BaseException as exc:
            if target is not None:
                target.rollback()
            status("failed", error={"type": type(exc).__name__, "message": str(exc)})
            raise
        finally:
            if reader is not None:
                reader.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("plan", "pc1-out", "pc2-out", "out"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--parent-manifest", type=Path)
    parser.add_argument("--shard-size", type=int, default=1000)
    parser.add_argument("--expected-covered", type=int)
    parser.add_argument("--expected-unprocessed", type=int)
    parser.add_argument("--expected-include", type=int)
    args = parser.parse_args()
    print(preview.canonical(merge_partial(args.plan, {"pc1": args.pc1_out, "pc2": args.pc2_out}, args.out,
        parent_manifest=args.parent_manifest, shard_size=args.shard_size, expected_covered=args.expected_covered,
        expected_unprocessed=args.expected_unprocessed, expected_include=args.expected_include)))
