"""Prepare the classifier pilot's next unseen 32 using the unchanged sampler.

Run only after the classifier policy is frozen. The immutable follow-up sampler
keeps its original seed, text cohort, target selection and 16/region quotas.
This wrapper adds a pinned exclusion inventory and a final evaluation receipt.
It never runs a classifier or reads review decisions, prices or predictions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import prepare_relevance_followup as parent

pilot = parent.pilot
HERE, ARTIFACTS = parent.HERE, parent.ARTIFACTS
PRIOR_STUDY = ARTIFACTS / "relevance_identity_v3_20260917_v1"
DEFAULT_OUT = ARTIFACTS / "relevance_classifier_v1_20260917_v1/evaluation_data"
PARENT_SHA256 = "5c6b0165daa62168ed9a1c872d67123c79ea7067caa9c06563b660c5c61c3232"
INVENTORY_SHA256 = "b6f5b9d8e0585cb06b7b1a1cd9ae0e7d4c742ca128e1e76b439061504e79c29a"
EXPECTED_COUNTS = {"base_reviews": 612, "general_reviews": 32, "context_reviews": 10,
                   "known_reviews": 654, "holdout": 11969}
BASE_REVIEW_PATHS = tuple(ARTIFACTS / relative for relative in (*parent.DEFAULT_REVIEWS,
    "local_relevance_followup_20260916_v2/new_inputs.jsonl",
    "relevance_two_stage_data_20260917_v1/human_inputs.jsonl",
    "relevance_policy_v2_data_20260917_v1/new_inputs.jsonl"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def row_sha(row):
    return parent.digest(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


def review_inventory(inventory_file, general_inputs, context_inputs, *, expected_sha256, expected_base_paths):
    """Read paths only; include the earlier human100 and every later exposed input."""
    inventory_file = Path(inventory_file).resolve()
    require(pilot.sha(inventory_file) == expected_sha256, "Prior review-path inventory checksum mismatch")
    values = json.loads(inventory_file.read_text(encoding="utf-8-sig"))
    require(isinstance(values, list) and values and all(isinstance(value, str) and value for value in values),
            "Review inventory must be a nonempty list of paths")
    base = [Path(value).resolve() for value in values]
    require(base == [Path(value).resolve() for value in expected_base_paths], "Prior review-path coverage changed")
    added = [Path(general_inputs).resolve(), Path(context_inputs).resolve()]
    combined = base + added
    require(len(set(combined)) == len(combined), "Duplicate review inventory paths")
    require(all(path.is_file() for path in combined), "Missing required review identity source")
    return base, added, combined


def base_identity_set(paths, legacy_articles):
    identities, positions = set(), set()
    for path in paths:
        for row in parent.review_rows(path):
            identity = parent.article_id(row)
            if identity is not None:
                require(isinstance(identity, str) and identity, "Invalid base review identity")
                identities.add(identity)
            else:
                position = row.get("article")
                require(type(position) is int and position >= 0, "Invalid numeric base review identity")
                positions.add(position)
    if positions:
        found = set()
        for position, row in enumerate(pilot.rows(legacy_articles)):
            if position in positions:
                identity = parent.article_id(row)
                require(isinstance(identity, str) and identity, "Unresolved numeric review identity")
                identities.add(identity)
                found.add(position)
        require(found == positions, "Unresolved numeric review positions")
    return identities


def frozen_input_ids(path, expected_count):
    manifest_path = path.with_name("manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    require(manifest.get("files", {}).get(path.name, {}).get("sha256") == pilot.sha(path),
            "Prior added input differs from its frozen manifest")
    values = list(parent.review_rows(path))
    require(len(values) == expected_count, "Unexpected prior added input count")
    identities = set()
    for row in values:
        require(set(row) == set(pilot.INPUT_FIELDS) and row["company_context"] is None,
                "Added review source must be original text-only inputs")
        identity = parent.article_id(row)
        require(isinstance(identity, str) and identity and identity not in identities, "Repeated or missing prior added input identity")
        identities.add(identity)
    return identities


def validate_outputs(out, summary, reviewed, holdout):
    require(summary["articles"] == summary["pairs"] == 32 and summary["regions"] == dict.fromkeys(parent.REGIONS, 16),
            "Parent sample size or regional balance changed")
    require(summary["review_packet_pairs"] == [16, 16]
            and summary["review_packet_regions"] == [dict.fromkeys(parent.REGIONS, 8)] * 2,
            "Parent review packet contract changed")
    require(summary["selection_seed"] == parent.SEED and summary["company_context"].startswith("null"),
            "Parent seed or context contract changed")
    for name, index in (("known_reviews", reviewed), ("holdout", holdout)):
        require(summary["exclusion_identity_coverage"][name]["ids"] == len(index["ids"]), "Parent exclusion count changed")
    require(summary["selected_known_review_overlap"] == summary["selected_holdout_overlap"] == 0,
            "Parent reports selected exclusion overlap")
    for name, info in summary["files"].items():
        require(pilot.sha(out / name) == info["sha256"], "Parent output checksum mismatch")
    inputs = list(pilot.rows(out / "new_inputs.jsonl"))
    first, second = (list(pilot.rows(out / name)) for name in ("review_inputs_01.jsonl", "review_inputs_02.jsonl"))
    provenance = list(pilot.rows(out / "provenance.jsonl"))
    require(len(inputs) == len(provenance) == 32 and len(first) == len(second) == 16 and first + second == inputs,
            "Review packets differ from ordered evaluation inputs")
    seen = set()
    for row, trace in zip(inputs, provenance):
        require(set(row) == set(pilot.INPUT_FIELDS) and row["company_context"] is None
                and row["source_split"] == "train" and row["case_kind"] == "real", "Invalid text-only evaluation input")
        require(row["article_id"] not in seen and 200 <= len(row["body"]) <= 6000
                and row["title"].strip() and "2020" <= row["published_at"][:4] <= "2022", "Invalid evaluation identity or cohort")
        seen.add(row["article_id"])
        require((row["article_id"], row["ticker"], row_sha(row))
                == (trace["article_id"], trace["ticker"], trace["input_sha256"]), "Input/provenance binding changed")
        bodies, urls = parent.identity_keys(row)
        bodies.update({trace["source_body_sha256"], trace["clean_body_sha256"]} - {None})
        require(not any(parent.match_kinds(row["article_id"], bodies, urls, index) for index in (reviewed, holdout)),
                "Evaluation output overlaps a known review or holdout")
    require(Counter(row["region"] for row in provenance) == Counter(dict.fromkeys(parent.REGIONS, 16)), "Provenance region mismatch")
    for packet in (provenance[:16], provenance[16:]):
        require(Counter(row["region"] for row in packet) == Counter(dict.fromkeys(parent.REGIONS, 8)), "Review packet region mismatch")
    return [{"article_id": row["article_id"], "ticker": row["ticker"], "input_sha256": row_sha(row)} for row in inputs]


def prepare_from_paths(out, *, source, companies, review_paths_file, general_inputs, context_inputs,
                       legacy_articles, legacy_metadata, fallback_source, expected_counts,
                       expected_inventory_sha256, expected_base_paths):
    """Explicit paths/counts support synthetic tests; production CLI uses fixed defaults."""
    require(pilot.sha(Path(parent.__file__)) == PARENT_SHA256, "Pinned parent sampler checksum mismatch")
    require(set(expected_counts) == set(EXPECTED_COUNTS)
            and all(type(value) is int and value > 0 for value in expected_counts.values())
            and expected_counts["base_reviews"] + expected_counts["general_reviews"] + expected_counts["context_reviews"]
            == expected_counts["known_reviews"], "Invalid exclusion-count contract")
    out = Path(out).resolve()
    require(not out.exists(), "Output must be a new directory; refusing to overwrite")
    base, added, reviews = review_inventory(review_paths_file, general_inputs, context_inputs,
        expected_sha256=expected_inventory_sha256, expected_base_paths=expected_base_paths)
    dependencies = [Path(source), Path(source).with_name("manifest.json"), Path(companies), Path(review_paths_file),
                    Path(legacy_articles), Path(legacy_metadata), *reviews, *(path.with_name("manifest.json") for path in added)]
    if fallback_source is not None:
        dependencies.append(Path(fallback_source))
    hashes = {str(path.resolve()): pilot.sha(path) for path in dependencies}
    code_paths = (Path(__file__), Path(parent.__file__), Path(pilot.__file__), HERE / "news_content.py",
                  HERE / "news_quality.py", HERE / "lake_news_normalize.py", HERE.parent / "ner/matcher.py")
    code_hashes = {str(path.resolve()): pilot.sha(path) for path in code_paths}
    base_ids = base_identity_set(base, legacy_articles)
    general_ids = frozen_input_ids(added[0], expected_counts["general_reviews"])
    context_ids = frozen_input_ids(added[1], expected_counts["context_reviews"])
    require(len(base_ids) == expected_counts["base_reviews"] and not (base_ids & general_ids or base_ids & context_ids or general_ids & context_ids),
            "Base/general/context prior identities overlap or have unexpected counts")
    reviewed, holdout, exclusion_info = parent.exclusion_indexes(reviews, legacy_articles, legacy_metadata, source, fallback_source)
    require(reviewed["ids"] == base_ids | general_ids | context_ids, "Hydrated review inventory differs from declared identities")
    for name, index in (("known_reviews", reviewed), ("holdout", holdout)):
        require(len(index["ids"]) == expected_counts[name], f"Unexpected {name} exclusion count before publication")
        require(not index["ids"] - index["body_resolved_ids"], f"Unresolved {name} exclusion bodies")

    def unchanged():
        for path, checksum in {**hashes, **code_hashes}.items():
            require(pilot.sha(path) == checksum, "Dependency changed during classifier evaluation preparation")

    unchanged()
    summary = parent.prepare(source=source, companies=companies, review_paths=reviews, legacy_articles=legacy_articles,
                             legacy_metadata=legacy_metadata, fallback_source=fallback_source, out=out)
    actual_manifest = json.loads((out / "manifest.json").read_text(encoding="utf8"))
    require(actual_manifest == summary, "Parent manifest differs from returned preparation summary")
    rows = validate_outputs(out, summary, reviewed, holdout)
    unchanged()
    receipt = {"schema": "cosmos-relevance-classifier-evaluation-1", "complete": True,
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(), "articles": 32, "regions": dict.fromkeys(parent.REGIONS, 16),
        "review_packet_pairs": [16, 16], "review_packet_regions": [dict.fromkeys(parent.REGIONS, 8)] * 2,
        "expected_exclusion_counts": expected_counts,
        "actual_exclusion_counts": {"base_reviews": len(base_ids), "general_reviews": len(general_ids), "context_reviews": len(context_ids),
                                    "known_reviews": len(reviewed["ids"]), "holdout": len(holdout["ids"])},
        "exclusion_counts_verified_before_parent_publication": True, "exclusion_sources": exclusion_info,
        "exclusion_identity_coverage": summary["exclusion_identity_coverage"],
        "parent_sampler_sha256": PARENT_SHA256, "parent_manifest_sha256": pilot.sha(out / "manifest.json"),
        "source_hashes": hashes, "code_hashes": code_hashes, "files": summary["files"], "rows": rows,
        "selection_seed": parent.SEED, "selection_seed_changed": False,
        "selection": "Same immutable parent hash seed; next unseen sample after adding all 654 prior review identities; 16 per region, cleaned full body 200..6000 characters",
        "prior_human100_included": (ARTIFACTS / "relevance_two_stage_data_20260917_v1/human_inputs.jsonl").resolve() in base,
        "prior_identity_study42_included": len(general_ids) == 32 and len(context_ids) == 10,
        "company_context": None, "model_inference_performed": False, "reference_decisions_generated": False,
        "review_decisions_used": False, "labels_or_returns_used": False, "semantic_topic_screening": False, "human_gold": False,
        "completion_marker": "evaluation_receipt.json; parent manifest alone is not completion of this wrapper",
        "limitations": ["A fresh local holdout relative to the enumerated 654 identities, not proof of no unknown external exposure.",
            "Same deterministic seed after expanded exclusions is not an independently randomized replication.",
            "Current company names do not certify historical identity; no registry context is inserted.",
            "The region-balanced length cohort is not a representative source-population accuracy sample."]}
    with (out / "evaluation_receipt.json").open("x", encoding="utf8", newline="\n") as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return receipt


def prepare(out=DEFAULT_OUT):
    return prepare_from_paths(out,
        source=ARTIFACTS / "news_scale_source_20260916_50k_final/training_candidates.jsonl",
        companies=HERE.parent / "ner/data/companies.csv", review_paths_file=PRIOR_STUDY / "review_paths.json",
        general_inputs=PRIOR_STUDY / "general_data/new_inputs.jsonl", context_inputs=PRIOR_STUDY / "context_data/inputs.jsonl",
        legacy_articles=ARTIFACTS / "news_impact_v2/articles.jsonl",
        legacy_metadata=ARTIFACTS / "news_quality_source_20260915/legacy_metadata.jsonl",
        fallback_source=ARTIFACTS / "news_quality_source_20260915/training_candidates.jsonl",
        expected_counts=dict(EXPECTED_COUNTS), expected_inventory_sha256=INVENTORY_SHA256, expected_base_paths=BASE_REVIEW_PATHS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    receipt = prepare(parser.parse_args().out)
    # No article identities, topics, text or judgments in coordinator output.
    print(json.dumps({key: receipt[key] for key in ("articles", "regions", "actual_exclusion_counts", "selection_seed", "files")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
