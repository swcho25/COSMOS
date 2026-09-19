"""Prepare a dated-context-supported challenge with no semantic selection.

Only source identities, region, publication time, cleaned text length, source
company candidates and the frozen historical registry determine eligibility.
This producer does not run a model or generate news relevance decisions.
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import json
from pathlib import Path

import historical_company_context as context
import prepare_relevance_followup as previous

pilot = previous.pilot
HERE, ARTIFACTS, REGIONS = previous.HERE, previous.ARTIFACTS, previous.REGIONS
SEED = "relevance-identity-v3-challenge-20260917"
PER_REGION, PER_TICKER = 8, 2
EXPECTED_COUNTS = {"known_reviews": 612, "reserved_random": 32, "holdout": 11969}


def rank(identity, purpose="article"):
    return previous.digest(purpose + ":" + SEED + ":" + identity)


def safe_source(row):
    """Retain article fields and candidate tickers, not arbitrary nested metadata."""
    result = {key: row.get(key) for key in previous.SOURCE_FIELDS}
    result["companies"] = [{"ticker": item.get("ticker")} for item in row.get("companies") or []
                           if isinstance(item, dict)]
    return result


def supported_target(row, clean, names, registry):
    """Choose one fixed target among source candidates with usable dated context."""
    candidates = {item.get("ticker") for item in row.get("companies") or [] if isinstance(item, dict)
                  and isinstance(item.get("ticker"), str) and item.get("ticker")}
    registry_tickers = {record["ticker"] for record in registry["records"]}
    matched, reasons = [], Counter()
    for ticker in sorted(candidates):
        if ticker not in names:
            reasons["source_company_not_in_inventory"] += 1
            continue
        if ticker not in registry_tickers:
            reasons["ticker_not_in_registry"] += 1
            continue
        try:
            value, timing = previous.make_input(row, clean, ticker, names)
            enriched, audit = context.resolve(value, registry)
        except ValueError:
            reasons["context_application_error"] += 1
            continue
        reasons[audit["reason"]] += 1
        if audit["context_added"]:
            matched.append((ticker, value, enriched, audit, timing))
    if not candidates:
        reasons["missing_source_company"] += 1
    if not matched:
        return None, reasons, []
    chosen = min(matched, key=lambda item: (rank(row["record_id"] + ":" + item[0], "target"), item[0]))
    return chosen, reasons, sorted(item[0] for item in matched)


def collect(source, names, registry, indexes):
    context.validate_registry(registry)
    counts, exclusions, overlaps, context_reasons, eligible = Counter(), Counter(), Counter(), Counter(), Counter()
    seen, candidates = set(), []
    for line, offset, length, row in previous.source_rows(source):
        counts["source_rows"] += 1
        identity = row["record_id"]
        context.require(isinstance(identity, str) and identity and identity not in seen,
                        "Missing or repeated source article identity")
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
        bodies, urls = previous.identity_keys(row)
        stored = previous.source_hash_keys(row)
        excluded = False
        for name, index in indexes.items():
            kinds = previous.match_kinds(identity, bodies, urls, index, stored)
            overlaps.update(name + "_" + kind for kind in kinds)
            excluded = excluded or bool(kinds)
        if excluded:
            exclusions["known_review_reserved_random_and_or_holdout_overlap"] += 1
            continue
        if not clean["title"].strip():
            exclusions["empty_clean_title"] += 1
            continue
        if not 200 <= len(clean["body"]) <= 6000:
            exclusions["outside_clean_body_length_cohort_200_6000"] += 1
            continue
        counts["eligible_text_rows_before_context"] += 1
        chosen, reasons, supported = supported_target(row, clean, names, registry)
        context_reasons.update(reasons)
        if chosen is None:
            exclusions["no_usable_dated_company_context"] += 1
            continue
        ticker, value, enriched, audit, timing = chosen
        eligible[row["region"]] += 1
        counts["eligible_context_rows"] += 1
        candidates.append({"article_id": identity, "ticker": ticker, "region": row["region"],
                           "rank": rank(identity), "bodies": bodies, "urls": urls, "source_hashes": stored,
                           "line": line, "offset": offset, "length": length,
                           "source_row_sha256": context.row_sha256(safe_source(row)),
                           "input_sha256": context.row_sha256(value), "context_input_sha256": context.row_sha256(enriched),
                           "supported_tickers": supported, "context_audit": audit})
    return candidates, {"source_counts": dict(counts), "eligible_context_rows": counts["eligible_context_rows"],
                        "eligible_context_rows_by_region": dict(eligible),
                        "context_candidate_pair_reason_counts": dict(context_reasons),
                        "context_application_errors": context_reasons["context_application_error"],
                        "expired_context_candidate_pairs": context_reasons["snapshot_expired"],
                        "first_exclusion_reason_counts": dict(exclusions),
                        "overlap_match_counts_nonexclusive": dict(overlaps)}


def select_unique(candidates, allow_smaller_balanced_sample=False):
    """Try decreasing quotas only when opted in; retain the same hash-greedy rule."""
    context.require(type(allow_smaller_balanced_sample) is bool, "Size adaptation flag must be boolean")
    ordered = sorted(candidates, key=lambda value: (value["rank"], value["article_id"], value["ticker"]))
    targets = range(PER_REGION, 0, -1) if allow_smaller_balanced_sample else (PER_REGION,)
    for per_region in targets:
        selected, quotas, tickers, skipped = [], Counter(), Counter(), Counter()
        seen = {key: set() for key in ("bodies", "urls", "source_hashes")}
        ids = set()
        for item in ordered:
            if quotas[item["region"]] >= per_region:
                continue
            if tickers[item["ticker"]] >= PER_TICKER:
                skipped["ticker_cap"] += 1
                continue
            if item["article_id"] in ids or any(item[key] & seen[key] for key in seen):
                skipped["within_sample_duplicate"] += 1
                continue
            selected.append(item)
            ids.add(item["article_id"])
            quotas[item["region"]] += 1
            tickers[item["ticker"]] += 1
            for key in seen:
                seen[key].update(item[key])
            if len(selected) == per_region * len(REGIONS):
                break
        if quotas == Counter(dict.fromkeys(REGIONS, per_region)):
            return sorted(selected, key=lambda item: (rank(item["article_id"], "presentation"), item["article_id"])), dict(skipped)
    raise ValueError("No balanced sample of at least 1 per region under the 2-per-ticker cap" if allow_smaller_balanced_sample
                     else "Fewer than 8 unique context-supported articles per region under the 2-per-ticker cap")


def materialize(source, selected, names, registry, indexes):
    context.validate_registry(registry)
    inputs, contextual, sources, provenance = [], [], [], []
    with Path(source).open("rb") as stream:
        for item in selected:
            stream.seek(item["offset"])
            raw_bytes = stream.read(item["length"])
            raw = context.read_json(raw_bytes)
            row = {key: raw.get(key) for key in previous.SOURCE_FIELDS}
            clean = pilot.minimal_clean(row)
            chosen, _, supported = supported_target(row, clean, names, registry)
            context.require(chosen is not None, "Selected source no longer has usable company context")
            ticker, value, enriched, audit, timing = chosen
            bodies, urls = previous.identity_keys(row)
            stored = previous.source_hash_keys(row)
            context.require(value["article_id"] == item["article_id"] and ticker == item["ticker"]
                            and row["region"] == item["region"] and supported == item["supported_tickers"]
                            and context.row_sha256(safe_source(row)) == item["source_row_sha256"]
                            and context.row_sha256(value) == item["input_sha256"]
                            and context.row_sha256(enriched) == item["context_input_sha256"]
                            and bodies == item["bodies"] and urls == item["urls"] and stored == item["source_hashes"],
                            "Source or context changed during materialization")
            context.require(not any(previous.match_kinds(item["article_id"], bodies, urls, index, stored)
                                    for index in indexes.values()), "Selected article overlaps an excluded identity")
            context.require({k: v for k, v in value.items() if k not in {"company_name", "company_context"}}
                            == {k: v for k, v in enriched.items() if k not in {"company_name", "company_context"}},
                            "Context application changed original article fields")
            inputs.append(value)
            contextual.append(enriched)
            sources.append(safe_source(row))
            provenance.append({"article_id": item["article_id"], "ticker": ticker, "region": item["region"],
                "source_line": item["line"], "source_row_sha256": item["source_row_sha256"],
                "source_line_bytes_sha256": context.sha256(raw_bytes), "source_dataset": row["source_dataset"],
                "input_sha256": item["input_sha256"], "context_input_sha256": item["context_input_sha256"],
                "article_selection_sha256": item["rank"], "target_selection_sha256": rank(item["article_id"] + ":" + ticker, "target"),
                "supported_source_tickers": supported, "context_audit": audit,
                "raw_body_text_sha256": pilot.text_hash(row["body"]), "clean_body_text_sha256": pilot.text_hash(clean["body"]),
                "raw_body_utf8_sha256": previous.digest(row["body"]), "clean_body_utf8_sha256": previous.digest(clean["body"]),
                "body_characters": len(clean["body"]), "body_changed": row["body"] != clean["body"],
                "publication_time_status": timing["timestamp_status"]})
    return {"inputs.jsonl": inputs, "context_inputs.jsonl": contextual,
            "sources.jsonl": sources, "provenance.jsonl": provenance}


def prepare(*, source, companies, registry, review_paths, reserved_random, legacy_articles, legacy_metadata,
            out, fallback_source=None, expected_counts=None, allow_smaller_balanced_sample=False):
    """expected_counts is for small synthetic fixtures; CLI enforces production counts."""
    counts_expected = dict(EXPECTED_COUNTS if expected_counts is None else expected_counts)
    context.require(set(counts_expected) == set(EXPECTED_COUNTS)
                    and all(type(value) is int and value > 0 for value in counts_expected.values()), "Invalid expected exclusion counts")
    source, out, registry, reserved_random = (Path(path).resolve() for path in (source, out, registry, reserved_random))
    context.require(not out.exists(), "Output must be a new directory; refusing to overwrite")
    reviews = [Path(path).resolve() for path in review_paths]
    context.require(reviews and len(set(reviews)) == len(reviews) and reserved_random not in reviews,
                    "Specify distinct known-review paths and a separate reserved random sample")
    source_manifest, reserved_manifest = source.with_name("manifest.json"), reserved_random.with_name("manifest.json")
    dependencies = [source, source_manifest, registry, Path(companies), reserved_random, reserved_manifest,
                    Path(legacy_articles), Path(legacy_metadata), *reviews]
    if fallback_source:
        dependencies.append(Path(fallback_source))
    hashes = {str(path.resolve()): pilot.sha(path) for path in dependencies}
    code_paths = (Path(__file__), Path(context.__file__), Path(previous.__file__), Path(pilot.__file__),
                  HERE / "news_content.py", HERE / "news_quality.py", HERE / "lake_news_normalize.py", HERE.parent / "ner/matcher.py")
    code_hashes = {str(path.resolve()): pilot.sha(path) for path in code_paths}
    export = context.read_json(source_manifest.read_bytes())
    context.require(export.get("complete") is True and type(export.get("training_candidates")) is int
                    and hashes[str(source)] == export.get("training_candidates_sha256"), "Canonical source export is not frozen or complete")
    reserved_info = context.read_json(reserved_manifest.read_bytes())
    reserved_rows = list(previous.review_rows(reserved_random))
    context.require(len(reserved_rows) == counts_expected["reserved_random"]
                    and reserved_info.get("files", {}).get(reserved_random.name, {}).get("sha256") == hashes[str(reserved_random)]
                    and len({previous.article_id(row) for row in reserved_rows}) == len(reserved_rows),
                    "Reserved random sample differs from its frozen manifest or expected size")
    data = context.validate_registry(context.read_json(registry.read_bytes()))
    names = previous.company_names(companies)
    reviewed, holdout, exclusion_sources = previous.exclusion_indexes(reviews, legacy_articles, legacy_metadata, source, fallback_source)
    reserved, reserved_holdout, reserved_sources = previous.exclusion_indexes([reserved_random], legacy_articles, legacy_metadata, source, fallback_source)
    context.require(holdout == reserved_holdout, "Holdout hydration differed across immutable passes")
    indexes = {"known_reviews": reviewed, "reserved_random": reserved, "holdout": holdout}
    for name, index in indexes.items():
        context.require(len(index["ids"]) == counts_expected[name], f"Unexpected {name} exclusion identity count")
        context.require(not (index["ids"] - index["body_resolved_ids"]), f"Unresolved {name} exclusion bodies")
    context.require(not reviewed["ids"] & reserved["ids"], "Reserved random identities overlap prior reviews")
    for row in reserved_rows:
        bodies, urls = previous.identity_keys(row)
        context.require(not any(previous.match_kinds(previous.article_id(row), bodies, urls, index, previous.source_hash_keys(row))
                                for index in (reviewed, holdout)), "Reserved random sample overlaps a prior review or holdout")
    candidates, statistics = collect(source, names, data, indexes)
    context.require(statistics["source_counts"]["source_rows"] == export["training_candidates"], "Canonical source row count mismatch")
    selected, skipped = select_unique(candidates, allow_smaller_balanced_sample=allow_smaller_balanced_sample)
    actual_per_region = len(selected) // len(REGIONS)
    outputs = materialize(source, selected, names, data, indexes)
    summary = {"schema": "cosmos-relevance-identity-challenge-1", "complete": True, "seed": SEED,
        "articles": len(selected), "pairs": len(selected), "regions": dict(Counter(item["region"] for item in selected)),
        "planned_per_region": PER_REGION, "actual_per_region": actual_per_region,
        "shortfall": PER_REGION * len(REGIONS) - len(selected), "shortfall_per_region": PER_REGION - actual_per_region,
        "ticker_eligible_counts": dict(sorted(Counter(item["ticker"] for item in candidates).items())),
        "ticker_cap_rejections_in_selected_quota_attempt": skipped.get("ticker_cap", 0),
        "size_adaptation": {"allowed": allow_smaller_balanced_sample, "used": actual_per_region < PER_REGION,
            "attempted_per_region": list(range(PER_REGION, actual_per_region - 1, -1)),
            "policy": "largest balanced quota reached by unchanged deterministic hash-greedy selection, from 8 down to 1",
            "decision_uses_counts_and_identity_metadata_only": True, "semantic_content_or_decisions_used": False,
            "time_source_body_duplicate_and_ticker_cap_constraints_relaxed": False},
        "ticker_counts": dict(sorted(Counter(item["ticker"] for item in selected).items())), "maximum_per_ticker": PER_TICKER,
        **statistics, "selection_skip_counts": skipped, "exclusion_sources": exclusion_sources,
        "reserved_random_exclusion_sources": reserved_sources, "expected_exclusion_counts": counts_expected,
        "exclusion_identity_coverage": {name: {"ids": len(index["ids"]), "body_keys": len(index["bodies"]),
            "url_keys": len(index["urls"]), "stored_source_hash_keys": len(index["source_hashes"]),
            "ids_without_recovered_body": len(index["ids"] - index["body_resolved_ids"]),
            "ids_without_recovered_url": len(index["ids"] - index["url_resolved_ids"])} for name, index in indexes.items()},
        "selected_exclusion_overlap": 0, "source_hashes": hashes, "code_hashes": code_hashes,
        "source_fields_accessed": list(previous.SOURCE_FIELDS), "input_fields": list(pilot.INPUT_FIELDS),
        "body_length_cohort": {"inclusive_min": 200, "inclusive_max": 6000, "unit": "Unicode characters after minimal cleaning"},
        "selection": f"Hash target among dated-context-supported source company candidates, then hash article order with {actual_per_region} per region and 2 per ticker; no alternative target after cap",
        "labels_or_returns_used": False, "review_decisions_used": False, "semantic_topic_screening": False,
        "model_inference_performed": False, "reference_decisions_generated": False, "human_gold": False,
        "limitations": ["This is a context-supported challenge, not a representative random accuracy sample.",
            "Source company candidates and registry facts do not certify article economic relevance.",
            "Registry relationships describe observations and do not establish uninterrupted ownership afterward.",
            "Current inventory names appear only in original inputs, never as historical evidence.",
            "Known review and holdout exclusions are limited to the enumerated identity sources.",
            "A reduced balanced sample is a sparse diagnostic only; it does not establish broad accuracy or issuer coverage."]}
    raw_outputs = {name: ("".join(context.canonical(row) + "\n" for row in rows)).encode("utf8") for name, rows in outputs.items()}
    summary["files"] = {name: {"sha256": context.sha256(raw), "bytes": len(raw), "rows": len(outputs[name])}
                        for name, raw in raw_outputs.items()}

    def unchanged():
        for path, checksum in {**hashes, **code_hashes}.items():
            context.require(pilot.sha(path) == checksum, "Dependency changed during challenge preparation")

    unchanged()
    out.mkdir(parents=True, exist_ok=False)
    for name, raw in raw_outputs.items():
        context._write_new(out / name, raw)
    unchanged()
    for name, info in summary["files"].items():
        context.require(pilot.sha(out / name) == info["sha256"], "Challenge output verification failed")
    # Completion marker is last; failed partial outputs remain immutable.
    context._write_new(out / "manifest.json", (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf8"))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ARTIFACTS / "news_scale_source_20260916_50k_final/training_candidates.jsonl")
    parser.add_argument("--companies", type=Path, default=HERE.parent / "ner/data/companies.csv")
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--review-paths", nargs="+", type=Path, required=True)
    parser.add_argument("--reserved-random", type=Path, required=True)
    parser.add_argument("--legacy-articles", type=Path, default=ARTIFACTS / "news_impact_v2/articles.jsonl")
    parser.add_argument("--legacy-metadata", type=Path, default=ARTIFACTS / "news_quality_source_20260915/legacy_metadata.jsonl")
    parser.add_argument("--fallback-source", type=Path, default=ARTIFACTS / "news_quality_source_20260915/training_candidates.jsonl")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-smaller-balanced-sample", action="store_true",
                        help="Opt in to decreasing the quota from 8 to at least 1 per region without relaxing eligibility or duplicate/ticker constraints")
    summary = prepare(**vars(parser.parse_args()))
    print(json.dumps({key: summary[key] for key in ("articles", "regions", "planned_per_region", "actual_per_region", "shortfall",
        "ticker_eligible_counts", "ticker_cap_rejections_in_selected_quota_attempt", "eligible_context_rows",
        "eligible_context_rows_by_region", "context_application_errors", "expired_context_candidate_pairs", "files")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
