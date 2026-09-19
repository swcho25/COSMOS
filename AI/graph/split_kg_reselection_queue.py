"""Split one verified complete candidate export into disjoint local PC queues.

Whole shards are balanced by bytes (or rows) divided by positive integer speed
weights. These weights are scheduling assumptions, not hardware measurements.
Default copying creates independent inputs; --hardlink explicitly shares bytes.
Only input preparation completes here: no inference, transfer, or publication.
Each queue preserves the full parent manifest/protocol and declares subset scope.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

import knowledge_graph_reselection_worker as worker
import local_news_relevance as base


def split_queue(manifest_path, out, *, weights=(1, 1), metric="bytes", hardlink=False):
    manifest_path, out = Path(manifest_path).resolve(), Path(out).resolve()
    if len(weights) != 2 or any(type(x) is not int or x <= 0 for x in weights):
        raise ValueError("Exactly two positive integer PC weights are required")
    if metric not in ("bytes", "rows"):
        raise ValueError("Balance metric must be bytes or rows")
    source = worker.inventory(manifest_path)
    parent = json.loads(manifest_path.read_text(encoding="utf8"))
    protocol_path = manifest_path.parent / "protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf8"))
    protocol_file_sha = base.sha(protocol_path)
    protocol_sha = protocol_file_sha
    if parent.get("protocol_sha256") != protocol_sha:
        raise ValueError("Parent protocol hash differs")
    if parent.get("scope") == "parent_shard_subset":
        raise ValueError("Split requires the full parent export, not an existing subset")
    if (not parent.get("source_inventory_fingerprint") or
            protocol.get("source_inventory_fingerprint") != parent["source_inventory_fingerprint"] or
            not protocol.get("source_inventory") or not protocol.get("model_receipt_sha256") or
            not protocol.get("code_sha256", {}).get("knowledge_graph_relevance_policy.py")):
        raise ValueError("Parent source/model/policy provenance missing or inconsistent")
    parts = source["files"]
    if source["expected_unique_pairs"] != sum(x["rows"] for x in parts):
        raise ValueError("Parent must attest unique pairs equal all shard rows")
    positive = sorted((x for x in parts if x["rows"]), key=lambda x: (-x[metric], x["path"]))
    if len(positive) < 2:
        raise ValueError("Two PCs require at least two nonempty shards")
    if out.exists():
        raise ValueError("Use a new output directory")
    if out.is_relative_to(manifest_path.parent):
        raise ValueError("Split output must be outside the frozen parent directory")
    groups, loads = [[], []], [0, 0]
    for index, part in zip(sorted(range(2), key=lambda i: (-weights[i], i)), positive[:2]):
        groups[index].append(part)
        loads[index] += part[metric]
    for part in positive[2:] + sorted((x for x in parts if not x["rows"]), key=lambda x: x["path"]):
        index = min(range(2), key=lambda i: (loads[i] / weights[i], -weights[i], i))
        groups[index].append(part)
        loads[index] += part[metric]
    assigned = [x["path"] for group in groups for x in group]
    if len(assigned) != len(set(assigned)) or set(assigned) != {x["path"] for x in parts}:
        raise ValueError("Partition coverage differs from parent")
    out.mkdir(parents=True)
    partitions = []
    for index, group in enumerate(groups):
        target = out / ("pc" + str(index + 1))
        (target / "parent").mkdir(parents=True)
        for name, source_path in (("manifest.json", manifest_path), ("protocol.json", protocol_path),
                                  ("_VERIFIED", manifest_path.parent / "_VERIFIED")):
            shutil.copyfile(source_path, target / "parent" / name)
        for part in group:
            destination = (target / part["path"]).resolve()
            if not destination.is_relative_to(target):
                raise ValueError("Subset destination escapes queue")
            destination.parent.mkdir(parents=True, exist_ok=True)
            (os.link if hardlink else shutil.copyfile)(manifest_path.parent / part["path"], destination)
            if base.sha(destination) != part["sha256"] or destination.stat().st_size != part["bytes"]:
                raise ValueError("Copied shard differs from parent")
        subset_protocol = {"schema": "cosmos-kg-queue-partition-1", "scope": "parent_shard_subset",
            "partition_index": index, "partition_count": 2, "weights": list(weights), "balance_metric": metric,
            "parent_manifest_sha256": source["manifest_sha256"], "parent_protocol_sha256": protocol_sha,
            "parent_protocol_file_sha256": protocol_file_sha, "parent_protocol_digest_encoding": "raw_file_bytes",
            "source_inventory_fingerprint": parent["source_inventory_fingerprint"],
            "model_receipt_sha256": protocol["model_receipt_sha256"], "code_sha256": protocol["code_sha256"],
            "parent_source_inventory": protocol["source_inventory"], "inference_performed": False,
            "global_selection_complete": False}
        worker.atomic_json(target / "protocol.json", subset_protocol)
        subset = {"schema": worker.MANIFEST_SCHEMA, "complete": True, "scope": "parent_shard_subset",
            "partition_index": index, "partition_count": 2, "files": sorted(group, key=lambda x: x["path"]),
            "expected_unique_pairs": sum(x["rows"] for x in group),
            "parent_expected_unique_pairs": source["expected_unique_pairs"],
            "parent_manifest_sha256": source["manifest_sha256"], "parent_protocol_sha256": protocol_sha,
            "source_inventory_fingerprint": parent["source_inventory_fingerprint"],
            "protocol_sha256": base.sha(target / "protocol.json"), "global_selection_complete": False,
            "human_gold": False, "production_approved": False, "ready_for_gnn_training": False}
        worker.atomic_json(target / "manifest.json", subset)
        worker.atomic_json(target / "_VERIFIED", {"manifest_sha256": base.sha(target / "manifest.json")})
        partitions.append({"directory": target.name, "manifest_sha256": base.sha(target / "manifest.json"),
            "paths": [x["path"] for x in group], "expected_unique_pairs": subset["expected_unique_pairs"],
            "load": loads[index], "weight": weights[index]})
    if worker.inventory(manifest_path) != source or base.sha(protocol_path) != protocol_file_sha:
        raise ValueError("Parent changed during split; no completed split plan")
    plan = {"schema": "cosmos-kg-queue-split-1", "complete": True, "inference_performed": False,
        "global_selection_complete": False, "parent_manifest_sha256": source["manifest_sha256"],
        "parent_protocol_sha256": protocol_sha, "expected_unique_pairs": source["expected_unique_pairs"],
        "metric": metric, "weights": list(weights), "copy_mode": "hardlink" if hardlink else "copy",
        "partitions": partitions, "code_sha256": base.sha(Path(__file__))}
    worker.atomic_json(out / "split_plan.json", plan)
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--weights", type=int, nargs=2, default=[1, 1])
    parser.add_argument("--metric", choices=("bytes", "rows"), default="bytes")
    parser.add_argument("--hardlink", action="store_true")
    args = parser.parse_args()
    print(base.canonical(split_queue(args.manifest, args.out, weights=args.weights,
                                     metric=args.metric, hardlink=args.hardlink)))
