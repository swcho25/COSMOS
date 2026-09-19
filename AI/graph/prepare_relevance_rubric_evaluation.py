"""Prepare another unseen 32 after freezing the relevance rubric and plan.

The unchanged parent sampler retains its seed, full cleaned-body cohort and
region quotas. This wrapper adds the classifier pilot's 32 inputs to the 654
prior identities. It performs no model inference or reference adjudication.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import prepare_relevance_classifier_evaluation as prior

parent, pilot = prior.parent, prior.pilot
HERE, ARTIFACTS = prior.HERE, prior.ARTIFACTS
DEFAULT_OUT = ARTIFACTS / "relevance_rubric_v1_20260917_v1/evaluation_data"
PRIOR_WRAPPER_SHA256 = "56875fd4452973185f8fdf1c5404a34a02af59e8588f1fc6d803bf6c423fbffa"
EXPECTED_COUNTS = {"base_reviews": 612, "general_reviews": 32, "context_reviews": 10,
                   "classifier_reviews": 32, "known_reviews": 686, "holdout": 11969}
require = prior.require


def prior_classifier_ids(path, expected_count, expected_prior_counts):
    """Verify the previous input, its parent manifest and its final receipt."""
    path = Path(path).resolve()
    identities = prior.frozen_input_ids(path, expected_count)
    receipt_path = path.with_name("evaluation_receipt.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    require(receipt.get("schema") == "cosmos-relevance-classifier-evaluation-1" and receipt.get("complete") is True,
            "Prior classifier evaluation has no complete receipt")
    require(receipt.get("parent_sampler_sha256") == prior.PARENT_SHA256
            and receipt.get("parent_manifest_sha256") == pilot.sha(path.with_name("manifest.json"))
            and receipt.get("files", {}).get(path.name, {}).get("sha256") == pilot.sha(path),
            "Prior classifier receipt/manifest/input checksum mismatch")
    require(receipt.get("actual_exclusion_counts") == expected_prior_counts
            and receipt.get("exclusion_counts_verified_before_parent_publication") is True,
            "Prior classifier exclusion receipt differs from expected inventory")
    rows = list(pilot.rows(path))
    bindings = [{"article_id": row["article_id"], "ticker": row["ticker"], "input_sha256": prior.row_sha(row)} for row in rows]
    require(receipt.get("rows") == bindings, "Prior classifier canonical row hashes differ from receipt")
    return identities


def prepare_from_paths(out, *, source, companies, review_paths_file, general_inputs, context_inputs,
                       classifier_inputs, legacy_articles, legacy_metadata, fallback_source,
                       expected_counts, expected_inventory_sha256, expected_base_paths):
    """Explicit fixture arguments; production entry point supplies fixed contracts."""
    require(pilot.sha(Path(prior.__file__)) == PRIOR_WRAPPER_SHA256, "Pinned prior wrapper checksum mismatch")
    require(pilot.sha(Path(parent.__file__)) == prior.PARENT_SHA256, "Pinned parent sampler checksum mismatch")
    require(set(expected_counts) == set(EXPECTED_COUNTS)
            and all(type(value) is int and value > 0 for value in expected_counts.values())
            and sum(expected_counts[key] for key in ("base_reviews", "general_reviews", "context_reviews", "classifier_reviews"))
            == expected_counts["known_reviews"], "Invalid rubric exclusion-count contract")
    out, classifier_inputs = Path(out).resolve(), Path(classifier_inputs).resolve()
    require(not out.exists(), "Output must be a new directory; refusing to overwrite")
    base, added, earlier_reviews = prior.review_inventory(review_paths_file, general_inputs, context_inputs,
        expected_sha256=expected_inventory_sha256, expected_base_paths=expected_base_paths)
    require(classifier_inputs.is_file() and classifier_inputs not in earlier_reviews, "Missing or duplicate prior classifier inputs")
    reviews = [*earlier_reviews, classifier_inputs]
    dependencies = [Path(source), Path(source).with_name("manifest.json"), Path(companies), Path(review_paths_file),
        Path(legacy_articles), Path(legacy_metadata), *reviews,
        *(path.with_name("manifest.json") for path in [*added, classifier_inputs]),
        classifier_inputs.with_name("evaluation_receipt.json")]
    if fallback_source is not None:
        dependencies.append(Path(fallback_source))
    hashes = {str(path.resolve()): pilot.sha(path) for path in dependencies}
    code_paths = (Path(__file__), Path(prior.__file__), Path(parent.__file__), Path(pilot.__file__),
                  HERE / "news_content.py", HERE / "news_quality.py", HERE / "lake_news_normalize.py", HERE.parent / "ner/matcher.py")
    code_hashes = {str(path.resolve()): pilot.sha(path) for path in code_paths}
    identity_sets = {"base_reviews": prior.base_identity_set(base, legacy_articles),
                     "general_reviews": prior.frozen_input_ids(added[0], expected_counts["general_reviews"]),
                     "context_reviews": prior.frozen_input_ids(added[1], expected_counts["context_reviews"])}
    previous_counts = {key: expected_counts[key] for key in prior.EXPECTED_COUNTS}
    previous_counts["known_reviews"] -= expected_counts["classifier_reviews"]
    identity_sets["classifier_reviews"] = prior_classifier_ids(classifier_inputs, expected_counts["classifier_reviews"], previous_counts)
    all_ids = set()
    for name, identities in identity_sets.items():
        require(len(identities) == expected_counts[name] and not all_ids & identities,
                "Prior identity partitions overlap or have unexpected counts")
        all_ids.update(identities)
    reviewed, holdout, exclusion_info = parent.exclusion_indexes(reviews, legacy_articles, legacy_metadata, source, fallback_source)
    require(reviewed["ids"] == all_ids, "Hydrated review identities differ from the declared prior inventory")
    for name, index in (("known_reviews", reviewed), ("holdout", holdout)):
        require(len(index["ids"]) == expected_counts[name], f"Unexpected {name} exclusion count before publication")
        require(not index["ids"] - index["body_resolved_ids"], f"Unresolved {name} exclusion bodies")

    def unchanged():
        for path, checksum in {**hashes, **code_hashes}.items():
            require(pilot.sha(path) == checksum, "Dependency changed during rubric evaluation preparation")

    unchanged()
    summary = parent.prepare(source=source, companies=companies, review_paths=reviews, legacy_articles=legacy_articles,
                             legacy_metadata=legacy_metadata, fallback_source=fallback_source, out=out)
    require(json.loads((out / "manifest.json").read_text(encoding="utf8")) == summary,
            "Parent manifest differs from returned summary")
    rows = prior.validate_outputs(out, summary, reviewed, holdout)
    unchanged()
    # The parent files are checked once more immediately before the wrapper's
    # completion marker; the parent's manifest alone is not wrapper completion.
    for filename, info in summary["files"].items():
        require(pilot.sha(out / filename) == info["sha256"], "Output changed before final rubric receipt")
    receipt = {"schema": "cosmos-relevance-rubric-evaluation-1", "complete": True,
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(), "articles": 32,
        "regions": dict.fromkeys(parent.REGIONS, 16), "review_packet_pairs": [16, 16],
        "review_packet_regions": [dict.fromkeys(parent.REGIONS, 8)] * 2,
        "expected_exclusion_counts": expected_counts,
        "actual_exclusion_counts": {**{name: len(values) for name, values in identity_sets.items()},
                                    "known_reviews": len(reviewed["ids"]), "holdout": len(holdout["ids"])},
        "exclusion_counts_verified_before_parent_publication": True, "exclusion_sources": exclusion_info,
        "exclusion_identity_coverage": summary["exclusion_identity_coverage"],
        "prior_wrapper_sha256": PRIOR_WRAPPER_SHA256, "parent_sampler_sha256": prior.PARENT_SHA256,
        "parent_manifest_sha256": pilot.sha(out / "manifest.json"), "source_hashes": hashes, "code_hashes": code_hashes,
        "files": summary["files"], "rows": rows, "selection_seed": parent.SEED, "selection_seed_changed": False,
        "selection": f"Same immutable parent hash seed, next unseen sample after {len(reviewed['ids'])} prior identities; 16 per region; full minimally cleaned body 200..6000 characters",
        "prior_classifier_input_manifest_receipt_and_row_hashes_verified": True,
        "company_context": None, "model_inference_performed": False, "reference_decisions_generated": False,
        "review_decisions_used": False, "labels_or_returns_used": False, "semantic_topic_screening": False, "human_gold": False,
        "completion_marker": "evaluation_receipt.json",
        "limitations": ["Fresh only against the enumerated local review and legacy holdout identities; external exposure is unknown.",
            "The unchanged deterministic seed with expanded exclusions is not an independently randomized replication.",
            "Double-independent AI review, if subsequently performed, is not human gold and is not produced by this sampler.",
            "Current company names do not verify historical identity; no company context is inserted.",
            "The balanced length cohort does not represent source-population accuracy."]}
    with (out / "evaluation_receipt.json").open("x", encoding="utf8", newline="\n") as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return receipt


def prepare(out=DEFAULT_OUT):
    return prepare_from_paths(out,
        source=ARTIFACTS / "news_scale_source_20260916_50k_final/training_candidates.jsonl",
        companies=HERE.parent / "ner/data/companies.csv", review_paths_file=prior.PRIOR_STUDY / "review_paths.json",
        general_inputs=prior.PRIOR_STUDY / "general_data/new_inputs.jsonl", context_inputs=prior.PRIOR_STUDY / "context_data/inputs.jsonl",
        classifier_inputs=ARTIFACTS / "relevance_classifier_v1_20260917_v1/evaluation_data/new_inputs.jsonl",
        legacy_articles=ARTIFACTS / "news_impact_v2/articles.jsonl",
        legacy_metadata=ARTIFACTS / "news_quality_source_20260915/legacy_metadata.jsonl",
        fallback_source=ARTIFACTS / "news_quality_source_20260915/training_candidates.jsonl",
        expected_counts=dict(EXPECTED_COUNTS), expected_inventory_sha256=prior.INVENTORY_SHA256,
        expected_base_paths=prior.BASE_REVIEW_PATHS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    result = prepare(parser.parse_args().out)
    print(json.dumps({key: result[key] for key in ("articles", "regions", "actual_exclusion_counts", "selection_seed", "files")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
