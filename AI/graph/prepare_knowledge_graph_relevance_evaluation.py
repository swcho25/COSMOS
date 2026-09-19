"""Prepare unseen article/company inputs for economic knowledge-graph selection.

Run only after the graph-selector policy and experiment plan are frozen. The
original sampler remains unchanged: the same deterministic seed, 2020..2022,
full minimally cleaned body 200..6000 characters, and 16 articles per region.
This wrapper adds the rubric experiment's 32 exposed inputs to the prior 686,
verifies both preceding receipts, and excludes all 718 plus price holdouts.
It does not read review verdicts, infer relevance, or alter prior artifacts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import prepare_relevance_rubric_evaluation as rubric

prior, parent, pilot = rubric.prior, rubric.parent, rubric.pilot
HERE, ARTIFACTS, require = rubric.HERE, rubric.ARTIFACTS, rubric.require
DEFAULT_OUT = ARTIFACTS / "kg_news_selection_v1_20260917_v1/evaluation_data"
RUBRIC_WRAPPER_SHA256 = "3f6feee221f6d8ddf09e58dfc198b4bcc34dd61a4e0cc74ee275761e46789205"
EXPECTED_COUNTS = {"base_reviews": 612, "general_reviews": 32, "context_reviews": 10,
                   "classifier_reviews": 32, "rubric_reviews": 32, "known_reviews": 718, "holdout": 11969}


def prior_rubric_ids(path, expected_count, expected_prior_counts):
    """Require original text-only inputs bound to a complete rubric receipt."""
    path = Path(path).resolve()
    identities = prior.frozen_input_ids(path, expected_count)
    receipt = json.loads(path.with_name("evaluation_receipt.json").read_text(encoding="utf-8-sig"))
    require(receipt.get("schema") == "cosmos-relevance-rubric-evaluation-1" and receipt.get("complete") is True,
            "Prior rubric evaluation has no complete receipt")
    require(receipt.get("prior_wrapper_sha256") == rubric.PRIOR_WRAPPER_SHA256
            and receipt.get("parent_sampler_sha256") == prior.PARENT_SHA256
            and receipt.get("parent_manifest_sha256") == pilot.sha(path.with_name("manifest.json"))
            and receipt.get("files", {}).get(path.name, {}).get("sha256") == pilot.sha(path),
            "Prior rubric receipt/manifest/input checksum mismatch")
    require(receipt.get("actual_exclusion_counts") == expected_prior_counts
            and receipt.get("expected_exclusion_counts") == expected_prior_counts
            and receipt.get("exclusion_counts_verified_before_parent_publication") is True
            and receipt.get("prior_classifier_input_manifest_receipt_and_row_hashes_verified") is True,
            "Prior rubric exclusion receipt differs from expected inventory")
    rows = list(pilot.rows(path))
    bindings = [{"article_id": row["article_id"], "ticker": row["ticker"], "input_sha256": prior.row_sha(row)} for row in rows]
    require(receipt.get("rows") == bindings, "Prior rubric canonical row hashes differ from receipt")
    return identities


def prepare_from_paths(out, *, source, companies, review_paths_file, general_inputs, context_inputs,
                       classifier_inputs, rubric_inputs, legacy_articles, legacy_metadata, fallback_source,
                       expected_counts, expected_inventory_sha256, expected_base_paths):
    """Synthetic-test path injection; production supplies the frozen inventory."""
    require(pilot.sha(Path(rubric.__file__)) == RUBRIC_WRAPPER_SHA256, "Pinned rubric wrapper checksum mismatch")
    require(pilot.sha(Path(prior.__file__)) == rubric.PRIOR_WRAPPER_SHA256, "Pinned classifier wrapper checksum mismatch")
    require(pilot.sha(Path(parent.__file__)) == prior.PARENT_SHA256, "Pinned parent sampler checksum mismatch")
    partition_keys = ("base_reviews", "general_reviews", "context_reviews", "classifier_reviews", "rubric_reviews")
    require(set(expected_counts) == set(EXPECTED_COUNTS)
            and all(type(value) is int and value > 0 for value in expected_counts.values())
            and sum(expected_counts[key] for key in partition_keys) == expected_counts["known_reviews"],
            "Invalid graph-selection exclusion-count contract")
    out, classifier_inputs, rubric_inputs = map(lambda value: Path(value).resolve(), (out, classifier_inputs, rubric_inputs))
    require(not out.exists(), "Output must be a new directory; refusing to overwrite")
    base, added, earlier_reviews = prior.review_inventory(review_paths_file, general_inputs, context_inputs,
        expected_sha256=expected_inventory_sha256, expected_base_paths=expected_base_paths)
    reviews = [*earlier_reviews, classifier_inputs, rubric_inputs]
    require(len(set(reviews)) == len(reviews) and all(path.is_file() for path in reviews),
            "Missing or duplicate classifier/rubric input source")
    dependencies = [Path(source), Path(source).with_name("manifest.json"), Path(companies), Path(review_paths_file),
        Path(legacy_articles), Path(legacy_metadata), *reviews,
        *(path.with_name("manifest.json") for path in [*added, classifier_inputs, rubric_inputs]),
        *(path.with_name("evaluation_receipt.json") for path in (classifier_inputs, rubric_inputs))]
    if fallback_source is not None:
        dependencies.append(Path(fallback_source))
    hashes = {str(path.resolve()): pilot.sha(path) for path in dependencies}
    code_paths = (Path(__file__), Path(rubric.__file__), Path(prior.__file__), Path(parent.__file__), Path(pilot.__file__),
                  HERE / "news_content.py", HERE / "news_quality.py", HERE / "lake_news_normalize.py", HERE.parent / "ner/matcher.py")
    code_hashes = {str(path.resolve()): pilot.sha(path) for path in code_paths}
    identity_sets = {"base_reviews": prior.base_identity_set(base, legacy_articles),
                     "general_reviews": prior.frozen_input_ids(added[0], expected_counts["general_reviews"]),
                     "context_reviews": prior.frozen_input_ids(added[1], expected_counts["context_reviews"])}
    classifier_counts = {key: expected_counts[key] for key in prior.EXPECTED_COUNTS}
    classifier_counts["known_reviews"] -= expected_counts["classifier_reviews"] + expected_counts["rubric_reviews"]
    identity_sets["classifier_reviews"] = rubric.prior_classifier_ids(classifier_inputs, expected_counts["classifier_reviews"], classifier_counts)
    rubric_counts = {key: expected_counts[key] for key in rubric.EXPECTED_COUNTS}
    rubric_counts["known_reviews"] -= expected_counts["rubric_reviews"]
    identity_sets["rubric_reviews"] = prior_rubric_ids(rubric_inputs, expected_counts["rubric_reviews"], rubric_counts)
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
            require(pilot.sha(path) == checksum, "Dependency changed during graph-selection evaluation preparation")

    unchanged()
    summary = parent.prepare(source=source, companies=companies, review_paths=reviews, legacy_articles=legacy_articles,
                             legacy_metadata=legacy_metadata, fallback_source=fallback_source, out=out)
    require(json.loads((out / "manifest.json").read_text(encoding="utf8")) == summary,
            "Parent manifest differs from returned summary")
    rows = prior.validate_outputs(out, summary, reviewed, holdout)
    unchanged()
    for filename, info in summary["files"].items():
        require(pilot.sha(out / filename) == info["sha256"], "Output changed before final graph-selection receipt")
    receipt = {"schema": "cosmos-knowledge-graph-relevance-evaluation-1", "complete": True,
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(), "articles": 32,
        "regions": dict.fromkeys(parent.REGIONS, 16), "review_packet_pairs": [16, 16],
        "review_packet_regions": [dict.fromkeys(parent.REGIONS, 8)] * 2,
        "expected_exclusion_counts": expected_counts,
        "actual_exclusion_counts": {**{name: len(values) for name, values in identity_sets.items()},
                                    "known_reviews": len(reviewed["ids"]), "holdout": len(holdout["ids"])},
        "exclusion_counts_verified_before_parent_publication": True, "exclusion_sources": exclusion_info,
        "exclusion_identity_coverage": summary["exclusion_identity_coverage"],
        "rubric_wrapper_sha256": RUBRIC_WRAPPER_SHA256, "classifier_wrapper_sha256": rubric.PRIOR_WRAPPER_SHA256,
        "parent_sampler_sha256": prior.PARENT_SHA256, "parent_manifest_sha256": pilot.sha(out / "manifest.json"),
        "source_hashes": hashes, "code_hashes": code_hashes, "files": summary["files"], "rows": rows,
        "selection_seed": parent.SEED, "selection_seed_changed": False,
        "selection": f"Same immutable parent hash seed; next unseen sample after {len(reviewed['ids'])} prior identities; 16 per region; full minimally cleaned body 200..6000 characters",
        "prior_classifier_and_rubric_input_manifest_receipt_and_row_hashes_verified": True,
        "company_context": None, "model_inference_performed": False, "reference_decisions_generated": False,
        "review_decisions_used": False, "labels_or_returns_used": False, "semantic_topic_screening": False, "human_gold": False,
        "completion_marker": "evaluation_receipt.json",
        "limitations": ["Fresh only against enumerated local review and legacy holdout identities; external exposure is unknown.",
            "An unchanged deterministic seed with expanded exclusions is not an independently randomized replication.",
            "Subsequent AI references and inter-reviewer agreement are not human gold or source-population accuracy.",
            "Current company names do not verify historical identity; no company context is inserted.",
            "The region-balanced length cohort does not represent the full source population."]}
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
        rubric_inputs=ARTIFACTS / "relevance_rubric_v1_20260917_v1/evaluation_data/new_inputs.jsonl",
        legacy_articles=ARTIFACTS / "news_impact_v2/articles.jsonl",
        legacy_metadata=ARTIFACTS / "news_quality_source_20260915/legacy_metadata.jsonl",
        fallback_source=ARTIFACTS / "news_quality_source_20260915/training_candidates.jsonl",
        expected_counts=dict(EXPECTED_COUNTS), expected_inventory_sha256=prior.INVENTORY_SHA256,
        expected_base_paths=prior.BASE_REVIEW_PATHS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    result = prepare(parser.parse_args().out)
    # Keep article text and target identities out of coordinator output.
    print(json.dumps({key: result[key] for key in ("articles", "regions", "actual_exclusion_counts", "selection_seed", "files")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
