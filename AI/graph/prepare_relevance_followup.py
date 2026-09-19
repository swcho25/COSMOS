"""Freeze a text-only, previously unreviewed 32-pair length-cohort sample.

Selection uses source region and deterministic hashes, never semantic keywords,
review decisions, prices or returns. Existing artifacts are read-only. Known
review identities are hydrated before sampling so alternate IDs/URLs and raw or
minimally cleaned body duplicates are excluded as well.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

import prepare_local_relevance_pilot as pilot

HERE, ARTIFACTS = pilot.HERE, pilot.ARTIFACTS
SEED = "local-relevance-followup-20260916-v2:"
REGIONS = ("domestic", "overseas")
SOURCE_FIELDS = ("record_id", "title", "body", "url", "canonical_url", "published_date",
                 "published_at_raw", "published_at", "published_timezone", "date_normalization_method",
                 "collected_at", "region", "split", "source_dataset", "companies", "body_sha256", "body_dedup_sha256")
DEFAULT_REVIEWS = (
    "local_relevance_pilot_20260916_v1/inputs.jsonl",
    "news_scale_review_20260916/blind_packet.json",
    "news_quality_dataset_20260915/human_review.json",
    "news_quality_dataset_20260915_v2/human_review.json",
    "current_event_review_20260915/ai_review.json",
    "current_event_holdout_review_20260915/packet.json",
    "news_target_audit_20260915/blind.jsonl",
)


def digest(value):
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def rank(identity, purpose="article"):
    return digest(purpose + ":" + SEED + identity)


def source_rows(path):
    """Stream source rows; expose only article/provenance/candidate fields."""
    with Path(path).open("rb") as stream:
        line_number = 0
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            line_number += 1
            if line.strip():
                raw = json.loads(line.decode("utf-8-sig" if line_number == 1 else "utf8"))
                yield line_number, offset, len(line), {name: raw.get(name) for name in SOURCE_FIELDS}


def identity_index():
    return {"ids": set(), "bodies": set(), "urls": set(), "source_hashes": set(),
            "body_resolved_ids": set(), "url_resolved_ids": set()}


def article_id(row):
    return row.get("record_id") or row.get("article_id")


def identity_keys(row):
    bodies = set()
    for name in ("body", "original_body", "clean_body"):
        body = row.get(name)
        if isinstance(body, str) and body.strip():
            bodies.update(pilot.body_keys({"title": row.get("title") or row.get("clean_title") or "", "body": body}))
    urls = {pilot.canonicalize_url(row.get(name)) for name in ("url", "canonical_url", "source_url")} - {None}
    return bodies, urls


def source_hash_keys(row):
    # Source hashes retain their field namespace; they are not assumed to use
    # the same normalization as locally calculated raw/clean text hashes.
    return {(name, row[name].lower()) for name in ("body_sha256", "body_dedup_sha256")
            if isinstance(row.get(name), str) and len(row[name]) == 64
            and all(char in "0123456789abcdefABCDEF" for char in row[name])}


def add_identity(index, row):
    identity = article_id(row)
    if not isinstance(identity, str) or not identity:
        raise ValueError("An exclusion identity is missing")
    bodies, urls = identity_keys(row)
    index["ids"].add(identity)
    index["bodies"].update(bodies)
    index["urls"].update(urls)
    index["source_hashes"].update(source_hash_keys(row))
    if bodies:
        index["body_resolved_ids"].add(identity)
    if urls:
        index["url_resolved_ids"].add(identity)


def match_kinds(identity, bodies, urls, index, stored_hashes=()):
    return [name for name, match in (("id", identity in index["ids"]),
                                    ("body", bool(bodies & index["bodies"])),
                                    ("url", bool(urls & index["urls"])),
                                    ("stored_source_body_hash", bool(set(stored_hashes) & index["source_hashes"]))) if match]


def review_rows(path):
    if Path(path).suffix == ".jsonl":
        yield from pilot.rows(path)
        return
    document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(document, list):
        yield from document
    elif isinstance(document, dict) and isinstance(document.get("records"), list):
        yield from document["records"]
    else:
        raise ValueError("Unknown review identity container: " + str(path))


def exclusion_indexes(review_paths, legacy_articles, legacy_metadata, source, fallback_source=None):
    reviewed, holdout = identity_index(), identity_index()
    numeric_indexes, review_counts = set(), {}
    for path in review_paths:
        count = 0
        for row in review_rows(path):
            count += 1
            # Numeric target-audit article positions are zero-based legacy rows.
            if article_id(row) is None and isinstance(row.get("article"), int):
                if isinstance(row["article"], bool) or row["article"] < 0:
                    raise ValueError("Invalid target-audit article position")
                numeric_indexes.add(row["article"])
            else:
                add_identity(reviewed, row)
        review_counts[str(Path(path).resolve())] = count
    resolved_positions, split_counts = set(), Counter()
    for position, row in enumerate(pilot.rows(legacy_articles)):
        split = row.get("split")
        if split not in {"train", "val", "test"}:
            raise ValueError("Legacy split is missing or unsupported")
        identity = row.get("record_id")
        if position in numeric_indexes:
            add_identity(reviewed, row)
            resolved_positions.add(position)
        elif identity in reviewed["ids"]:
            add_identity(reviewed, row)
        if split in {"val", "test"}:
            add_identity(holdout, row)
            split_counts[split] += 1
    if numeric_indexes != resolved_positions:
        raise ValueError("Cannot resolve all prior numeric article identities")
    metadata_found = set()
    for row in pilot.rows(legacy_metadata):
        identity = row.get("record_id")
        if identity in holdout["ids"]:
            add_identity(holdout, row)
            metadata_found.add(identity)
        if identity in reviewed["ids"]:
            add_identity(reviewed, row)
    if metadata_found != holdout["ids"]:
        raise ValueError("Cannot recover every val/test metadata identity")
    # Hydration precedes eligibility: an excluded article may occur after its
    # alternate-ID duplicate in the source, so a one-pass filter is insufficient.
    for _, _, _, row in source_rows(source):
        if row["record_id"] in reviewed["ids"]:
            add_identity(reviewed, row)
        if row["record_id"] in holdout["ids"]:
            add_identity(holdout, row)
    fallback_scanned = False
    if fallback_source and reviewed["ids"] - reviewed["body_resolved_ids"]:
        fallback_scanned = True
        for _, _, _, row in source_rows(fallback_source):
            if row["record_id"] in reviewed["ids"]:
                add_identity(reviewed, row)
    if reviewed["ids"] - reviewed["body_resolved_ids"]:
        raise ValueError("Known reviewed article bodies could not all be recovered")
    return reviewed, holdout, {"review_records_by_file": review_counts,
                              "numeric_review_positions_resolved": len(resolved_positions),
                              "holdout_articles_by_split": dict(split_counts), "fallback_source_scanned": fallback_scanned}


def company_names(path):
    names = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            ticker, name = row["ticker"], row["name_official"]
            if ticker in names or not ticker or not name.strip():
                raise ValueError("Company names require unique tickers and nonempty official names")
            names[ticker] = {"name": name, "market": row["market"]}
    return names


def choose_ticker(row, names):
    candidates = {item.get("ticker") for item in row.get("companies") or [] if isinstance(item, dict)}
    if not candidates or not all(isinstance(ticker, str) and ticker in names for ticker in candidates):
        return None
    return min(candidates, key=lambda ticker: (rank(row["record_id"] + ":" + ticker, "target"), ticker))


def make_input(row, clean, ticker, names):
    timing = pilot.audit_timestamp(row)
    published_at = timing["publication_time_candidate"] or str(row["published_date"])[:10]
    if not "2020" <= published_at[:4] <= "2022":
        raise ValueError("Input publication timestamp is outside the training years")
    value = {"article_id": row["record_id"], "ticker": ticker, "company_name": names[ticker]["name"],
             "published_at": published_at, "title": clean["title"], "body": clean["body"],
             "company_context": None, "source_url": row.get("url") or row.get("canonical_url"),
             "source_split": "train", "case_kind": "real"}
    if tuple(value) != pilot.INPUT_FIELDS:
        raise ValueError("Input whitelist changed")
    return value, timing


def prepare(*, source, companies, review_paths, legacy_articles, legacy_metadata, out, fallback_source=None):
    source, out = Path(source).resolve(), Path(out).resolve()
    review_paths = [Path(path).resolve() for path in review_paths]
    if out.exists():
        raise FileExistsError("Choose a new output directory; existing inputs are immutable")
    source_manifest = source.with_name("manifest.json")
    manifest = json.loads(source_manifest.read_text(encoding="utf8"))
    if manifest.get("complete") is not True or not isinstance(manifest.get("training_candidates"), int):
        raise ValueError("Source export must be complete with a row count")
    dependencies = [source, source_manifest, Path(companies), Path(legacy_articles), Path(legacy_metadata), *review_paths]
    if fallback_source:
        dependencies.append(Path(fallback_source))
    fingerprints = {str(path.resolve()): pilot.sha(path) for path in dependencies}
    if fingerprints[str(source)] != manifest.get("training_candidates_sha256"):
        raise ValueError("Frozen source export checksum mismatch")
    names = company_names(companies)
    reviewed, holdout, exclusion_info = exclusion_indexes(review_paths, legacy_articles, legacy_metadata, source, fallback_source)
    counts, reasons, overlap_counts, eligible_counts = Counter(), Counter(), Counter(), Counter()
    seen_ids, candidates = set(), []
    for line_number, offset, length, row in source_rows(source):
        counts["source_rows"] += 1
        identity = row["record_id"]
        if not isinstance(identity, str) or not identity or identity in seen_ids:
            raise ValueError("Missing or repeated source record identity")
        seen_ids.add(identity)
        try:
            day = dt.date.fromisoformat(str(row["published_date"])[:10])
        except ValueError:
            reasons["invalid_publication_date"] += 1
            continue
        if not 2020 <= day.year <= 2022:
            reasons["outside_2020_2022"] += 1
            continue
        counts["training_period_rows"] += 1
        if row.get("split") not in (None, "train"):
            reasons["explicit_non_train_split"] += 1
            continue
        region = row["region"]
        if region not in REGIONS:
            reasons["unsupported_region"] += 1
            continue
        clean = pilot.minimal_clean(row)
        bodies = {pilot.text_hash(row["body"]), pilot.text_hash(clean["body"])} - {None}
        urls = pilot.url_keys(row)
        stored_hashes = source_hash_keys(row)
        old_matches = match_kinds(identity, bodies, urls, reviewed, stored_hashes)
        held_matches = match_kinds(identity, bodies, urls, holdout, stored_hashes)
        for prefix, matches in (("known_review", old_matches), ("holdout", held_matches)):
            for kind in matches:
                overlap_counts[prefix + "_" + kind] += 1
        if old_matches or held_matches:
            reasons["known_review_and_or_holdout_overlap"] += 1
            continue
        if not clean["title"].strip():
            reasons["empty_clean_title"] += 1
            continue
        if not 200 <= len(clean["body"]) <= 6000:
            reasons["outside_clean_body_length_cohort_200_6000"] += 1
            continue
        ticker = choose_ticker(row, names)
        if ticker is None:
            reasons["missing_or_unknown_source_company"] += 1
            continue
        try:
            make_input(row, clean, ticker, names)
        except ValueError:
            reasons["publication_timestamp_outside_training_years"] += 1
            continue
        eligible_counts[region] += 1
        candidates.append({"article_id": identity, "ticker": ticker, "region": region,
                           "rank": rank(identity), "bodies": bodies, "urls": urls,
                           "source_hashes": stored_hashes,
                           "line": line_number, "offset": offset, "length": length})
    if counts["source_rows"] != manifest["training_candidates"]:
        raise ValueError("Source row count differs from complete manifest")
    selected, selected_ids, selected_bodies, selected_urls, selected_hashes, quotas = [], set(), set(), set(), set(), Counter()
    sample_duplicates = 0
    for item in sorted(candidates, key=lambda item: (item["rank"], item["article_id"])):
        if quotas[item["region"]] == 16:
            continue
        if (item["article_id"] in selected_ids or item["bodies"] & selected_bodies or item["urls"] & selected_urls
                or item["source_hashes"] & selected_hashes):
            sample_duplicates += 1
            continue
        selected.append(item)
        selected_ids.add(item["article_id"])
        selected_bodies.update(item["bodies"])
        selected_urls.update(item["urls"])
        selected_hashes.update(item["source_hashes"])
        quotas[item["region"]] += 1
        if len(selected) == 32:
            break
    if quotas != Counter(dict.fromkeys(REGIONS, 16)):
        raise ValueError("Fewer than 16 unique eligible articles in a region")
    # Two review packets each contain eight articles from each source region.
    groups = {region: sorted((item for item in selected if item["region"] == region),
                             key=lambda item: rank(item["article_id"], "presentation")) for region in REGIONS}
    ordered = []
    for packet in range(2):
        group = [item for region in REGIONS for item in groups[region][packet * 8:(packet + 1) * 8]]
        ordered.extend(sorted(group, key=lambda item: rank(item["article_id"], "presentation")))
    inputs, provenance = [], []
    with source.open("rb") as stream:
        for item in ordered:
            stream.seek(item["offset"])
            original = json.loads(stream.read(item["length"]).decode("utf-8-sig"))
            row = {name: original.get(name) for name in SOURCE_FIELDS}
            clean = pilot.minimal_clean(row)
            value, timing = make_input(row, clean, item["ticker"], names)
            bodies, urls = identity_keys(row)
            if (value["article_id"] != item["article_id"] or bodies != item["bodies"] or urls != item["urls"]):
                raise ValueError("Source identity changed while sampling")
            inputs.append(value)
            provenance.append({"article_id": item["article_id"], "ticker": item["ticker"], "region": item["region"],
                               "source_line": item["line"], "source_dataset": row["source_dataset"],
                               "source_body_sha256": pilot.text_hash(row["body"]), "clean_body_sha256": pilot.text_hash(clean["body"]),
                               "input_sha256": digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
                               "source_body_utf8_sha256": digest(row["body"]), "clean_body_utf8_sha256": digest(clean["body"]),
                               "body_changed": row["body"] != clean["body"], "body_characters": len(clean["body"]),
                               "publication_time_status": timing["timestamp_status"], "market": names[item["ticker"]]["market"]})
    for path, expected in fingerprints.items():
        if pilot.sha(path) != expected:
            raise ValueError("Dependency changed during preparation: " + path)
    out.mkdir(parents=True, exist_ok=False)
    outputs = {"new_inputs.jsonl": inputs, "review_inputs_01.jsonl": inputs[:16],
               "review_inputs_02.jsonl": inputs[16:], "provenance.jsonl": provenance}
    for filename, values in outputs.items():
        pilot.write_jsonl(out / filename, values)
    summary = {
        "schema_version": "local-relevance-followup-2", "articles": 32, "pairs": 32,
        "regions": dict(quotas), "review_packet_pairs": [16, 16], "review_packet_regions": [{r: 8 for r in REGIONS}] * 2,
        "source_counts": dict(counts), "eligible_by_region": dict(eligible_counts), "first_exclusion_reason_counts": dict(reasons),
        "overlap_match_counts_nonexclusive": dict(overlap_counts), "within_sample_duplicates_skipped": sample_duplicates,
        "exclusion_sources": exclusion_info,
        "exclusion_identity_coverage": {name: {"ids": len(index["ids"]), "body_keys": len(index["bodies"]),
            "url_keys": len(index["urls"]), "stored_source_body_hash_keys": len(index["source_hashes"]),
            "ids_without_recovered_body": len(index["ids"] - index["body_resolved_ids"]),
            "ids_without_recovered_url": len(index["ids"] - index["url_resolved_ids"])}
            for name, index in (("known_reviews", reviewed), ("holdout", holdout))},
        "selected_known_review_overlap": 0, "selected_holdout_overlap": 0,
        "body_length_cohort": {"unit": "Unicode characters after minimal cleaning", "inclusive_min": 200, "inclusive_max": 6000,
                               "selected_min": min(len(row["body"]) for row in inputs), "selected_max": max(len(row["body"]) for row in inputs)},
        "source_hashes": fingerprints, "selection_seed": SEED,
        "selection": "Lowest SHA256 article rank with 16 per source region; one SHA256-selected source company; cross-ID body/URL deduplication; no semantic keyword gate",
        "input_fields": list(pilot.INPUT_FIELDS), "source_fields_accessed": list(SOURCE_FIELDS),
        "identity_hash_normalization": "NFC plus collapsed whitespace for calculated raw/clean body keys; source body_sha256/body_dedup_sha256 kept in separate field namespaces; canonical URLs",
        "cleaning": "Unchanged prepare_local_relevance_pilot.minimal_clean; complete cleaned body; no truncation, paraphrase or event screening",
        "company_names": "Current official company dictionary; historical aliases, listing status and identity are not certified",
        "company_context": "null; no contemporaneous business profile supplied",
        "labels_or_returns_used": False, "review_decisions_used": False, "semantic_topic_screening": False,
        "human_gold": False, "human_review_completed": 0, "model_inference_performed": False,
        "limitations": [
            "The sample is new relative to the enumerated local reviews and legacy holdout identities, not all unknown external reviews.",
            "Region balance and the 200-6000 character length cohort do not represent the source population.",
            "Source company candidates and current official names are fallible and do not certify economic relevance or historical identity.",
            "Val/test split, identities and text are used only for exclusion; no price/outcome/prediction files or review verdict fields are used.",
            "Original publication authenticity, later edits and knowledge embedded in a future model cannot be certified here.",
        ],
        "code_hashes": {path.name: pilot.sha(path) for path in (Path(__file__), Path(pilot.__file__), HERE / "news_content.py",
                           HERE / "news_quality.py", HERE / "lake_news_normalize.py", HERE.parent / "ner/matcher.py")},
        "files": {filename: {"sha256": pilot.sha(out / filename), "bytes": (out / filename).stat().st_size, "rows": len(values)}
                  for filename, values in outputs.items()},
    }
    (out / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ARTIFACTS / "news_scale_source_20260916_50k_final/training_candidates.jsonl")
    parser.add_argument("--companies", type=Path, default=HERE.parent / "ner/data/companies.csv")
    parser.add_argument("--review-paths", nargs="+", type=Path, default=[ARTIFACTS / path for path in DEFAULT_REVIEWS])
    parser.add_argument("--legacy-articles", type=Path, default=ARTIFACTS / "news_impact_v2/articles.jsonl")
    parser.add_argument("--legacy-metadata", type=Path, default=ARTIFACTS / "news_quality_source_20260915/legacy_metadata.jsonl")
    parser.add_argument("--fallback-source", type=Path, default=ARTIFACTS / "news_quality_source_20260915/training_candidates.jsonl")
    parser.add_argument("--out", type=Path, default=ARTIFACTS / "local_relevance_followup_20260916_v2")
    summary = prepare(**vars(parser.parse_args()))
    # Keep article topics, identities and decisions out of coordinator output.
    print(json.dumps({name: summary[name] for name in ("articles", "pairs", "regions", "review_packet_pairs", "source_counts",
                                                      "eligible_by_region", "first_exclusion_reason_counts", "files")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
