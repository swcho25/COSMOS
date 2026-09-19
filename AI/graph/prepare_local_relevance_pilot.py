"""Prepare a small real-news relevance pilot without model or outcome access.

Only 2020--2022 articles from the existing frozen training export are eligible.
Prior AI current-event reviews supply diagnostic strata and a separately stored
selection proxy. They are NOT human gold or labels for the broader economic-
relevance task. Model inputs contain neither those reviews nor price features.
Existing sources are read only; an existing output directory is refused.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from lake_news_normalize import canonicalize_url
from news_content import sanitize_article
from news_quality import audit_timestamp


HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE / "artifacts"
SEED = "local-relevance-pilot-20260916-v1:"
INPUT_FIELDS = (
    "article_id", "ticker", "company_name", "published_at", "title", "body",
    "company_context", "source_url", "source_split", "case_kind",
)
STRATA = ("direct_event", "incidental_or_other_subject", "historical_background", "uncertain")
SELECTIONS = {"supported": "채택", "unsupported": "제외", "uncertain": "보류"}
# The 240-review verdict is article-level. Restrict ambiguous multi-company
# cases using its written reasons instead of transferring that verdict blindly.
# 9: SDI is background, only Samsung Electronics is uncertain.
# 114: Netflix's subscriber event lacks a date; other companies are a schedule.
# 234: Woori's completed compensation is background; Hana/Shinhan are uncertain.
UNCERTAIN_TARGETS = {
    9: {"005930"}, 98: {"005930", "066570"}, 114: {"NFLX"},
    234: {"086790", "055550"}, 237: {"AAPL", "AMZN"},
}


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def rows(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def text_hash(text):
    if not isinstance(text, str) or not text.strip():
        return None
    return hashlib.sha256(" ".join(unicodedata.normalize("NFC", text).split()).encode("utf8")).hexdigest()


def minimal_clean(row):
    """Reuse existing text hygiene; never generate or paraphrase article text."""
    clean = sanitize_article({"title": row.get("title") or "", "body": row.get("body") or ""})
    # Same extra boundary as existing target_news_features.clean_target_article,
    # implemented here to avoid importing torch/transformers for preparation.
    match = re.search(r"(?m)^\s*관련\s*뉴스\s*$", clean["body"])
    if match:
        clean["body"] = clean["body"][:match.start()]
    return clean


def body_keys(row):
    return {text_hash(row.get("body")), text_hash(minimal_clean(row)["body"])} - {None}


def url_keys(row):
    return {canonicalize_url(row.get(key)) for key in ("url", "canonical_url")} - {None}


def holdout_index(articles, metadata):
    ids, bodies, urls = set(), set(), set()
    counts = Counter()
    for row in rows(articles):
        if row.get("split") not in {"train", "val", "test"}:
            raise ValueError("Legacy split is missing or unsupported")
        if row["split"] in {"val", "test"}:
            ids.add(row["record_id"])
            bodies.update(body_keys(row))
            counts[row["split"]] += 1
    found = set()
    for row in rows(metadata):
        if row["record_id"] in ids:
            urls.update(url_keys(row))
            found.add(row["record_id"])
    if ids != found:
        raise ValueError("Cannot recover every legacy val/test identity")
    return {"ids": ids, "bodies": bodies, "urls": urls, "counts": dict(counts)}


def overlaps_holdout(row, holdout):
    return (row["record_id"] in holdout["ids"] or bool(body_keys(row) & holdout["bodies"])
            or bool(url_keys(row) & holdout["urls"]))


def stratum(review):
    verdict = review["verdict"]
    if verdict == "supported":
        return "direct_event"
    if verdict == "uncertain":
        return "uncertain"
    if verdict == "unsupported":
        return "historical_background" if review["historical_background"] == "only" else "incidental_or_other_subject"
    raise ValueError("Unknown prior review verdict")


def eligible_tickers(packet, review):
    available = {company["ticker"] for company in packet["source_companies"]}
    if review["verdict"] == "supported":
        result = set(review["supported_tickers"])
    elif review["verdict"] == "uncertain" and len(available) > 1:
        result = UNCERTAIN_TARGETS.get(packet["review_id"], set())
    else:
        result = available
    if not result <= available:
        raise ValueError("Prior pair reference names a non-source company")
    return sorted(result)


def order(value, prefix="selection:"):
    return hashlib.sha256((prefix + SEED + value).encode("utf8")).hexdigest()


def choose(candidates, per_stratum):
    """One target per article, evenly distributed among four diagnostic strata."""
    grouped = defaultdict(list)
    for item in candidates:
        grouped[item["stratum"]].append(item)
    size = min(per_stratum, *(len(grouped[name]) for name in STRATA))
    if size < 8:
        raise ValueError("Fewer than eight eligible articles in at least one stratum")
    result = []
    for name in STRATA:
        group = sorted(grouped[name], key=lambda row: order(row["input"]["article_id"]))
        result.extend(group[:size])
    result.sort(key=lambda row: order(row["input"]["article_id"], "presentation:"))
    ids = [row["input"]["article_id"] for row in result]
    if len(ids) != len(set(ids)):
        raise ValueError("An article appears more than once in the pilot")
    return result, {name: len(grouped[name]) for name in STRATA}


def make_case(raw, packet, review, ticker):
    day = dt.date.fromisoformat(str(raw["published_date"])[:10])
    if not 2020 <= day.year <= 2022:
        raise ValueError("Outside 2020--2022 training period")
    if day.isoformat() != packet["published_date"]:
        raise ValueError("Review/source publication date differs")
    if raw["title"] != packet["title"] or raw["body"] != packet["original_body"]:
        raise ValueError("Review/source text differs")
    clean = minimal_clean(raw)
    if clean["title"] != packet["clean_title"] or clean["body"] != packet["clean_body"]:
        raise ValueError("Minimal cleaner differs from the prior reviewed text")
    company = next(company for company in packet["source_companies"] if company["ticker"] == ticker)
    if not company.get("name_official"):
        raise ValueError("Missing target company name")
    timing = audit_timestamp(raw)
    published_at = timing["publication_time_candidate"] or day.isoformat()
    model_input = {
        "article_id": raw["record_id"], "ticker": ticker, "company_name": company["name_official"],
        "published_at": published_at, "title": clean["title"], "body": clean["body"],
        "company_context": None, "source_url": raw.get("url") or raw.get("canonical_url"),
        "source_split": "train", "case_kind": "real",
    }
    if tuple(model_input) != INPUT_FIELDS:
        raise ValueError("Model input whitelist changed")
    evaluation = {
        "article_id": raw["record_id"], "ticker": ticker, "expected_label": None,
        "expected_selection": SELECTIONS[review["verdict"]], "reason": review["reason"],
        "reference_type": "prior_ai_review", "reference_review_id": packet["review_id"],
        "reference_verdict": review["verdict"], "reference_quote": review["quote"],
        "reference_task": "current_corporate_event_support_proxy_not_full_economic_relevance",
        "sample_stratum": stratum(review), "human_gold": False,
    }
    provenance = {
        "article_id": raw["record_id"], "ticker": ticker, "review_id": packet["review_id"],
        "source_dataset": raw.get("source_dataset"), "region": packet["region"], "year": day.year,
        "source_title_sha256": text_hash(raw["title"]), "source_body_sha256": text_hash(raw["body"]),
        "input_body_sha256": text_hash(clean["body"]), "body_changed": clean["body"] != raw["body"],
        "publication_precision": "timestamp" if timing["publication_time_candidate"] else "date",
        "publication_time_status": timing["timestamp_status"],
        "company_name_source": "prior_review_current_dictionary_not_certified_historical_profile",
    }
    return {"input": model_input, "evaluation": evaluation, "provenance": provenance, "stratum": stratum(review)}


def write_jsonl(path, records):
    with path.open("x", encoding="utf8", newline="\n") as stream:
        for row in records:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


def prepare(*, source, review_dir, legacy_articles, legacy_metadata, out, per_stratum=10, max_body_characters=20000):
    if not 8 <= per_stratum <= 12:
        raise ValueError("per_stratum must be 8--12 (32--48 total)")
    source, review_dir, out = Path(source), Path(review_dir), Path(out)
    if out.exists():
        raise FileExistsError("Choose a new output directory; existing pilot is immutable")
    packet_path, review_path = review_dir / "blind_packet.json", review_dir / "ai_review.json"
    packet = json.loads(packet_path.read_text(encoding="utf8"))
    review_document = json.loads(review_path.read_text(encoding="utf8"))
    if review_document.get("human_gold") is not False or review_document["packet_sha256"] != sha(packet_path):
        raise ValueError("Prior review/packet contract differs")
    reviews = {row["record_id"]: row for row in review_document["reviews"]}
    if len(reviews) != len(review_document["reviews"]):
        raise ValueError("Duplicate prior review identity")
    rejected = Counter()
    wanted = {}
    for item in packet:
        review = reviews[item["record_id"]]
        if not "2020-01-01" <= item["published_date"] <= "2022-12-31":
            rejected["outside_training_period"] += 1
        elif review["feed_contamination"] != "none":
            rejected["prior_feed_contamination"] += 1
        elif not eligible_tickers(item, review):
            rejected["no_unambiguous_pair_reference"] += 1
        else:
            wanted[item["record_id"]] = item
    holdout = holdout_index(legacy_articles, legacy_metadata)
    dependencies = [source, packet_path, review_path, Path(legacy_articles), Path(legacy_metadata)]
    source_manifest = source.with_name("manifest.json")
    manifest = json.loads(source_manifest.read_text(encoding="utf8"))
    if manifest.get("complete") is not True or not manifest.get("training_candidates_sha256"):
        raise ValueError("Source requires a complete export manifest with SHA256")
    dependencies.append(source_manifest)
    fingerprints = {str(path.resolve()): sha(path) for path in dependencies}
    if fingerprints[str(source.resolve())] != manifest["training_candidates_sha256"]:
        raise ValueError("Frozen source export checksum mismatch")
    found, candidates = set(), []
    for line_number, raw in enumerate(rows(source), 1):
        record_id = raw["record_id"]
        if record_id not in wanted:
            continue
        if record_id in found:
            raise ValueError("Repeated source identity in frozen export")
        found.add(record_id)
        if raw.get("split") in {"val", "test"} or overlaps_holdout(raw, holdout):
            rejected["holdout_overlap"] += 1
            continue
        item, review = wanted[record_id], reviews[record_id]
        clean = minimal_clean(raw)
        if not clean["body"].strip() or len(clean["body"]) > max_body_characters:
            rejected["empty_or_oversized_body_no_truncation"] += 1
            continue
        tickers = eligible_tickers(item, review)
        ticker = min(tickers, key=lambda value: order(record_id + ":" + value, "target:"))
        case = make_case(raw, item, review, ticker)
        case["provenance"]["source_line_number"] = line_number
        candidates.append(case)
    if found != set(wanted):
        raise ValueError("Prior reviewed articles missing from source export: " + str(len(set(wanted) - found)))
    selected, eligible_counts = choose(candidates, per_stratum)
    out.mkdir(parents=True, exist_ok=False)
    for key, filename in (("input", "inputs.jsonl"), ("evaluation", "evaluation.jsonl"), ("provenance", "provenance.jsonl")):
        write_jsonl(out / filename, (case[key] for case in selected))
    summary = {
        "schema_version": "local-relevance-pilot-1", "pairs": len(selected), "articles": len(selected),
        "strata": dict(Counter(case["stratum"] for case in selected)),
        "expected_selection_proxy": dict(Counter(case["evaluation"]["expected_selection"] for case in selected)),
        "years": dict(Counter(str(case["provenance"]["year"]) for case in selected)),
        "regions": dict(Counter(case["provenance"]["region"] for case in selected)),
        "publication_precision": dict(Counter(case["provenance"]["publication_precision"] for case in selected)),
        "body_characters": {"min": min(len(case["input"]["body"]) for case in selected),
                            "max": max(len(case["input"]["body"]) for case in selected)},
        "eligible_articles_by_stratum": eligible_counts, "excluded_articles": dict(rejected),
        "heldout_comparison_articles": holdout["counts"], "selected_holdout_overlap": 0,
        "source_hashes": fingerprints, "code_hashes": {path.name: sha(path) for path in (
            Path(__file__), HERE / "news_content.py", HERE / "news_quality.py", HERE / "lake_news_normalize.py",
            HERE.parent / "ner/matcher.py")},
        "selection_seed": SEED, "selection": "One deterministic target per article, equal four-stratum allocation, independent SHA256 presentation order",
        "input_fields": list(INPUT_FIELDS), "source_split": "train", "case_kind": "real",
        "cleaning": "Existing sanitize_article plus standalone related-news boundary; no paraphrase or truncation; prior clean text must match",
        "company_context": "null; no business profile certified as available at publication",
        "labels_or_returns_in_model_input": False, "future_outcome_datasets_opened": False,
        "evaluation_is_model_input": False, "human_gold": False, "human_review_completed": 0,
        "limitations": [
            "Prior AI review is a fallible current-corporate-event selection proxy, not human economic-relevance gold.",
            "expected_label remains null: the old three-way event verdict is not forced into the new four-way economic label.",
            "This is a balanced development pilot, not an untouched validation/test set or population accuracy estimate.",
            "No price tables, returns, future labels, or old predictions are used; article text can contain contemporaneous market reporting.",
            "Original publication-time text authenticity and historical security/company identity are not certified.",
            "No model download, inference, GNN selection, training, or remote writes are performed by this preparer.",
        ],
        "files": {name: {"sha256": sha(out / name), "bytes": (out / name).stat().st_size}
                  for name in ("inputs.jsonl", "evaluation.jsonl", "provenance.jsonl")},
    }
    (out / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ARTIFACTS / "news_scale_source_20260916_50k_final/training_candidates.jsonl")
    parser.add_argument("--review-dir", type=Path, default=ARTIFACTS / "news_scale_review_20260916")
    parser.add_argument("--legacy-articles", type=Path, default=ARTIFACTS / "news_impact_v2/articles.jsonl")
    parser.add_argument("--legacy-metadata", type=Path, default=ARTIFACTS / "news_quality_source_20260915/legacy_metadata.jsonl")
    parser.add_argument("--out", type=Path, default=ARTIFACTS / "local_relevance_pilot_20260916_v1")
    parser.add_argument("--per-stratum", type=int, default=10)
    parser.add_argument("--max-body-characters", type=int, default=20000)
    args = parser.parse_args()
    summary = prepare(**vars(args))
    print(json.dumps({key: summary[key] for key in ("pairs", "strata", "years", "regions", "excluded_articles", "files")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
