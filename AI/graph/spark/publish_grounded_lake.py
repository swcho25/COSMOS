"""Fail-closed grounded-KG preflight and resumable HDFS-only publication.

The assembler writes the same bounded-shard contract as publish_lake_backfill,
plus grounded_contract with a hash-bound local semantic verification report.
This wrapper independently checks every source row against the exact declared
schema before Spark can silently discard unknown fields or coerce values. It
requires all eight product roots, evidence/documents/history sidecars, and source
lineage checks. The report is a record of upstream checks, not a replacement for
performing them or a claim of model precision. NULL sentiment stays NULL.

The existing publisher verifies every Parquet shard by exact bidirectional
exceptAll, then global keys, schema, counts and file checksums. Immutable source
lineage, publication, semantic and schema preflight reports accompany its final
commit. Only the last global _VERIFIED marker commits the complete run. The
grounded extension permits fixed named graph sidecars only; old callers keep
their original path allowlist. No database or model is changed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import publish_lake_backfill as publisher


EXTRA_GRAPH_PATHS = frozenset({"historical_relationships", "evidence", "documents"})
REQUIRED_DESTINATIONS = frozenset({
    ("/data-lake/cleaned/news", "data"),
    ("/data-lake/analyzed/news/company-mentions", "data"),
    ("/data-lake/analyzed/news/relationships", "data"),
    ("/data-lake/features/company", "data"),
    ("/data-lake/features/relationship", "data"),
    ("/data-lake/aggregated/company-metrics", "data"),
    ("/data-lake/aggregated/relationship-scores", "data"),
    ("/data-lake/snapshots/graph", "nodes"),
    ("/data-lake/snapshots/graph", "edges"),
    ("/data-lake/snapshots/graph", "historical_relationships"),
    ("/data-lake/snapshots/graph", "evidence"),
    ("/data-lake/snapshots/graph", "documents"),
    ("/data-lake/metadata/id-maps/news", "data"),
    ("/data-lake/analyzed/news/sentiment", "data"),
    ("/data-lake/analyzed/disclosures/company-mentions", "data"),
    ("/data-lake/analyzed/disclosures/relationships", "data"),
})
REQUIRED_CHECKS = frozenset({"source_lineage", "canonical_document_ids", "foreign_keys",
                             "evidence_quotes", "window_aggregates", "null_semantics"})
INTEGER_BITS = {"byte": 8, "short": 16, "integer": 32, "long": 64}
PRIMITIVES = frozenset({"string", "boolean", "double", "float", "date", "timestamp", *INTEGER_BITS})


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"Nonfinite JSON value: {value}")


def strict_json(text):
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)


def _schema(schema, label):
    if isinstance(schema, str):
        if schema not in PRIMITIVES:
            raise ValueError(f"Unsupported exact JSON schema type at {label}: {schema}")
        return
    if not isinstance(schema, dict):
        raise ValueError(f"Invalid schema at {label}")
    kind = schema.get("type")
    if kind == "struct":
        fields = schema.get("fields")
        if not isinstance(fields, list) or not fields:
            raise ValueError(f"Empty/invalid struct at {label}")
        names = set()
        for field in fields:
            name = field.get("name")
            if not isinstance(name, str) or not name or name in names or type(field.get("nullable")) is not bool:
                raise ValueError(f"Invalid field declaration at {label}")
            names.add(name)
            # JSON and Parquet readers normalize nullable=True. Require that
            # reality explicitly; key non-nullability is checked separately.
            if field["nullable"] is not True:
                raise ValueError(f"Use nullable=true schema and explicit semantic constraints at {label}.{name}")
            _schema(field.get("type"), label + "." + name)
    elif kind == "array":
        if type(schema.get("containsNull")) is not bool:
            raise ValueError(f"Array requires explicit containsNull at {label}")
        _schema(schema.get("elementType"), label + "[]")
    else:
        # Spark exceptAll does not support maps. JSON provenance is a string.
        raise ValueError(f"Unsupported structured schema at {label}: {kind}")


def exact_value(value: Any, schema: Any, nullable: bool, label: str):
    if value is None:
        if not nullable:
            raise ValueError(f"Unexpected NULL at {label}")
        return
    if isinstance(schema, dict):
        if schema["type"] == "struct":
            if not isinstance(value, dict):
                raise ValueError(f"Expected object at {label}")
            fields = {field["name"]: field for field in schema["fields"]}
            if set(value) != set(fields):
                raise ValueError(f"Exact field set mismatch at {label}: missing={sorted(set(fields)-set(value))}, extra={sorted(set(value)-set(fields))}")
            for name, field in fields.items():
                exact_value(value[name], field["type"], field["nullable"], label + "." + name)
        elif schema["type"] == "array":
            if not isinstance(value, list):
                raise ValueError(f"Expected array at {label}")
            for index, item in enumerate(value):
                exact_value(item, schema["elementType"], schema["containsNull"], f"{label}[{index}]")
        return
    if schema in {"string", "date", "timestamp"}:
        if not isinstance(value, str):
            raise ValueError(f"Expected {schema} string at {label}")
        if schema == "date":
            dt.date.fromisoformat(value)
        elif schema == "timestamp":
            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError(f"Timestamp must have timezone at {label}")
    elif schema == "boolean":
        if type(value) is not bool:
            raise ValueError(f"Expected boolean at {label}")
    elif schema in INTEGER_BITS:
        bits = INTEGER_BITS[schema]
        if type(value) is not int or not -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
            raise ValueError(f"Expected bounded {schema} at {label}")
    elif schema in {"double", "float"}:
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError(f"Expected finite number at {label}")
        if schema == "float" and abs(value) > 3.4028234663852886e38:
            raise ValueError(f"Float overflow at {label}")


def validate_grounded(root, run_id):
    root = Path(root).resolve()
    publication, digest = publisher.validate_publication(root, run_id, extra_graph_paths=EXTRA_GRAPH_PATHS)
    # Reject duplicate metadata fields too, not only source records.
    if strict_json((root / "backfill_publication.json").read_text(encoding="utf-8-sig")) != publication:
        raise ValueError("Publication JSON did not round-trip exactly")
    contract = publication.get("grounded_contract", {})
    if contract.get("schema_version") != "grounded-lake-1":
        raise ValueError("Grounded publication requires grounded-lake-1 contract")
    validation_name = publisher.safe_name(contract.get("validation_file"), "validation_file")
    if not validation_name.endswith(".json"):
        raise ValueError("Grounded validation report must be JSON")
    validation_path = (root / validation_name).resolve()
    if validation_path.parent != root or not validation_path.is_file():
        raise ValueError("Grounded validation report missing or outside run")
    if publisher.file_sha256(validation_path) != contract.get("validation_sha256"):
        raise ValueError("Grounded validation report SHA mismatch")
    validation = strict_json(validation_path.read_text(encoding="utf-8-sig"))
    if validation.get("status") != "PASS" or validation.get("source_manifest_sha256") != publication["source_manifest_sha256"]:
        raise ValueError("Grounded semantic report is not PASS or refers to another source manifest")
    checks = validation.get("checks", {})
    if any(checks.get(name) is not True for name in REQUIRED_CHECKS):
        raise ValueError("Grounded semantic report is missing a required successful check")
    source_manifest = strict_json((root / "source_manifest.json").read_text(encoding="utf-8-sig"))
    if not isinstance(source_manifest, dict) or not source_manifest:
        raise ValueError("Frozen source manifest is empty")
    destinations = {(d["target_root"], d.get("dataset_path", "data")) for d in publication["datasets"]}
    if destinations != REQUIRED_DESTINATIONS:
        raise ValueError(f"Grounded destination set mismatch: missing={sorted(REQUIRED_DESTINATIONS-destinations)}, extra={sorted(destinations-REQUIRED_DESTINATIONS)}")
    verified = []
    for dataset in publication["datasets"]:
        _schema(dataset["schema"], dataset["name"])
        total = 0
        for part in dataset["files"]:
            source = publisher.verify_source(root, part)
            count = 0
            with source.open(encoding="utf-8-sig") as stream:
                for line_no, line in enumerate(stream, 1):
                    if not line.strip():
                        raise ValueError(f"Blank JSONL row: {source}:{line_no}")
                    row = strict_json(line)
                    label = f"{dataset['name']}:{line_no}"
                    exact_value(row, dataset["schema"], False, label)
                    for key in dataset["key_columns"]:
                        if row[key] is None or row[key] == "":
                            raise ValueError(f"NULL/empty key {key} at {label}")
                    count += 1
            if count != part["row_count"]:
                raise ValueError(f"Strict source row count mismatch: {source}")
            publisher.verify_source(root, part)
            total += count
        verified.append({"name": dataset["name"], "rows": total, "schema_sha256": publisher.object_sha(dataset["schema"])})
    if publisher.file_sha256(root / "backfill_publication.json") != digest:
        raise ValueError("Publication changed during preflight")
    if publisher.file_sha256(validation_path) != contract["validation_sha256"]:
        raise ValueError("Grounded validation report changed during preflight")
    if publisher.file_sha256(root / "source_manifest.json") != publication["source_manifest_sha256"]:
        raise ValueError("Source manifest changed during preflight")
    report = {"schema_version": "grounded-lake-preflight-1", "run_id": run_id, "status": "PASS",
              "publication_sha256": digest, "source_manifest_sha256": publication["source_manifest_sha256"],
              "semantic_validation_sha256": contract["validation_sha256"],
              "wrapper_sha256": publisher.file_sha256(Path(__file__)),
              "publisher_sha256": publisher.file_sha256(Path(publisher.__file__)),
              "helper_sha256": publisher.file_sha256(Path(__file__).with_name("publish_lake_recent.py")),
              "datasets": verified, "checks": {"exact_json_schema": True, "source_hashes": True,
              "source_counts": True, "nonnull_nonempty_keys": True, "required_semantic_report_bound": True}}
    return publication, report


def run(root, run_id, *, preflight_only=False, sandbox_only=False):
    root = Path(root).resolve()
    publication, report = validate_grounded(root, run_id)
    report_path = root / "grounded_preflight.json"
    publisher.save_json(report_path, report)
    if preflight_only:
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return report
    names = ["source_manifest.json", "backfill_publication.json", "finish.done.json",
             publication["grounded_contract"]["validation_file"], "grounded_preflight.json"]
    metadata = [{"name": name, "local_file": name, "sha256": publisher.file_sha256(root / name)} for name in names]
    return publisher.run(root, run_id, sandbox_only=sandbox_only,
                         extra_graph_paths=EXTRA_GRAPH_PATHS, commit_metadata=metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--sandbox-only", action="store_true")
    args = parser.parse_args()
    run(args.run_dir, args.run_id, preflight_only=args.preflight_only, sandbox_only=args.sandbox_only)


if __name__ == "__main__":
    main()
