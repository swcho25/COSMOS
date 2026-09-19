"""Freeze prior-exposure and GNN holdout identity keys for the new KG pilot.

The older keyword-selected local exports are read ONLY to recover identities of
excluded articles. They are not a sampling population or a source of labels.
ID, normalized body and canonical URL keys are shared with the previous audits.
The new full-export sampler propagates these exclusions across duplicate groups.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import prepare_relevance_classifier_evaluation as prior

parent, pilot, ARTIFACTS = prior.parent, prior.pilot, prior.ARTIFACTS


def freeze(out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Choose a new exclusion output directory")
    _, _, reviews = prior.review_inventory(
        prior.PRIOR_STUDY / "review_paths.json",
        prior.PRIOR_STUDY / "general_data/new_inputs.jsonl",
        prior.PRIOR_STUDY / "context_data/inputs.jsonl",
        expected_sha256=prior.INVENTORY_SHA256, expected_base_paths=prior.BASE_REVIEW_PATHS)
    reviews += [ARTIFACTS / name for name in (
        "relevance_classifier_v1_20260917_v1/evaluation_data/new_inputs.jsonl",
        "relevance_rubric_v1_20260917_v1/evaluation_data/new_inputs.jsonl",
        "kg_news_selection_v1_20260917_v1/evaluation_data/new_inputs.jsonl")]
    legacy = ARTIFACTS / "news_impact_v2/articles.jsonl"
    metadata = ARTIFACTS / "news_quality_source_20260915/legacy_metadata.jsonl"
    source = ARTIFACTS / "news_scale_source_20260916_50k_final/training_candidates.jsonl"
    fallback = ARTIFACTS / "news_quality_source_20260915/training_candidates.jsonl"
    dependencies = [*reviews, legacy, metadata, source, fallback,
                    Path(__file__), Path(parent.__file__), Path(pilot.__file__), Path(prior.__file__)]
    hashes = {str(path.resolve()): pilot.sha(path) for path in dependencies}
    reviewed, holdout, info = parent.exclusion_indexes(reviews, legacy, metadata, source, fallback)
    if len(reviewed["ids"]) != 750 or len(holdout["ids"]) != 11969:
        raise ValueError("Prior exposure/holdout denominator changed")
    if any(index["ids"] - index["body_resolved_ids"] for index in (reviewed, holdout)):
        raise ValueError("Exclusion body hydration incomplete")
    if hashes != {str(path.resolve()): pilot.sha(path) for path in dependencies}:
        raise ValueError("Identity source changed while reading")
    out.mkdir(parents=True, exist_ok=False)
    target = out / "frozen_exclusion_keys.jsonl"
    with target.open("x", encoding="utf8", newline="\n") as stream:
        for label, index in (("known_review", reviewed), ("price_holdout", holdout)):
            for source_key, kind in (("ids", "id"), ("bodies", "body_sha256"), ("urls", "url")):
                for value in sorted(index[source_key]):
                    stream.write(json.dumps({"source": label, "kind": kind, "value": value},
                                            ensure_ascii=False, sort_keys=True) + "\n")
    receipt = {"schema": "kg-cascade-exclusions-1", "complete": True,
        "known_articles": 750, "price_holdout_articles": 11969,
        "keys": {label: {key: len(index[key]) for key in ("ids", "bodies", "urls", "body_resolved_ids", "url_resolved_ids")}
                 for label, index in (("known_review", reviewed), ("price_holdout", holdout))},
        "source_sha256": hashes, "identity_hydration": info,
        "file": {"path": target.name, "sha256": pilot.sha(target)},
        "review_decisions_used": False, "prices_used": False,
        "local_export_used_for_sampling": False,
        "stored_source_hash_namespace_keys_not_exported": True}
    (out / "manifest.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(json.dumps(freeze(parser.parse_args().out), ensure_ascii=False))
