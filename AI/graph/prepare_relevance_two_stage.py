"""Prepare 100 untouched human-review inputs and a frozen 32-pair GPU subset.

This prepares text only; it does not run a selector, infer reference decisions,
load models, or read prices/outcomes. The earlier sampling code and datasets are
immutable dependencies. Hash sampling uses neither event keywords nor labels.
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import json
from pathlib import Path

import prepare_relevance_followup as previous

pilot = previous.pilot
HERE, ARTIFACTS, REGIONS = previous.HERE, previous.ARTIFACTS, previous.REGIONS
SEED = "relevance-two-stage-data-20260917-v1:"
HUMAN_FIELDS = {"human_label": None, "human_time_status": None, "human_selection": None,
                "human_evidence_sentences": [], "human_reason": "", "human_reviewer": None,
                "human_reviewed_at": None, "human_reviewed": False, "review_status": "pending"}


def rank(identity, purpose="article"):
    return previous.digest(purpose + ":" + SEED + identity)


def target(row, names):
    values = {item.get("ticker") for item in row.get("companies") or [] if isinstance(item, dict)}
    if not values or not all(isinstance(value, str) and value in names for value in values):
        return None
    return min(values, key=lambda value: (rank(row["record_id"] + ":" + value, "target"), value))


def collect(source, names, reviewed, holdout):
    counts, exclusions, matches, eligible = Counter(), Counter(), Counter(), Counter()
    seen, candidates = set(), []
    for line, offset, length, row in previous.source_rows(source):
        counts["source_rows"] += 1
        identity = row["record_id"]
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("Missing or repeated source article identity")
        seen.add(identity)
        try:
            date = dt.date.fromisoformat(str(row["published_date"])[:10])
        except ValueError:
            exclusions["invalid_publication_date"] += 1
            continue
        if not 2020 <= date.year <= 2022:
            exclusions["outside_2020_2022"] += 1
            continue
        counts["training_period_rows"] += 1
        if row["split"] not in (None, "train"):
            exclusions["explicit_non_train_split"] += 1
            continue
        if row["region"] not in REGIONS:
            exclusions["unsupported_region"] += 1
            continue
        clean = pilot.minimal_clean(row)
        bodies = {pilot.text_hash(row["body"]), pilot.text_hash(clean["body"])} - {None}
        urls, stored = pilot.url_keys(row), previous.source_hash_keys(row)
        old = previous.match_kinds(identity, bodies, urls, reviewed, stored)
        held = previous.match_kinds(identity, bodies, urls, holdout, stored)
        for prefix, kinds in (("prior_review", old), ("holdout", held)):
            matches.update(prefix + "_" + kind for kind in kinds)
        if old or held:
            exclusions["prior_review_and_or_holdout_overlap"] += 1
            continue
        if not clean["title"].strip():
            exclusions["empty_clean_title"] += 1
            continue
        if not 200 <= len(clean["body"]) <= 6000:
            exclusions["outside_clean_body_length_cohort_200_6000"] += 1
            continue
        ticker = target(row, names)
        if ticker is None:
            exclusions["missing_or_unknown_source_company"] += 1
            continue
        try:
            previous.make_input(row, clean, ticker, names)
        except ValueError:
            exclusions["publication_timestamp_outside_training_years"] += 1
            continue
        eligible[row["region"]] += 1
        candidates.append({"article_id": identity, "ticker": ticker, "region": row["region"],
                           "rank": rank(identity), "bodies": bodies, "urls": urls, "source_hashes": stored,
                           "line": line, "offset": offset, "length": length})
    return candidates, {"source_counts": dict(counts), "eligible_by_region": dict(eligible),
                        "first_exclusion_reason_counts": dict(exclusions), "overlap_match_counts_nonexclusive": dict(matches)}


def select_unique(candidates):
    selected, quotas, duplicate_count = [], Counter(), 0
    seen = {key: set() for key in ("bodies", "urls", "source_hashes")}
    for item in sorted(candidates, key=lambda row: (row["rank"], row["article_id"])):
        if quotas[item["region"]] == 50:
            continue
        if any(item[key] & seen[key] for key in seen):
            duplicate_count += 1
            continue
        selected.append(item)
        quotas[item["region"]] += 1
        for key in seen:
            seen[key].update(item[key])
        if len(selected) == 100:
            break
    if quotas != Counter(dict.fromkeys(REGIONS, 50)):
        raise ValueError("Fewer than 50 unique eligible articles in a region")
    return sorted(selected, key=lambda row: rank(row["article_id"], "human-presentation")), duplicate_count


def evaluation_subset(selected):
    # Membership is fixed by a separate hash among the already selected 100.
    groups = {region: sorted((row for row in selected if row["region"] == region),
                            key=lambda row: (rank(row["article_id"], "evaluation"), row["article_id"]))[:16]
              for region in REGIONS}
    if any(len(group) != 16 for group in groups.values()):
        raise ValueError("Evaluation subset requires 16 articles from each region")
    ordered = []
    for packet in range(2):
        rows = [row for region in REGIONS for row in groups[region][packet * 8:(packet + 1) * 8]]
        ordered.extend(sorted(rows, key=lambda row: rank(row["article_id"], "review-presentation")))
    return ordered


def prepare(*, source, companies, review_paths, prior_followup, legacy_articles, legacy_metadata, out, fallback_source=None):
    source, out, prior_followup = Path(source).resolve(), Path(out).resolve(), Path(prior_followup).resolve()
    if out.exists():
        raise FileExistsError("Choose a new output directory; existing data is immutable")
    source_manifest, prior_manifest = source.with_name("manifest.json"), prior_followup.with_name("manifest.json")
    export = json.loads(source_manifest.read_text(encoding="utf8"))
    old = json.loads(prior_manifest.read_text(encoding="utf8"))
    if export.get("complete") is not True or type(export.get("training_candidates")) is not int:
        raise ValueError("Source export must be complete with a row count")
    prior_rows = list(pilot.rows(prior_followup))
    if (old.get("pairs") != 32 or len(prior_rows) != 32
            or len({previous.article_id(row) for row in prior_rows}) != 32
            or old.get("files", {}).get(prior_followup.name, {}).get("sha256") != pilot.sha(prior_followup)):
        raise ValueError("Previous follow-up32 differs from its frozen manifest")
    reviews = [Path(path).resolve() for path in review_paths]
    if prior_followup not in reviews:
        reviews.append(prior_followup)
    dependencies = [source, source_manifest, prior_followup, prior_manifest, Path(companies),
                    Path(legacy_articles), Path(legacy_metadata), *reviews]
    if fallback_source:
        dependencies.append(Path(fallback_source))
    hashes = {str(path.resolve()): pilot.sha(path) for path in dependencies}
    if hashes[str(source)] != export.get("training_candidates_sha256"):
        raise ValueError("Frozen source export checksum mismatch")
    names = previous.company_names(companies)
    reviewed, holdout, exclusion_sources = previous.exclusion_indexes(reviews, legacy_articles, legacy_metadata, source, fallback_source)
    candidates, counts = collect(source, names, reviewed, holdout)
    if counts["source_counts"]["source_rows"] != export["training_candidates"]:
        raise ValueError("Source row count differs from manifest")
    selected, duplicates = select_unique(candidates)
    eval_order = evaluation_subset(selected)
    eval_ids = {row["article_id"] for row in eval_order}
    inputs, provenance = [], []
    with source.open("rb") as stream:
        for item in selected:
            stream.seek(item["offset"])
            raw = json.loads(stream.read(item["length"]).decode("utf-8-sig"))
            row = {name: raw.get(name) for name in previous.SOURCE_FIELDS}
            clean = pilot.minimal_clean(row)
            value, timing = previous.make_input(row, clean, item["ticker"], names)
            bodies, urls = previous.identity_keys(row)
            if (value["article_id"] != item["article_id"] or bodies != item["bodies"] or urls != item["urls"]
                    or previous.source_hash_keys(row) != item["source_hashes"] or target(row, names) != item["ticker"]):
                raise ValueError("Source changed while materializing selected articles")
            inputs.append(value)
            provenance.append({"article_id": item["article_id"], "ticker": item["ticker"], "region": item["region"],
                "market": names[item["ticker"]]["market"], "source_line": item["line"], "source_dataset": row["source_dataset"],
                "in_evaluation_subset": item["article_id"] in eval_ids, "article_selection_sha256": item["rank"],
                "evaluation_selection_sha256": rank(item["article_id"], "evaluation"),
                "input_sha256": previous.digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
                "raw_body_text_sha256": pilot.text_hash(row["body"]), "clean_body_text_sha256": pilot.text_hash(clean["body"]),
                "raw_body_utf8_sha256": previous.digest(row["body"]), "clean_body_utf8_sha256": previous.digest(clean["body"]),
                "body_characters": len(clean["body"]), "body_changed": row["body"] != clean["body"],
                "publication_time_status": timing["timestamp_status"]})
    indexed = {row["article_id"]: row for row in inputs}
    eval_inputs = [indexed[row["article_id"]] for row in eval_order]
    for path, expected in hashes.items():
        if pilot.sha(path) != expected:
            raise ValueError("Dependency changed during preparation")
    out.mkdir(parents=True, exist_ok=False)
    outputs = {"human_inputs.jsonl": inputs, "human_review_packet.jsonl": [{**row,
                   "input_sha256": previous.digest(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
                   **HUMAN_FIELDS} for row in inputs],
               "eval_inputs.jsonl": eval_inputs, "review_inputs_01.jsonl": eval_inputs[:16],
               "review_inputs_02.jsonl": eval_inputs[16:], "provenance.jsonl": provenance}
    for name, rows in outputs.items():
        pilot.write_jsonl(out / name, rows)
    summary = {"schema_version": "relevance-two-stage-data-1", "human_sample_articles": 100, "human_sample_pairs": 100,
        "human_sample_regions": dict(Counter(row["region"] for row in selected)), "evaluation_pairs": 32,
        "evaluation_regions": dict(Counter(row["region"] for row in eval_order)), "review_packet_pairs": [16, 16],
        "review_packet_regions": [{region: 8 for region in REGIONS} for _ in range(2)], **counts,
        "within_sample_duplicates_skipped": duplicates, "exclusion_sources": exclusion_sources,
        "previous_followup_articles_excluded": len(prior_rows),
        "exclusion_identity_coverage": {name: {"ids": len(index["ids"]), "body_keys": len(index["bodies"]),
            "url_keys": len(index["urls"]), "stored_source_hash_keys": len(index["source_hashes"]),
            "ids_without_recovered_body": len(index["ids"] - index["body_resolved_ids"]),
            "ids_without_recovered_url": len(index["ids"] - index["url_resolved_ids"])}
            for name, index in (("known_reviews_including_previous_followup", reviewed), ("holdout", holdout))},
        "selected_known_review_overlap": 0, "selected_holdout_overlap": 0, "evaluation_is_subset_of_human_sample": True,
        "body_length_cohort": {"unit": "Unicode characters after minimal cleaning", "inclusive_min": 200, "inclusive_max": 6000,
            "human_min": min(len(row["body"]) for row in inputs), "human_max": max(len(row["body"]) for row in inputs),
            "eval_min": min(len(row["body"]) for row in eval_inputs), "eval_max": max(len(row["body"]) for row in eval_inputs)},
        "selection_seed": SEED, "selection": "Hash-selected 50 per source region; one hash-selected source company; hash-selected 16 per region within the100 for evaluation; no semantic gate",
        "input_fields": list(pilot.INPUT_FIELDS), "source_fields_accessed": list(previous.SOURCE_FIELDS),
        "human_review_fields_initial_values": HUMAN_FIELDS, "human_review_completed": 0, "human_gold": False,
        "ai_predictions_in_human_packet": False, "reference_decisions_generated": False, "model_inference_performed": False,
        "labels_or_returns_used": False, "review_decisions_used": False, "semantic_topic_screening": False,
        "source_hashes": hashes, "cleaning": "Unchanged minimal_clean; complete cleaned body; no truncation or paraphrase",
        "company_names": "Current official dictionary; historical aliases, listing status and corporate attribution are not certified",
        "company_context": "null; no contemporaneous business profile supplied",
        "limitations": ["This region-balanced 200-6000-character cohort is not a source-population accuracy sample.",
            "The 32-pair evaluation is a subset of the 100 human-review inputs, not 32 additional independent articles.",
            "Known review and holdout exclusions are bounded by the enumerated local identity sources; unknown external exposure is not certified.",
            "Source mentions and official names do not certify economic relevance or historical company attribution.",
            "No human labels, AI predictions, future outcomes, price datasets or keyword-based event selections were used or generated."],
        "code_hashes": {path.name: pilot.sha(path) for path in (Path(__file__), Path(previous.__file__), Path(pilot.__file__),
            HERE / "news_content.py", HERE / "news_quality.py", HERE / "lake_news_normalize.py", HERE.parent / "ner/matcher.py")},
        "files": {name: {"sha256": pilot.sha(out / name), "bytes": (out / name).stat().st_size, "rows": len(rows)}
                  for name, rows in outputs.items()}}
    (out / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ARTIFACTS / "news_scale_source_20260916_50k_final/training_candidates.jsonl")
    parser.add_argument("--companies", type=Path, default=HERE.parent / "ner/data/companies.csv")
    parser.add_argument("--review-paths", nargs="+", type=Path, default=[ARTIFACTS / path for path in previous.DEFAULT_REVIEWS])
    parser.add_argument("--prior-followup", type=Path, default=ARTIFACTS / "local_relevance_followup_20260916_v2/new_inputs.jsonl")
    parser.add_argument("--legacy-articles", type=Path, default=ARTIFACTS / "news_impact_v2/articles.jsonl")
    parser.add_argument("--legacy-metadata", type=Path, default=ARTIFACTS / "news_quality_source_20260915/legacy_metadata.jsonl")
    parser.add_argument("--fallback-source", type=Path, default=ARTIFACTS / "news_quality_source_20260915/training_candidates.jsonl")
    parser.add_argument("--out", type=Path, default=ARTIFACTS / "relevance_two_stage_data_20260917_v1")
    result = prepare(**vars(parser.parse_args()))
    print(json.dumps({key: result[key] for key in ("human_sample_articles", "human_sample_regions", "evaluation_pairs",
        "evaluation_regions", "review_packet_pairs", "source_counts", "eligible_by_region", "files")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
