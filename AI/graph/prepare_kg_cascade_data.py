"""Freeze an article-group-balanced, label-free cascade pilot from ALL shards.

The verified seven-field candidate export is streamed once into a disk-backed
index. Articles are joined transitively by article ID, canonical URL and the
same NFC/whitespace-normalized raw/minimally-cleaned body keys as earlier review
exclusions. No body text is kept in the whole-source index. An exclusion touching
any member excludes its entire connected component, including bridges found in
later shards. Duplicate article/company pairs must have identical input hashes.

One target pair per component is chosen by a frozen hash. The lowest group hashes
are sampled and a separate hash assigns exact train/dev/calibration/test sizes.
This is an article-component-balanced pilot, NOT a random sample of all source
pairs. It adds no year, language, topic, length, confidence or sport filters.
Long/uncertain inputs remain visible for later fallback, never silently cropped.

--exclusions accepts NDJSON key rows {kind: id|body_sha256|url, value: string}
or aggregate rows {ids: [...], bodies: [...], urls: [...]}; optional source tags
record known_review/price_holdout provenance. Body digests must already use
prepare_local_relevance_pilot.text_hash/body_keys. The caller is responsible for
hydrating and validating the complete known-review and price-holdout inventory.
This module pins that file's bytes and reports its actual key coverage.

The four output directories have worker-compatible protocol/manifest/_VERIFIED
files. Test inputs are sealed by hashes before any teacher label is read. A new
output directory is required; failed runs retain diagnostic state without a
completion marker. No GPU, labels, predictions, network or production writes.
"""
from __future__ import annotations

import argparse
from array import array
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import heapq
import itertools
import json
from pathlib import Path
import sqlite3
import time

import knowledge_graph_news_preview as preview
import knowledge_graph_relevance_policy as policy
import knowledge_graph_reselection_worker as worker
import local_news_relevance as base
import prepare_local_relevance_pilot as prior


SEED = "kg-cascade-v1-20260917"
SIZES = {"train": 1536, "dev": 256, "calibration": 768, "test": 512}
SCOPE = "article_connected_component_balanced_pilot_one_target_per_group"
SCHEMA = "cosmos-kg-cascade-data-1"


def digest(value):
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def rank(identity, purpose):
    return digest(base.canonical([SEED, purpose, identity]))


def parse_line(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf8")
    return json.loads(raw, object_pairs_hook=policy.unique_object,
                      parse_constant=policy.reject_constant)


def open_binary(path):
    return gzip.open(path, "rb") if path.suffix.lower() == ".gz" else path.open("rb")


def load_exclusions(path):
    """Read identity keys only; verdict-bearing records are rejected."""
    keys = {"id": set(), "body_sha256": set(), "url": set()}
    sources = defaultdict(Counter)
    rows = 0
    with Path(path).open("rb") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"Blank exclusion line {number}")
            value = parse_line(line)
            if not isinstance(value, dict):
                raise ValueError("Exclusion record must be an object")
            source = value.get("source", "unspecified")
            if not isinstance(source, str) or not source:
                raise ValueError("Exclusion source must be a nonempty string")
            if "kind" in value:
                if set(value) - {"kind", "value", "source"} or not {"kind", "value"} <= set(value):
                    raise ValueError("Exclusion key schema differs")
                items = [(value["kind"], value["value"])]
            else:
                if not set(value) <= {"ids", "bodies", "urls", "source"} or not set(value) & {"ids", "bodies", "urls"}:
                    raise ValueError("Exclusion aggregate schema differs")
                items = []
                for field, kind in (("ids", "id"), ("bodies", "body_sha256"), ("urls", "url")):
                    members = value.get(field, [])
                    if not isinstance(members, list):
                        raise ValueError("Exclusion aggregate values must be arrays")
                    items.extend((kind, member) for member in members)
            for kind, key in items:
                if kind not in keys or not isinstance(key, str) or not key.strip():
                    raise ValueError("Invalid exclusion identity key")
                if kind == "body_sha256" and (len(key) != 64 or any(c not in "0123456789abcdef" for c in key)):
                    raise ValueError("Exclusion body digest must be lowercase SHA256")
                if kind == "url":
                    key = prior.canonicalize_url(key)
                    if key is None:
                        raise ValueError("Exclusion URL cannot be normalized")
                keys[kind].add(key)
                sources[source][kind] += 1
            rows += 1
    if not keys["id"] or not keys["body_sha256"]:
        raise ValueError("Hydrated exclusion inventory needs article IDs and body keys")
    return keys, {"rows": rows, "unique_keys": {k: len(v) for k, v in keys.items()},
                  "declared_keys_by_source": {k: dict(v) for k, v in sorted(sources.items())},
                  "source_inventory_completeness_verified_by_this_module": False}


