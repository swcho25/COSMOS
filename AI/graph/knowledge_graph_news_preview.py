"""Publish a local, source-pinned article/company selection preview.

This exporter does not classify news or write to HDFS, a database, or a service.
Its unit is an article/company pair: selecting a story for one company never
selects it for every mentioned company. Only explicitly included pairs become
news -> company HAS_ECONOMIC_INFORMATION_ABOUT edges. Mere co-occurrence cannot
produce company -> company relations. Literal quotations attest source presence,
not semantic correctness; AI selections are never marked as human gold.

Inputs require article_id, ticker, company_name, published_at, title, body and
source_url. Extra input metadata is ignored. The input digest covers exactly
those seven fields, serialized as UTF-8 JSON with ensure_ascii=False,
sort_keys=True and separators=(',', ':'). Publication time and URL may be null;
publication time is preserved verbatim and is never substituted for event time.

Selections have exactly article_id, ticker, input_sha256, decision, relation,
evidence, reason, policy_version and model_provenance. include requires a direct
or indirect relation and a literal quote. exclude requires incidental and a
literal quote. review permits uncertainty and empty evidence. The caller is
responsible for the economic-relevance policy and classifier validation.

All input pairs require exactly one selection, and no extra selections are
allowed. The output directory must not exist. Files are written exclusively;
manifest.json is renamed into place only after all payloads are complete. An
interrupted export may leave an incomplete directory without a final manifest;
it must not be consumed or overwritten. This bounded in-memory preview is not
a replacement for a distributed full-history production loader.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any


VERSION = "knowledge-graph-news-preview-1"
INPUT_FIELDS = (
    "article_id", "ticker", "company_name", "published_at", "title", "body", "source_url"
)
SELECTION_FIELDS = {
    "article_id", "ticker", "input_sha256", "decision", "relation", "evidence",
    "reason", "policy_version", "model_provenance",
}
DECISIONS = {"include", "exclude", "review"}
RELATIONS = {"direct", "indirect", "incidental", "unclear"}
PREDICATE = "HAS_ECONOMIC_INFORMATION_ABOUT"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_input(row: dict) -> dict:
    require(isinstance(row, dict) and all(key in row for key in INPUT_FIELDS), "Input fields missing")
    for key in ("article_id", "ticker", "company_name", "title", "body"):
        require(isinstance(row[key], str), "Input must contain text: " + key)
    for key in ("article_id", "ticker", "company_name"):
        require(bool(row[key].strip()), "Input identity must not be empty: " + key)
    require(bool(row["title"].strip() or row["body"].strip()), "Article content is empty")
    for key in ("published_at", "source_url"):
        require(row[key] is None or isinstance(row[key], str), "Input must contain text or null: " + key)
    return {key: row[key] for key in INPUT_FIELDS}


def canonical_input_sha256(row: dict) -> str:
    return hashlib.sha256(canonical(validate_input(row)).encode("utf-8")).hexdigest()


def pair_key(row: dict) -> tuple[str, str]:
    return row["article_id"], row["ticker"]


def validate_selection(selection: dict, row: dict) -> dict:
    validate_input(row)
    require(isinstance(selection, dict) and set(selection) == SELECTION_FIELDS, "Selection field contract differs")
    for key in ("article_id", "ticker", "input_sha256", "reason", "policy_version"):
        require(isinstance(selection[key], str) and bool(selection[key].strip()), "Selection text missing: " + key)
    require(pair_key(selection) == pair_key(row), "Selection identity differs from input")
    require(selection["input_sha256"] == canonical_input_sha256(row), "Selection input hash differs")
    require(isinstance(selection["decision"], str) and selection["decision"] in DECISIONS, "Unknown decision")
    require(isinstance(selection["relation"], str) and selection["relation"] in RELATIONS, "Unknown relation")
    require(isinstance(selection["model_provenance"], dict) and bool(selection["model_provenance"]), "Model provenance must be a nonempty object")
    canonical(selection["model_provenance"])
    require(isinstance(selection["evidence"], list), "Evidence must be a list")
    seen = set()
    for quote in selection["evidence"]:
        require(isinstance(quote, dict) and set(quote) == {"source", "quote"}, "Quote field contract differs")
        require(isinstance(quote["source"], str) and quote["source"] in ("title", "body"), "Unknown quote source")
        require(isinstance(quote["quote"], str) and bool(quote["quote"].strip()), "Quote must be nonempty text")
        require(quote["quote"] in row[quote["source"]], "Quote is not literal input evidence")
        identity = quote["source"], quote["quote"]
        require(identity not in seen, "Duplicate evidence quote")
        seen.add(identity)
    if selection["decision"] == "include":
        require(selection["relation"] in ("direct", "indirect"), "Include requires direct or indirect economic relevance")
        require(bool(selection["evidence"]), "Include requires literal evidence")
    elif selection["decision"] == "exclude":
        require(selection["relation"] == "incidental", "Exclude requires incidental mention")
        require(bool(selection["evidence"]), "Exclude requires literal evidence")
    return json.loads(canonical(selection))


def _unique(rows: list[dict], kind: str) -> dict[tuple[str, str], dict]:
    indexed = {}
    for row in rows:
        require(isinstance(row, dict), kind + " must contain JSON objects")
        require(all(isinstance(row.get(key), str) and row[key].strip() for key in ("article_id", "ticker")), kind + " identity missing")
        key = pair_key(row)
        require(key not in indexed, "Duplicate " + kind + " pair: " + repr(key))
        indexed[key] = row
    return indexed


def prepare_preview(inputs: list[dict], selections: list[dict]) -> tuple[dict[str, list[dict]], dict]:
    """Validate the entire pair set before returning any selected graph records."""
    rows = [validate_input(row) for row in inputs]
    indexed = _unique(rows, "input")
    require(bool(indexed), "At least one input pair is required")
    decisions = _unique(selections, "selection")
    require(set(indexed) == set(decisions), "Selection coverage differs: every input pair needs exactly one selection")
    articles = {}
    for row in rows:
        article = {key: row[key] for key in ("article_id", "title", "body", "published_at", "source_url")}
        if row["article_id"] in articles:
            require(articles[row["article_id"]] == article, "Conflicting source content for the same article_id")
        articles[row["article_id"]] = article

    payloads = {name: [] for name in (
        "news_nodes.jsonl", "company_nodes.jsonl", "selected_links.jsonl",
        "selection_audit.jsonl", "review.jsonl", "excluded.jsonl",
    )}
    selected_articles = set()
    selected_companies: dict[str, set[str]] = {}
    counts: Counter = Counter()
    relations: Counter = Counter()
    policies = set()
    for key in sorted(indexed):
        row = indexed[key]
        selection = validate_selection(decisions[key], row)
        decision = selection["decision"]
        counts[decision] += 1
        relations[selection["relation"]] += 1
        policies.add(selection["policy_version"])
        audit = dict(selection, human_gold=False, ready_for_gnn_training=False, production_approved=False)
        payloads["selection_audit.jsonl"].append(audit)
        if decision != "include":
            payloads["excluded.jsonl" if decision == "exclude" else "review.jsonl"].append(audit)
            continue
        selected_articles.add(row["article_id"])
        selected_companies.setdefault(row["ticker"], set()).add(row["company_name"])
        payloads["selected_links.jsonl"].append({
            **audit,
            "edge_id": "economic-info:" + hashlib.sha256(canonical(list(key)).encode("utf-8")).hexdigest(),
            "source_node": "news:" + row["article_id"],
            "target_node": "company:" + row["ticker"],
            "predicate": PREDICATE,
            "published_at": row["published_at"],
            "event_date": None,
            "event_date_status": "not_extracted",
            "stock_price_effect": "not_established",
        })
    for article_id in sorted(selected_articles):
        payloads["news_nodes.jsonl"].append({
            **articles[article_id], "node_id": "news:" + article_id, "node_type": "news",
            "event_date": None, "event_date_status": "not_extracted",
        })
    for ticker, names in sorted(selected_companies.items()):
        payloads["company_nodes.jsonl"].append({
            "node_id": "company:" + ticker, "node_type": "company", "ticker": ticker,
            "company_names": sorted(names), "historical_security_mapping_verified": False,
        })
    summary = {
        "schema": VERSION,
        "input_pairs": len(rows),
        "input_articles": len(articles),
        "decision_counts": {key: counts[key] for key in sorted(DECISIONS)},
        "relation_counts": {key: relations[key] for key in sorted(RELATIONS)},
        "selected_articles": len(selected_articles),
        "selected_companies": len(selected_companies),
        "selected_edges": counts["include"],
        "company_company_edges": 0,
        "policy_versions": sorted(policies),
        "metrics_are": "descriptive_counts_not_accuracy",
        "human_gold": False,
        "ready_for_gnn_training": False,
        "production_approved": False,
        "publication_time_is_event_time": False,
        "selection_validation": "schema_source_hash_literal_quote_and_pair_coverage_only",
    }
    return payloads, summary


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON object key: " + key)
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON constant: " + value)


def read_jsonl(path: Path | str) -> list[dict]:
    rows = []
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            require(bool(line.strip()), "Blank JSONL record at line " + str(number))
            row = json.loads(line, object_pairs_hook=_no_duplicate_keys, parse_constant=_invalid_constant)
            require(isinstance(row, dict), "JSONL record must be an object at line " + str(number))
            rows.append(row)
    return rows


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_preview(inputs: Path | str, selections: Path | str, out: Path | str) -> dict:
    """Export a new local preview directory; the final manifest is the receipt."""
    input_path, selection_path, output = Path(inputs), Path(selections), Path(out)
    require(not output.exists(), "Output directory already exists; refusing overwrite")
    original_hashes = {"inputs_sha256": file_sha256(input_path), "selections_sha256": file_sha256(selection_path)}
    payloads, summary = prepare_preview(read_jsonl(input_path), read_jsonl(selection_path))
    require(original_hashes == {"inputs_sha256": file_sha256(input_path), "selections_sha256": file_sha256(selection_path)}, "Source files changed during validation")
    output.mkdir(parents=True, exist_ok=False)
    files = {}
    for name, rows in payloads.items():
        path = output / name
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(canonical(row) + "\n")
        files[name] = {"records": len(rows), "sha256": file_sha256(path), "bytes": path.stat().st_size}
    manifest = {**summary, **original_hashes, "complete": True, "files": files}
    temporary = output / "manifest.json.pending"
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
    temporary.rename(output / "manifest.json")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--selections", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = build_preview(args.inputs, args.selections, args.out)
    print(json.dumps({key: result[key] for key in ("complete", "input_pairs", "decision_counts", "selected_edges")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
