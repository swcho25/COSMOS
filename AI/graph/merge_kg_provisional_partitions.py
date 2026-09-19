"""Verify and merge a paused B prefix and two completed provisional workers.

No model, teacher or held-out label is loaded. The split receipt, base snapshot,
worker protocols, SQLite row/chunk checksums, published exports and source bytes
are checked before a new merged receipt can be published. Predictions and input
digests are preserved verbatim; only source_ref is remapped to the original
parent, with the original worker reference/checksum retained as provenance.

Duplicate pairs, duplicate source positions, missing rows, mismatched models,
code or offsets are fatal. Inputs are opened read-only and are never changed.
Merge output must be new. An interrupted/failed attempt remains incomplete and
must not be consumed; rerun into another fresh output directory. --check-plan
checks the small split plan and bound metadata without opening models, workers,
or scanning full source parts. Actual merge is allowed only after both workers
have exited, checkpointed their SQLite WAL, and published completion receipts.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import gzip
import hashlib
from pathlib import Path
import sqlite3

import kg_provisional_bulk as bulk

HERE = Path(__file__).resolve().parent
SCHEMA = "cosmos-kg-provisional-merged-1"
SPLIT_SCHEMA = "cosmos-kg-provisional-remaining-split-1"
read, require, sha = bulk.read, bulk.require, bulk.preview.file_sha256
preview, durable, transfer = bulk.preview, bulk.durable, bulk.transfer


def bound_path(root, relative):
    require(isinstance(relative, str) and relative, "Missing relative path")
    path = (root / relative).resolve()
    require(not Path(relative).is_absolute() and path.is_relative_to(root), "Bound path escapes its directory")
    return path


def check_flags(value):
    require(all(type(value.get(key)) is type(item) and value.get(key) == item for key, item in bulk.FLAGS.items()),
            "Provisional flags differ")


def check_plan(path):
    path = Path(path).resolve()
    root, plan = path.parent, read(path)
    receipt = read(root / "split_plan_receipt.json")
    require(receipt.get("schema") == "cosmos-kg-provisional-split-receipt-1"
            and receipt.get("complete") is True and receipt.get("plan_sha256") == sha(path), "Split plan receipt differs")
    require(plan.get("schema") == SPLIT_SCHEMA and plan.get("complete") is True, "Incomplete/unknown split plan")
    check_flags(plan)
    parent, base, parts = plan["parent"], plan["base"], plan["partitions"]
    require(isinstance(parts, list) and len(parts) == 2 and {part["id"] for part in parts} == {"pc1", "pc2"},
            "Exactly pc1 and pc2 partitions are required")
    require(type(parent["expected_unique_pairs"]) is int and parent["expected_unique_pairs"] > 0
            and type(base["committed_unique_pairs"]) is int and base["committed_unique_pairs"] >= 0,
            "Invalid parent/base denominator")
    for prefix in ("db", "protocol", "status", "model_binding"):
        bound_path(root, base[prefix + "_path"])
        require(transfer.valid_sha(base[prefix + "_sha256"]), "Invalid base binding")
    for part in parts:
        require(type(part["expected_unique_pairs"]) is int and part["expected_unique_pairs"] > 0,
                "Invalid child denominator")
        manifest = bound_path(root, part["input_manifest"])
        require(sha(manifest) == part["manifest_sha256"], "Partition manifest changed")
        child = read(manifest)
        require(child.get("complete") is True and child["expected_unique_pairs"] == part["expected_unique_pairs"],
                "Partition denominator differs")
        mappings = part["mappings"]
        require(isinstance(mappings, list) and len({m["child_path"] for m in mappings}) == len(mappings),
                "Duplicate child mappings")
        expected = {m["child_path"]: {"path": m["child_path"], "sha256": m["child_sha256"], "rows": m["rows"], "bytes": m["bytes"]}
                    for m in mappings}
        require(expected == {item["path"]: item for item in child["files"]}
                and len(child["files"]) == len(expected), "Child file mappings differ from manifest")
        require(sum(m["rows"] for m in mappings) == part["expected_unique_pairs"]
                and sum(m["bytes"] for m in mappings) == part["bytes"], "Child mapping counts differ")
        for mapping in mappings:
            bound_path(manifest.parent, mapping["child_path"])
            require(all(type(mapping[k]) is int and mapping[k] >= 0 for k in ("parent_start_byte", "parent_start_row", "rows", "bytes"))
                    and transfer.valid_sha(mapping["parent_sha256"]), "Invalid parent mapping")
    require(base["committed_unique_pairs"] + sum(p["expected_unique_pairs"] for p in parts) == parent["expected_unique_pairs"],
            "Split denominator does not cover parent")
    return plan


def readonly_db(path):
    require(path.is_file(), "Missing SQLite snapshot")
    wal = Path(str(path) + "-wal")
    require(not wal.exists() or wal.stat().st_size == 0, "Worker SQLite WAL must be checkpointed before merge")
    return sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)


def _model_key(value):
    return {key: item for key, item in value.items() if key != "checkpoint"}


def _safe_float_restore(value):
    # Provisional runner makes nonfinite values serializable without turning
    # them into valid probabilities. Reconstruct only for deterministic replay;
    # retain the exact original serialized raw_prediction in the merged output.
    if isinstance(value, dict):
        if set(value) == {"invalid_nonfinite_number"} and value["invalid_nonfinite_number"] in ("nan", "inf", "-inf"):
            return float(value["invalid_nonfinite_number"])
        return {key: _safe_float_restore(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_float_restore(item) for item in value]
    return value


class ParentReader:
    def __init__(self, manifest):
        self.root, self.path, self.stream = manifest.parent, None, None

    def seek(self, relative, offset):
        if relative != self.path:
            self.close()
            path = bound_path(self.root, relative)
            self.stream = (gzip.open if path.suffix == ".gz" else open)(path, "rb")
            self.path = relative
        self.stream.seek(offset)
        return self.stream

    def row(self, ref):
        offset, length = ref["byte_offset"], ref["line_bytes"]
        require(type(offset) is int and offset >= 0 and type(length) is int and length > 0, "Invalid source offset/length")
        stream = self.seek(ref["path"], max(0, offset - 1))
        if offset:
            require(stream.read(1) == b"\n", "Source offset is not a line boundary")
        raw = stream.readline()
        require(len(raw) == length and raw.strip(), "Source line length differs")
        value = transfer.strict_json(raw.decode("utf8"))
        require(value.get("company_context") is None, "Unexpected external source context")
        return preview.validate_input(value)

    def close(self):
        if self.stream is not None:
            self.stream.close()
        self.path, self.stream = None, None


def validate_protocol(protocol, base, inventory):
    require(protocol.get("schema") == bulk.SCHEMA and protocol.get("inventory") == inventory,
            "Worker source protocol differs")
    check_flags(protocol)
    require(protocol.get("student_module") == bulk.STUDENT_MODULE
            and _model_key(protocol["model"]) == _model_key(base["model"]), "Worker model differs from split base")
    require(protocol.get("code_hashes") == base["code_hashes"]
            and protocol.get("decision_rule") == "three_class_argmax_no_confidence_cutoff"
            and protocol.get("teacher_execution") == "never"
            and protocol.get("labels_seen") is False and protocol.get("predictions_used_for_tuning") is False,
            "Worker code, decision policy or scope differs")
    require(protocol.get("source_part_sha256") == {x["path"]: x["sha256"] for x in inventory["files"]},
            "Worker source part binding differs")


def worker_rows(out, protocol, db, expected_pairs):
    receipt_path = out / "provisional_receipt.json"
    receipt = read(receipt_path)
    protocol_sha = sha(out / "protocol.json")
    check_flags(receipt)
    count, articles = db.execute("SELECT COUNT(*),COUNT(DISTINCT article_id) FROM pairs").fetchone()
    require(receipt.get("schema") == bulk.SCHEMA and receipt.get("state") == "provisional_complete"
            and receipt.get("complete") is True and receipt.get("protocol_sha256") == protocol_sha
            and receipt.get("processed_unique_pairs") == count == expected_pairs
            and receipt.get("expected_unique_pairs") == expected_pairs and receipt.get("observed_unique_articles") == articles
            and receipt.get("decisions") == bulk.read_progress(db)["decisions"]
            and type(receipt.get("teacher_calls")) is int and receipt["teacher_calls"] == 0,
            "Worker is incomplete or receipt counters differ")
    require(durable.meta(db, "protocol_sha256") == protocol_sha
            and durable.meta(db, "provisional_receipt_sha256") == sha(receipt_path), "Worker SQLite receipt binding differs")
    require(db.execute("SELECT COUNT(*) FROM files WHERE complete=0").fetchone()[0] == 0, "Worker has incomplete source files")
    bulk.validate_state(db, protocol)
    bulk.verify_export(out, receipt["outputs"], receipt["decisions"], protocol)
    root = bound_path(out, receipt["outputs"]["root"])
    emitted = 0
    for decision, items in receipt["outputs"]["files"].items():
        for item in items:
            path = bound_path(root, item["path"])
            lines = 0
            with path.open(encoding="utf8") as stream:
                for line in stream:
                    value = transfer.strict_json(line)
                    payload_sha = bulk.digest(value)
                    saved = db.execute("SELECT result_sha256,decision FROM pairs WHERE article_id=? AND ticker=?",
                                       preview.pair_key(value)).fetchone()
                    require(saved == (payload_sha, decision) and value["decision"] == decision,
                            "Published result differs from durable worker row")
                    lines += 1
                    emitted += 1
                    yield value, payload_sha
            require(lines == item["rows"], "Published shard row count differs")
    require(emitted == expected_pairs, "Worker published row coverage differs")


def merge(plan_path, worker_outputs, out, *, parent_manifest=None, shard_size=1000):
    require(set(worker_outputs) == {"pc1", "pc2"}, "Both completed workers are required")
    require(type(shard_size) is int and 1 <= shard_size <= 10000, "Invalid output shard size")
    plan_path, out = Path(plan_path).resolve(), Path(out).resolve()
    plan = check_plan(plan_path)
    plan_sha, root = sha(plan_path), plan_path.parent
    parent_manifest = Path(parent_manifest or plan["parent"]["manifest_path"]).resolve()
    require(sha(parent_manifest) == plan["parent"]["manifest_sha256"], "Parent manifest changed")
    parent = bulk.inventory(parent_manifest)
    require(parent["expected_unique_pairs"] == plan["parent"]["expected_unique_pairs"], "Parent denominator differs")
    base, components = plan["base"], {}
    for prefix in ("db", "protocol", "status", "model_binding"):
        path = bound_path(root, base[prefix + "_path"])
        require(sha(path) == base[prefix + "_sha256"], "Base snapshot binding changed: " + prefix)
    base_protocol = read(bound_path(root, base["protocol_path"]))
    require(read(bound_path(root, base["model_binding_path"])) == base_protocol["model"], "Base model binding differs")
    require(base_protocol["code_hashes"] == bulk.code_hashes(), "Original provisional implementation changed")
    validate_protocol(base_protocol, base_protocol, parent)
    protected = [root, parent_manifest.parent, *(Path(p).resolve() for p in worker_outputs.values())]
    require(not out.exists() and all(not out.is_relative_to(p) and not p.is_relative_to(out) for p in protected),
            "Merge requires a new output separate from all inputs")
    out.mkdir(parents=True)
    protocol = {"schema": SCHEMA, **bulk.FLAGS, "split_plan_sha256": plan_sha, "parent_inventory": parent,
        "model": base_protocol["model"], "worker_code_hashes": base_protocol["code_hashes"],
        "merge_code_sha256": sha(Path(__file__)), "teacher_calls": 0, "labels_seen": False,
        "predictions_used_for_tuning": False, "inputs_inferred": True, "components": components}
    part_by_path = {x["path"]: x for x in parent["files"]}
    reader = ParentReader(parent_manifest)
    counts = dict.fromkeys(bulk.DECISIONS, 0)
    processed = 0
    def progress(state, **extra):
        bulk.atomic_json(out / "status.json", {"schema": SCHEMA, "state": state, "complete": state == "provisional_complete",
            **bulk.FLAGS, "processed_unique_pairs": processed, "expected_unique_pairs": parent["expected_unique_pairs"],
            "decisions": counts, "teacher_calls": 0, "updated_at": bulk.now(), **extra})
    with ExitStack() as stack:
        target = stack.enter_context(closing(sqlite3.connect(out / "state.sqlite3")))
        target.execute("PRAGMA journal_mode=WAL")
        target.execute("PRAGMA synchronous=FULL")
        target.executescript("""CREATE TABLE pairs(article_id TEXT,ticker TEXT,input_sha256 TEXT,
            source_path TEXT,source_offset INTEGER,line_bytes INTEGER,decision TEXT,result_json TEXT,result_sha256 TEXT,
            component TEXT,PRIMARY KEY(article_id,ticker),UNIQUE(source_path,source_offset));""")
        try:
            progress("verifying_components")
            db = stack.enter_context(closing(readonly_db(bound_path(root, base["db_path"]))))
            require(durable.meta(db, "protocol_sha256") == base["protocol_sha256"], "Base SQLite protocol differs")
            bulk.validate_state(db, base_protocol)
            require(db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0] == base["committed_unique_pairs"], "Base count differs")
            cursors = {path: (offset, consumed) for path, offset, consumed in db.execute("SELECT path,offset,rows_consumed FROM files")}
            mapped = {}
            for partition in plan["partitions"]:
                for item in partition["mappings"]:
                    path = item["parent_path"]
                    require(path in part_by_path and path not in mapped, "Duplicate or unknown mapped parent shard")
                    offset, consumed = cursors[path]
                    require((item["parent_start_byte"], item["parent_start_row"]) == (offset, consumed)
                            and item["rows"] == part_by_path[path]["rows"] - consumed
                            and item["parent_sha256"] == part_by_path[path]["sha256"], "Split mapping differs from base source cursor")
                    mapped[path] = item
            require(set(mapped) == {path for path, (_, consumed) in cursors.items() if consumed < part_by_path[path]["rows"]},
                    "Split mapping does not cover all remaining parent shards")
            components["base"] = {"protocol_sha256": base["protocol_sha256"], "db_sha256": base["db_sha256"],
                                  "expected_pairs": base["committed_unique_pairs"]}
            jobs = [("base", base_protocol, db, None, None)]
            for part in plan["partitions"]:
                name, worker = part["id"], Path(worker_outputs[part["id"]]).resolve()
                source = bulk.inventory(bound_path(root, part["input_manifest"]))
                require(source["manifest_sha256"] == part["manifest_sha256"], "Worker partition manifest differs")
                child = read(bound_path(root, part["input_manifest"]))
                require(child.get("scope") == "parent_shard_subset"
                        and child.get("parent_manifest_sha256") == parent["manifest_sha256"], "Child belongs to another split source")
                worker_protocol = read(worker / "protocol.json")
                validate_protocol(worker_protocol, base_protocol, source)
                worker_db = stack.enter_context(closing(readonly_db(worker / "state.sqlite3")))
                components[name] = {"protocol_sha256": sha(worker / "protocol.json"),
                    "receipt_sha256": sha(worker / "provisional_receipt.json"),
                    "db_sha256": sha(worker / "state.sqlite3"), "expected_pairs": part["expected_unique_pairs"]}
                jobs.append((name, worker_protocol, worker_db, worker, part))
            for name, source_protocol, component_db, worker, part in jobs:
                mapping = {item["child_path"]: item for item in part["mappings"]} if part else None
                if name == "base":
                    rows = ((transfer.strict_json(payload), row_sha) for payload, row_sha in component_db.execute(
                        "SELECT result_json,result_sha256 FROM pairs ORDER BY source_path,source_offset"))
                else:
                    rows = worker_rows(worker, source_protocol, component_db, part["expected_unique_pairs"])
                component_count = 0
                for value, result_sha in rows:
                    require(bulk.digest(value) == result_sha, "Component result checksum changed")
                    original_ref = value["source_ref"]
                    require(original_ref == bulk.source_ref(source_protocol, original_ref["path"], original_ref["byte_offset"], original_ref["line_bytes"]),
                            "Component source reference differs")
                    if mapping is None:
                        ref = dict(original_ref)
                    else:
                        item = mapping[original_ref["path"]]
                        require(item["parent_path"] in part_by_path
                                and item["parent_sha256"] == part_by_path[item["parent_path"]]["sha256"]
                                and (item["child_path"].endswith(".gz") or
                                     original_ref["byte_offset"] + original_ref["line_bytes"] <= item["bytes"]),
                                "Partition source mapping exceeds its bound")
                        ref = bulk.source_ref(base_protocol, item["parent_path"],
                                              item["parent_start_byte"] + original_ref["byte_offset"], original_ref["line_bytes"])
                    row = reader.row(ref)
                    require(preview.pair_key(row) == preview.pair_key(value)
                            and preview.canonical_input_sha256(row) == value["input_sha256"], "Original parent source identity differs")
                    replayed = bulk.interpret(row, _safe_float_restore(value["raw_prediction"]), base_protocol["model"]["student_manifest_sha256"])
                    require(value == {"article_id": row["article_id"], "ticker": row["ticker"], "input_sha256": value["input_sha256"],
                                      "source_ref": original_ref, **replayed}, "Stored provisional interpretation differs")
                    merged = {**value, "source_ref": ref, "component_ref": {"component": name,
                        "protocol_sha256": components[name]["protocol_sha256"], "result_sha256": result_sha,
                        "source_ref": original_ref}}
                    payload = preview.canonical(merged)
                    try:
                        target.execute("INSERT INTO pairs VALUES (?,?,?,?,?,?,?,?,?,?)", (*preview.pair_key(row), value["input_sha256"],
                            ref["path"], ref["byte_offset"], ref["line_bytes"], value["decision"], payload, bulk.digest(merged), name))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("Duplicate pair or original source position across components") from exc
                    processed += 1
                    component_count += 1
                    counts[value["decision"]] += 1
                    if processed % 1000 == 0:
                        target.commit()
                        progress("merging", component=name)
                require(component_count == components[name]["expected_pairs"], "Component coverage differs")
                target.commit()
            require(processed == parent["expected_unique_pairs"], "Merged unique-pair coverage differs")
            for path, item in part_by_path.items():
                cursor, rows = 0, 0
                for offset, length in target.execute("SELECT source_offset,line_bytes FROM pairs WHERE source_path=? ORDER BY source_offset", (path,)):
                    require(offset == cursor, "Missing or overlapping original source byte range")
                    cursor += length
                    rows += 1
                require(rows == item["rows"] and reader.seek(path, cursor).read(1) == b"", "Parent row/EOF coverage differs")
            articles = target.execute("SELECT COUNT(DISTINCT article_id) FROM pairs").fetchone()[0]
            expected_articles = read(parent_manifest).get("counts", {}).get("mention_unique_articles")
            require(expected_articles is None or articles == expected_articles, "Merged article denominator differs")
            export = out / "exports"
            export.mkdir()
            writers = {decision: bulk.shared.Shards(export, decision, shard_size) for decision in bulk.DECISIONS}
            try:
                for decision, payload, expected in target.execute("SELECT decision,result_json,result_sha256 FROM pairs ORDER BY source_path,source_offset"):
                    require(hashlib.sha256(payload.encode("utf8")).hexdigest() == expected, "Merged row checksum differs")
                    writers[decision].add(transfer.strict_json(payload))
            finally:
                for writer in writers.values(): writer.close()
            require(all(sum(f["rows"] for f in writers[d].files) == counts[d] for d in bulk.DECISIONS), "Merged export counts differ")
            require(sha(plan_path) == plan_sha and bulk.inventory(parent_manifest) == parent
                    and sha(Path(__file__)) == protocol["merge_code_sha256"], "Merge plan/source/code changed during merge")
            for prefix in ("db", "protocol", "status", "model_binding"):
                require(sha(bound_path(root, base[prefix + "_path"])) == base[prefix + "_sha256"], "Base changed during merge")
            for name, _, _, worker, _ in jobs[1:]:
                require(sha(worker / "protocol.json") == components[name]["protocol_sha256"]
                        and sha(worker / "provisional_receipt.json") == components[name]["receipt_sha256"]
                        and sha(worker / "state.sqlite3") == components[name]["db_sha256"], "Worker changed during merge")
            bulk.atomic_json(out / "protocol.json", protocol)
            export_manifest = {"schema": "cosmos-provisional-merged-export-1", "complete": True, **bulk.FLAGS,
                "protocol_sha256": sha(out / "protocol.json"), "decisions": counts,
                "files": {decision: writer.files for decision, writer in writers.items()}}
            bulk.atomic_json(export / "manifest.json", export_manifest)
            target.commit()
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            receipt = {"schema": SCHEMA, "state": "provisional_complete", "complete": True, **bulk.FLAGS,
                "completion_scope": "merged_provisional_predictions_only_not_validated_selection",
                "processed_unique_pairs": processed, "expected_unique_pairs": parent["expected_unique_pairs"],
                "observed_unique_articles": articles, "decisions": counts, "teacher_calls": 0,
                "protocol_sha256": sha(out / "protocol.json"), "split_plan_sha256": plan_sha,
                "state_sqlite_sha256": sha(out / "state.sqlite3"), "export_manifest": "exports/manifest.json",
                "export_manifest_sha256": sha(export / "manifest.json"), "finished_at": bulk.now()}
            bulk.atomic_json(out / "provisional_receipt.json", receipt)
            progress("provisional_complete")
            return receipt
        except BaseException as exc:
            target.rollback()
            progress("failed", error={"type": type(exc).__name__, "message": str(exc)})
            raise
        finally:
            reader.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--check-plan", action="store_true")
    parser.add_argument("--pc1-out", type=Path)
    parser.add_argument("--pc2-out", type=Path)
    parser.add_argument("--parent-manifest", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--shard-size", type=int, default=1000)
    args = parser.parse_args()
    if args.check_plan:
        checked = check_plan(args.plan)
        print(preview.canonical({"plan_valid": True, "complete_workers_verified": False,
            "expected_unique_pairs": checked["parent"]["expected_unique_pairs"],
            "base_pairs": checked["base"]["committed_unique_pairs"],
            "partitions": {p["id"]: p["expected_unique_pairs"] for p in checked["partitions"]}}))
    else:
        if not args.pc1_out or not args.pc2_out or not args.out:
            parser.error("merge requires --pc1-out, --pc2-out and a new --out")
        print(preview.canonical(merge(args.plan, {"pc1": args.pc1_out, "pc2": args.pc2_out}, args.out,
            parent_manifest=args.parent_manifest, shard_size=args.shard_size)))