def identity_keys(row):
    result = [("id", row["article_id"])]
    result.extend(("body_sha256", key) for key in sorted(prior.body_keys(row)))
    url = prior.canonicalize_url(row["source_url"])
    if url is not None:
        result.append(("url", url))
    return result


class Components:
    """Compact union-find arrays; bodies and millions of key strings stay in SQL."""
    def __init__(self):
        self.parents = array("I")
        self.sizes = array("I")
        self.excluded = bytearray()

    def add(self):
        node = len(self.parents)
        if node >= 2**32 - 1:
            raise ValueError("Connected-component index exceeds uint32 capacity")
        self.parents.append(node)
        self.sizes.append(1)
        self.excluded.append(0)
        return node

    def root(self, node):
        while self.parents[node] != node:
            self.parents[node] = self.parents[self.parents[node]]
            node = self.parents[node]
        return node

    def union(self, left, right):
        left, right = self.root(left), self.root(right)
        if left == right:
            return left
        if self.sizes[left] < self.sizes[right]:
            left, right = right, left
        self.parents[right] = left
        self.sizes[left] += self.sizes[right]
        self.excluded[left] |= self.excluded[right]
        return left

    def exclude(self, node):
        self.excluded[self.root(node)] = 1


def connect(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA temp_store=FILE")
    db.execute("PRAGMA cache_size=-65536")
    db.executescript("""
        CREATE TABLE identity_keys(kind TEXT NOT NULL, value TEXT NOT NULL,
            node INTEGER NOT NULL, PRIMARY KEY(kind,value)) WITHOUT ROWID;
        CREATE TABLE pairs(article_id TEXT NOT NULL, ticker TEXT NOT NULL,
            input_sha256 TEXT NOT NULL, node INTEGER NOT NULL, file_index INTEGER NOT NULL,
            byte_offset INTEGER NOT NULL, line_bytes INTEGER NOT NULL,
            PRIMARY KEY(article_id,ticker)) WITHOUT ROWID;
        CREATE TABLE roots(node INTEGER PRIMARY KEY, root INTEGER NOT NULL,
            excluded INTEGER NOT NULL);
        CREATE TABLE groups(root INTEGER PRIMARY KEY, canonical_id TEXT NOT NULL,
            pair_count INTEGER NOT NULL, excluded INTEGER NOT NULL);
        CREATE TABLE selected(root INTEGER PRIMARY KEY, fold TEXT NOT NULL,
            ordering TEXT NOT NULL, input_sha256 TEXT NOT NULL, payload TEXT NOT NULL);
    """)
    return db


def prepare(manifest, exclusions, out, *, sizes=None, progress_every=10000):
    """Prepare a new dataset; exact-sized synthetic fixtures may inject sizes."""
    started = time.perf_counter()
    manifest, exclusions, out = (Path(p).resolve() for p in (manifest, exclusions, out))
    sizes = dict(SIZES if sizes is None else sizes)
    if set(sizes) != set(SIZES) or any(type(n) is not int or n < 1 for n in sizes.values()):
        raise ValueError("All four partition sizes must be positive integers")
    if type(progress_every) is not int or progress_every < 1:
        raise ValueError("progress_every must be positive")
    if out.exists():
        raise FileExistsError("Output exists; choose a new cascade dataset directory")
    out.mkdir(parents=True)
    counts = Counter()

    def progress(state, **fields):
        worker.atomic_json(out / "status.json", {
            "schema": SCHEMA, "state": state, "complete": state == "complete",
            "counts": dict(counts), "elapsed_seconds": time.perf_counter() - started,
            "updated_at": datetime.now(timezone.utc).isoformat(), **fields})

    try:
        progress("verifying_source")
        source = worker.inventory(manifest)
        exclusion_sha = base.sha(exclusions)
        excluded_keys, exclusion_summary = load_exclusions(exclusions)
        if base.sha(exclusions) != exclusion_sha:
            raise ValueError("Exclusion inventory changed while being read")
        code_paths = [Path(__file__), Path(worker.__file__), Path(preview.__file__), Path(base.__file__),
                      Path(prior.__file__), worker.HERE / "lake_news_normalize.py",
                      worker.HERE / "news_content.py", worker.HERE.parent / "ner/matcher.py"]
        code_sha = {str(p): base.sha(p) for p in code_paths}
        parent_protocol = parse_line((manifest.parent / "protocol.json").read_bytes())
        plan = {
            "schema": SCHEMA, "seed": SEED, "sizes": sizes, "scope": SCOPE,
            "source_inventory": source, "source_manifest": str(manifest),
            "exclusions_path": str(exclusions), "exclusions_sha256": exclusion_sha,
            "exclusion_summary": exclusion_summary, "code_sha256": code_sha,
            "group_keys": ["article_id", "canonical_url", "NFC_whitespace_body_and_minimal_clean_body_sha256"],
            "grouping": "transitive_connected_components_all_source_rows_before_sampling",
            "group_identity": "lexicographically_smallest_article_id_in_component",
            "group_selection": "lowest_seeded_sha256_of_group_identity",
            "target_selection": "lowest_independently_seeded_sha256_of_canonical_input_digest",
            "fold_assignment": "separate_seeded_group_hash_order_then_exact_sized_partition_slices",
            "filters_added": [], "labels_read": False, "predictions_read": False,
            "human_gold": False, "production_approved": False,
            "test_use": "sealed_until_model_and_route_thresholds_are_frozen",
            "population_accuracy_claim": False,
        }
        worker.atomic_json(out / "sampling_plan.json", plan)
        plan_sha = base.sha(out / "sampling_plan.json")
        components = Components()
        with closing(connect(out / "index.sqlite3")) as db:
            progress("indexing_all_source")
            for file_index, part in enumerate(source["files"]):
                part_path = manifest.parent / part["path"]
                file_rows = 0
                with open_binary(part_path) as stream:
                    while True:
                        offset = stream.tell()
                        raw = stream.readline()
                        if not raw:
                            break
                        if not raw.strip():
                            raise ValueError("Blank source candidate line")
                        value = parse_line(raw)
                        if not isinstance(value, dict) or set(value) != set(preview.INPUT_FIELDS):
                            raise ValueError("Candidate row must contain exactly the seven input fields")
                        row = preview.validate_input(value)
                        sha = preview.canonical_input_sha256(row)
                        previous = db.execute("SELECT input_sha256 FROM pairs WHERE article_id=? AND ticker=?",
                                              preview.pair_key(row)).fetchone()
                        if previous:
                            if previous[0] != sha:
                                raise ValueError("Conflicting duplicate candidate pair")
                            counts["identical_duplicate_pairs"] += 1
                        else:
                            keys = identity_keys(row)
                            for kind in ("body_sha256", "url"):
                                if not any(k == kind for k, _ in keys):
                                    counts["pairs_without_" + kind] += 1
                            existing = db.execute("SELECT node FROM identity_keys WHERE kind='id' AND value=?",
                                                  (row["article_id"],)).fetchone()
                            node = existing[0] if existing else components.add()
                            for kind, key in keys:
                                linked = db.execute("SELECT node FROM identity_keys WHERE kind=? AND value=?",
                                                    (kind, key)).fetchone()
                                if linked is not None:
                                    components.union(node, linked[0])
                                else:
                                    db.execute("INSERT INTO identity_keys VALUES (?,?,?)", (kind, key, node))
                                if key in excluded_keys[kind]:
                                    components.exclude(node)
                                    counts["direct_exclusion_key_matches_" + kind] += 1
                            db.execute("INSERT INTO pairs VALUES (?,?,?,?,?,?,?)", (*preview.pair_key(row),
                                       sha, node, file_index, offset, len(raw)))
                            counts["unique_pairs"] += 1
                        counts["source_rows"] += 1
                        file_rows += 1
                        if counts["source_rows"] % progress_every == 0:
                            db.commit()
                            progress("indexing_all_source", current_file=part["path"],
                                     distinct_article_ids=len(components.parents))
                if file_rows != part["rows"]:
                    raise ValueError("Candidate part row count differs: " + part["path"])
                counts["completed_source_files"] += 1
                db.commit()
            if counts["unique_pairs"] != source["expected_unique_pairs"]:
                raise ValueError("Candidate distinct-pair denominator differs from manifest")
            progress("finalizing_transitive_groups")
            db.executemany("INSERT INTO roots VALUES (?,?,?)",
                ((node, components.root(node), components.excluded[components.root(node)])
                 for node in range(len(components.parents))))
            db.executescript("""
                CREATE INDEX roots_by_root ON roots(root);
                CREATE INDEX pairs_by_node ON pairs(node);
                INSERT INTO groups SELECT r.root, MIN(p.article_id), COUNT(*), MAX(r.excluded)
                    FROM pairs p JOIN roots r ON p.node=r.node GROUP BY r.root;
            """)
            db.commit()
            counts["article_ids"] = len(components.parents)
            counts["all_groups"] = db.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
            counts["excluded_groups"] = db.execute("SELECT COUNT(*) FROM groups WHERE excluded=1").fetchone()[0]
            counts["excluded_unique_pairs"] = db.execute("SELECT COALESCE(SUM(pair_count),0) FROM groups WHERE excluded=1").fetchone()[0]
            counts["eligible_groups"] = counts["all_groups"] - counts["excluded_groups"]
            for kind, n in db.execute("SELECT kind,COUNT(*) FROM identity_keys GROUP BY kind"):
                counts["identity_keys_" + kind] = n
            requested = sum(sizes.values())
            if counts["eligible_groups"] < requested:
                raise ValueError("Fewer eligible independent article groups than requested split sizes")
            # heapq.nsmallest keeps at most requested small records, not all bodies.
            groups = heapq.nsmallest(requested,
                ((rank(identity, "group"), identity, root, n) for root, identity, n in db.execute(
                    "SELECT root,canonical_id,pair_count FROM groups WHERE excluded=0")))
            groups.sort(key=lambda g: (rank(g[1], "fold"), g[1]))
            assignments, position = [], 0
            for fold in SIZES:
                for _, identity, root, pair_count in groups[position:position + sizes[fold]]:
                    candidates = db.execute("""SELECT p.article_id,p.ticker,p.input_sha256,
                        p.file_index,p.byte_offset,p.line_bytes FROM roots r
                        JOIN pairs p ON p.node=r.node WHERE r.root=?""", (root,))
                    chosen = min(candidates, key=lambda x: (rank(x[2], "target"), x[0], x[1]))
                    assignments.append({"root": root, "fold": fold, "group_id": identity,
                        "group_pair_count": pair_count, "ordering": rank(identity, "fold"),
                        "article_id": chosen[0], "ticker": chosen[1], "input_sha256": chosen[2],
                        "file_index": chosen[3], "byte_offset": chosen[4], "line_bytes": chosen[5]})
                position += sizes[fold]
            if len({a["root"] for a in assignments}) != requested:
                raise ValueError("Selected article components overlap")
            progress("extracting_selected_inputs")
            assignments.sort(key=lambda a: (a["file_index"], a["byte_offset"]))
            for file_index, group in itertools.groupby(assignments, key=lambda a: a["file_index"]):
                part = source["files"][file_index]
                with open_binary(manifest.parent / part["path"]) as stream:
                    for item in group:
                        stream.seek(item["byte_offset"])
                        raw = stream.read(item["line_bytes"])
                        row = preview.validate_input(parse_line(raw))
                        if preview.canonical_input_sha256(row) != item["input_sha256"]:
                            raise ValueError("Selected source bytes changed after indexing")
                        db.execute("INSERT INTO selected VALUES (?,?,?,?,?)", (item["root"], item["fold"],
                                   item["ordering"], item["input_sha256"], base.canonical(row)))
            db.commit()
            progress("rechecking_source_before_publication")
            if worker.inventory(manifest) != source or base.sha(exclusions) != exclusion_sha:
                raise ValueError("Source or exclusions changed during sampling")
            if any(base.sha(Path(path)) != sha for path, sha in code_sha.items()):
                raise ValueError("Preparation code changed during sampling")
            if base.sha(out / "sampling_plan.json") != plan_sha:
                raise ValueError("Frozen sampling plan changed during sampling")
            folds = {}
            for fold in SIZES:
                folder = out / fold
                folder.mkdir()
                path = folder / "inputs.jsonl"
                with path.open("x", encoding="utf8", newline="\n") as stream:
                    for (payload,) in db.execute("SELECT payload FROM selected WHERE fold=? ORDER BY ordering,root", (fold,)):
                        stream.write(payload + "\n")
                fold_protocol = {**parent_protocol,
                    "scope": SCOPE, "parent_export_protocol_sha256": source["dataset_protocol_sha256"],
                    "parent_export_manifest_sha256": source["manifest_sha256"],
                    "output": str(folder), "parent_export_output": parent_protocol.get("output"),
                    "cascade_sampling": {"plan_sha256": plan_sha, "partition": fold,
                        "seed": SEED, "rows": sizes[fold], "article_groups": sizes[fold],
                        "input_only": True, "labels_read": False,
                        "sealed_test": fold == "test", "population_accuracy_claim": False}}
                worker.atomic_json(folder / "protocol.json", fold_protocol)
                fold_manifest = {"schema": worker.MANIFEST_SCHEMA, "complete": True,
                    "protocol_sha256": base.sha(folder / "protocol.json"),
                    "expected_unique_pairs": sizes[fold], "files": [
                        {"path": path.name, "rows": sizes[fold], "bytes": path.stat().st_size,
                         "sha256": base.sha(path)}],
                    "scope": SCOPE, "partition": fold, "sampling_plan_sha256": plan_sha,
                    "parent_manifest_sha256": source["manifest_sha256"],
                    "production_approved": False, "human_gold": False}
                worker.atomic_json(folder / "manifest.json", fold_manifest)
                worker.atomic_json(folder / "_VERIFIED", {"manifest_sha256": base.sha(folder / "manifest.json")})
                verified = worker.inventory(folder / "manifest.json")
                folds[fold] = {"rows": sizes[fold], "manifest_sha256": verified["manifest_sha256"],
                    "inputs_sha256": base.sha(path), "protocol_sha256": verified["dataset_protocol_sha256"]}
            trace = out / "selection_trace.jsonl"
            with trace.open("x", encoding="utf8", newline="\n") as stream:
                for item in sorted(assignments, key=lambda a: (a["fold"], a["ordering"])):
                    stream.write(base.canonical({**item, "source_part": source["files"][item["file_index"]]["path"],
                        "offset_kind": "uncompressed_byte_offset", "group_canonical_id": item["group_id"]}) + "\n")
            counts["selected_groups"] = requested
            counts["selected_unique_pairs"] = requested
            receipt = {"schema": SCHEMA, "complete": True, "scope": SCOPE, "seed": SEED,
                "counts": dict(counts), "folds": folds, "sizes": sizes,
                "sampling_plan_sha256": plan_sha, "selection_trace_sha256": base.sha(trace),
                "source_inventory": source, "exclusions_sha256": exclusion_sha,
                "exclusion_summary": exclusion_summary, "cross_split_group_overlap": 0,
                "labels_read": False, "predictions_read": False,
                "test_sealed_before_teacher_inference": True,
                "human_gold": False, "ready_for_gnn_training": False, "production_approved": False,
                "population_accuracy_claim": False,
                "limitations": [
                    "Groups receive equal sampling weight; this is not pair-population sampling.",
                    "Exact normalized content/URL groups do not detect every paraphrase or shared event.",
                    "The caller validates completeness of the hydrated exclusion-key inventory.",
                    "Existing NER coverage defines the source universe; missed firms/news are absent.",
                    "A deterministic hash sample does not prove independent news events."]}
            worker.atomic_json(out / "manifest.json", receipt)
            worker.atomic_json(out / "_VERIFIED", {"manifest_sha256": base.sha(out / "manifest.json")})
            progress("complete")
            return receipt
    except BaseException as exc:
        progress("interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                 error={"type": type(exc).__name__, "message": str(exc)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--exclusions", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    for fold, n in SIZES.items():
        parser.add_argument("--" + fold, type=int, default=n)
    args = parser.parse_args()
    receipt = prepare(args.manifest, args.exclusions, args.out,
                      sizes={fold: getattr(args, fold) for fold in SIZES})
    print(base.canonical({"complete": receipt["complete"], "counts": receipt["counts"],
                          "sizes": receipt["sizes"], "scope": receipt["scope"]}))


if __name__ == "__main__":
    main()
