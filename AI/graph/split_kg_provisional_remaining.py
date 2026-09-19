"""Copy the unprocessed suffix of a stopped provisional B run to two PC queues.

The original OS worker lock is held throughout validation and copying. Its
SQLite database is opened read-only and backed up with SQLite's backup API,
including committed WAL pages. No source, checkpoint, original STOP marker,
protocol or result is changed. A completed split is preparation only: the B
model's failed quality gate and every provisional-only flag remain unchanged.

Completed source rows are omitted; a partially consumed shard is copied from
its exact uncompressed byte cursor without parsing or reserializing its rows.
Untouched shards are copied unchanged. Largest-first byte balancing assumes
two equally fast GPUs, and makes no throughput promise. Each child source ref
maps to its parent by adding parent_start_byte to the uncompressed child offset.
The parent export must attest that every source row is a unique pair; committed
row and unique-pair counts must agree. The merge independently checks identities.

Only a new output directory is accepted. Interrupted preparation leaves an
incomplete directory without a final receipt; retry into another directory.
No inference, LLM calls, labels, training, publication or deletion occurs here.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import gzip
from pathlib import Path
import shutil
import sqlite3

import kg_provisional_bulk as bulk
import knowledge_graph_news_preview as preview
import knowledge_graph_reselection_worker as durable

SCHEMA = "cosmos-kg-provisional-remaining-split-1"
require, read, atomic_json = bulk.require, bulk.read, bulk.atomic_json
sha = preview.file_sha256


def _readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _flags(value):
    return all(type(value.get(key)) is type(expected) and value.get(key) == expected
               for key, expected in bulk.FLAGS.items())


def _inspect_part(path, offset, consumed, expected_rows):
    """Check the entire line inventory and the exact cursor, in bounded memory."""
    rows, boundary, total_bytes = 0, 0, 0
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as stream:
        for raw in stream:
            require(bool(raw.strip()), "Blank source row cannot be split")
            rows += 1
            total_bytes += len(raw)
            if rows == consumed:
                boundary = total_bytes
    require(rows == expected_rows, "Parent shard row count differs")
    require(boundary == offset and consumed <= rows, "Committed cursor is not the exact consumed-row boundary")
    return total_bytes


def _copy_suffix(source, destination, offset):
    opener = gzip.open if source.suffix == ".gz" else open
    with opener(source, "rb") as incoming, destination.open("wb") as outgoing:
        incoming.seek(offset)
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)


def split_remaining(manifest, paused_run, out):
    manifest, paused_run, out = (Path(value).resolve() for value in (manifest, paused_run, out))
    require(not out.exists(), "Use a new split output directory")
    for original in (manifest.parent, paused_run):
        require(not out.is_relative_to(original) and not original.is_relative_to(out),
                "Split output must be separate from parent source and paused run")
    require((paused_run / "provisional.lock").is_file(), "Original worker lock is missing")
    with durable.WorkerLock(paused_run / "provisional.lock"):
        source = bulk.inventory(manifest)  # Verifies every parent shard's bytes/hash.
        parent = read(manifest)
        dataset_protocol = read(manifest.parent / "protocol.json")
        require(parent.get("scope") != "parent_shard_subset", "Expected the original full-source manifest")
        require(source["expected_unique_pairs"] == sum(part["rows"] for part in source["files"]),
                "Parent must attest unique pairs equal all source rows")
        require(parent.get("source_inventory_fingerprint") and
                dataset_protocol.get("source_inventory_fingerprint") == parent["source_inventory_fingerprint"] and
                dataset_protocol.get("source_inventory"), "Parent source provenance is missing or inconsistent")
        protocol_path, status_path = paused_run / "protocol.json", paused_run / "status.json"
        protocol, status = read(protocol_path), read(status_path)
        protocol_sha, status_sha = sha(protocol_path), sha(status_path)
        require(protocol.get("schema") == bulk.SCHEMA and _flags(protocol), "Original protocol is not provisional B")
        require(protocol.get("inventory") == source and Path(protocol["manifest_path"]).resolve() == manifest,
                "Paused run belongs to another source inventory")
        require(protocol.get("source_part_sha256") == {part["path"]: part["sha256"] for part in source["files"]},
                "Paused run part bindings differ")
        require(protocol.get("student_module") == bulk.STUDENT_MODULE and
                protocol.get("policy") == "provisional_candidate_only" and
                protocol.get("decision_rule") == "three_class_argmax_no_confidence_cutoff" and
                protocol.get("teacher_execution") == "never" and
                protocol.get("labels_seen") is False and protocol.get("predictions_used_for_tuning") is False and
                protocol.get("release_validation_bypassed") is False, "Original execution policy differs")
        checkpoint = Path(protocol["model"]["checkpoint"]).resolve()
        require(not out.is_relative_to(checkpoint) and not checkpoint.is_relative_to(out),
                "Split output must be separate from the model")
        require(bulk.model_contract(checkpoint) == protocol["model"], "Original model binding changed")
        require(bulk.code_hashes() == protocol.get("code_hashes"), "Original code binding changed")
        require(status.get("state") in ("paused_stop_file", "paused_limit") and status.get("complete") is False
                and _flags(status) and type(status.get("teacher_calls")) is int and status["teacher_calls"] == 0,
                "Source run must be explicitly paused and provisional")
        stop_path = paused_run / "STOP"
        stop_sha = sha(stop_path) if stop_path.is_file() else None
        with closing(_readonly(paused_run / "state.sqlite3")) as db:
            require(durable.meta(db, "protocol_sha256") == protocol_sha, "Database protocol binding differs")
            bulk.validate_state(db, protocol)
            progress = bulk.read_progress(db)
            committed = db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
            require(committed == progress["consumed_source_rows"], "Consumed rows differ from committed unique pairs")
            require(0 <= committed < source["expected_unique_pairs"], "No remaining pairs to split")
            computed = bulk.status(db, protocol, status["state"])
            for key in ("processed_unique_pairs", "expected_unique_pairs", "decisions", "consumed_source_rows",
                        "completed_files", "expected_files", "student_inference_seconds", "student_setup_seconds"):
                require(status.get(key) == computed[key], "Paused status differs from committed database: " + key)
            cursors = {path: (offset, rows, complete) for path, offset, rows, complete in
                       db.execute("SELECT path,offset,rows_consumed,complete FROM files")}
            observed = {path: (count, distinct) for path, count, distinct in db.execute(
                "SELECT source_path,COUNT(*),COUNT(DISTINCT source_offset) FROM pairs GROUP BY source_path")}
            for path, (_, consumed, _) in cursors.items():
                require(observed.get(path, (0, 0)) == (consumed, consumed), "Committed source coverage is not unique")

            remaining = []
            untouched_started = False
            for index, part in enumerate(source["files"]):
                offset, consumed, complete = cursors[part["path"]]
                # The original worker consumes files in manifest order. Refuse
                # arbitrary holes even if a manually edited state is rehashed.
                require(not untouched_started or consumed == 0, "Committed source cursor has non-prefix holes")
                if not complete:
                    untouched_started = True
                total_bytes = _inspect_part(manifest.parent / part["path"], offset, consumed, part["rows"])
                require(not complete or offset == total_bytes, "Completed shard cursor is not EOF")
                if consumed == part["rows"]:
                    continue  # A max-pairs pause can precede the EOF transaction.
                remaining.append({"parent_path": part["path"], "parent_sha256": part["sha256"],
                    "parent_start_byte": offset, "parent_start_row": consumed,
                    "rows": part["rows"] - consumed, "parent_bytes": part["bytes"],
                    "bytes": total_bytes - offset if consumed else part["bytes"], "index": index})
            require(len(remaining) >= 2, "Two PCs require at least two nonempty remaining shards")
            require(committed + sum(item["rows"] for item in remaining) == source["expected_unique_pairs"],
                    "Remaining row denominator differs")
            out.mkdir(parents=True)
            (out / "base").mkdir()
            with closing(sqlite3.connect(out / "base/state.sqlite3")) as snapshot:
                db.backup(snapshot)
                snapshot.execute("PRAGMA journal_mode=DELETE")
            for name in ("protocol.json", "status.json"):
                shutil.copyfile(paused_run / name, out / "base" / name)
            atomic_json(out / "base/model_binding.json", protocol["model"])
            base = {"committed_unique_pairs": committed, "consumed_source_rows": progress["consumed_source_rows"],
                    "decisions": progress["decisions"], "stop_marker_preserved": True, "stop_marker_sha256": stop_sha}
            for key, name in (("db", "state.sqlite3"), ("protocol", "protocol.json"), ("status", "status.json"),
                              ("model_binding", "model_binding.json")):
                base[key + "_path"] = "base/" + name
                base[key + "_sha256"] = sha(out / "base" / name)
            require(base["protocol_sha256"] == protocol_sha and base["status_sha256"] == status_sha,
                    "Paused metadata changed during snapshot")

        groups, loads = [[], []], [0, 0]
        for item in sorted(remaining, key=lambda item: (-item["bytes"], item["parent_path"])):
            target = min(range(2), key=lambda i: (loads[i], i))
            groups[target].append(item)
            loads[target] += item["bytes"]
        partitions = []
        for index, group in enumerate(groups):
            partition_id = "pc" + str(index + 1)
            target = out / partition_id / "input"
            (target / "candidates").mkdir(parents=True)
            (target / "parent").mkdir()
            for name in ("manifest.json", "protocol.json", "_VERIFIED"):
                shutil.copyfile(manifest.parent / name, target / "parent" / name)
            files, mappings = [], []
            for item in sorted(group, key=lambda item: item["index"]):
                original = manifest.parent / item["parent_path"]
                suffix = ".jsonl.gz" if original.suffix == ".gz" and item["parent_start_row"] == 0 else ".jsonl"
                child_path = "candidates/part-" + str(item["index"]).zfill(6) + suffix
                destination = target / child_path
                if item["parent_start_row"]:
                    _copy_suffix(original, destination, item["parent_start_byte"])
                else:
                    shutil.copyfile(original, destination)
                file_sha = sha(destination)
                require(destination.stat().st_size == item["bytes"], "Copied remaining bytes differ")
                if not item["parent_start_row"]:
                    require(file_sha == item["parent_sha256"], "Untouched shard copy differs")
                files.append({"path": child_path, "sha256": file_sha, "rows": item["rows"], "bytes": item["bytes"]})
                mappings.append({"child_path": child_path, "child_sha256": file_sha,
                    **{key: item[key] for key in ("rows", "bytes", "parent_path", "parent_sha256",
                                                  "parent_start_byte", "parent_start_row")}})
            subset_protocol = {"schema": "cosmos-kg-remaining-queue-partition-1", "scope": "parent_shard_subset",
                "partition_index": index, "partition_count": 2, "weights": [1, 1], "balance_metric": "bytes",
                "parent_manifest_sha256": source["manifest_sha256"],
                "parent_protocol_sha256": parent["protocol_sha256"],
                "source_inventory_fingerprint": parent["source_inventory_fingerprint"],
                "model_receipt_sha256": dataset_protocol["model_receipt_sha256"],
                "code_sha256": dataset_protocol["code_sha256"], "parent_source_inventory": dataset_protocol["source_inventory"],
                "base_protocol_sha256": protocol_sha, "base_db_sha256": base["db_sha256"],
                "inference_performed": False, "global_selection_complete": False, **bulk.FLAGS}
            atomic_json(target / "protocol.json", subset_protocol)
            subset = {"schema": durable.MANIFEST_SCHEMA, "complete": True, "scope": "parent_shard_subset",
                "partition_index": index, "partition_count": 2, "files": files,
                "expected_unique_pairs": sum(item["rows"] for item in files),
                "parent_expected_unique_pairs": source["expected_unique_pairs"],
                "parent_manifest_sha256": source["manifest_sha256"], "parent_protocol_sha256": parent["protocol_sha256"],
                "source_inventory_fingerprint": parent["source_inventory_fingerprint"],
                "protocol_sha256": sha(target / "protocol.json"), "global_selection_complete": False,
                "ready_for_gnn_training": False, **bulk.FLAGS}
            atomic_json(target / "manifest.json", subset)
            atomic_json(target / "_VERIFIED", {"manifest_sha256": sha(target / "manifest.json")})
            durable.inventory(target / "manifest.json")
            partitions.append({"id": partition_id, "input_manifest": partition_id + "/input/manifest.json",
                "manifest_sha256": sha(target / "manifest.json"), "expected_unique_pairs": subset["expected_unique_pairs"],
                "bytes": loads[index], "mappings": mappings})
        require(bulk.inventory(manifest) == source, "Parent changed during split")
        require(sha(protocol_path) == protocol_sha and sha(status_path) == status_sha,
                "Paused metadata changed during split")
        require((sha(stop_path) if stop_path.is_file() else None) == stop_sha, "Original stop marker changed")
        require(bulk.code_hashes() == protocol["code_hashes"] and bulk.model_contract(checkpoint) == protocol["model"],
                "Original code/model changed during split")
        require(committed + sum(part["expected_unique_pairs"] for part in partitions) == source["expected_unique_pairs"],
                "Base and partition denominators differ")
        plan = {"schema": SCHEMA, "complete": True, **bulk.FLAGS,
            "inference_performed": False, "global_selection_complete": False,
            "labels_seen": False, "predictions_used_for_tuning": False,
            "parent": {"manifest_path": str(manifest), "manifest_sha256": source["manifest_sha256"],
                       "expected_unique_pairs": source["expected_unique_pairs"]},
            "base": base, "partitions": partitions, "remaining_unique_pairs": source["expected_unique_pairs"] - committed,
            "copy_mode": "copy", "balance_metric": "bytes", "weights": [1, 1],
            "mapping_offset_encoding": "uncompressed_utf8_bytes", "row_numbering": "zero_based_consumed_prefix",
            "code_sha256": sha(Path(__file__))}
        atomic_json(out / "split_plan.json", plan)
        atomic_json(out / "split_plan_receipt.json", {"schema": "cosmos-kg-provisional-split-receipt-1",
            "complete": True, "plan_sha256": sha(out / "split_plan.json"), **bulk.FLAGS})
        return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--paused-run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(preview.canonical(split_remaining(args.manifest, args.paused_run, args.out)))
