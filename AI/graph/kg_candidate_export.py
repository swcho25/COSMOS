"""Pure contracts for the all-year KG candidate export; no model or Spark imports.

Reuse the existing widget/sidebar sanitizer, not the lake date normalizer: graph
relevance does not require a known timestamp and must not invent an end-of-day
publication instant. Source date/URL values are retained verbatim in provenance.
Only byte-identical canonical seven-field inputs may be deduplicated. No price,
training split, sports, confidence, keyword, year or fixed company filter applies.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

from news_content import sanitize_article
from knowledge_graph_news_preview import canonical, canonical_input_sha256, validate_input

SCHEMA = "cosmos-kg-candidate-export-1"
NEWS = "/datasets/news/snapshots/20260908-prepared/news"
MENTIONS = "/data-lake/sandbox/news/junwoo/company-mentions/model_version=dict-v1.3"
RUNS = {r: f"{MENTIONS}/run_id=dict-v1.3-{r}-20260911-0655" for r in ("domestic", "overseas")}
OUTPUT_ROOT = "/data-lake/sandbox/news/junwoo/kg-reselection/"
SOURCE_FIELDS = ("record_id", "title", "body", "published_at_raw", "published_at", "published_date",
                 "url", "canonical_url", "source_uri", "source_dataset", "source_record_id",
                 "published_timezone", "date_normalization_method", "collected_at", "region", "year",
                 "month", "publisher", "language", "text_eligible", "is_body_representative")


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def text(value, field, *, optional=False):
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise ValueError("Expected text: " + field)
    return value


def first_text(row, fields):
    for field in fields:
        value = row.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError("Expected text or null: " + field)
        if isinstance(value, str) and value.strip():
            return value, field
    return None, None


def normalize_source(row):
    """Return canonical article fields and lossless metadata references.

    Whitespace, dates and URL query strings are not normalized. Null source text
    maps to an empty string and is recorded in provenance. No article is silently
    discarded here, including missing dates, long bodies and older/newer years.
    """
    identity = text(row.get("record_id"), "record_id")
    if not identity.strip():
        raise ValueError("Empty record_id")
    title, body = text(row.get("title"), "title", optional=True), text(row.get("body"), "body", optional=True)
    cleaned = sanitize_article({"title": title, "body": body})
    published, publication_field = first_text(row, ("published_at_raw", "published_at", "published_date"))
    url, url_field = first_text(row, ("url", "canonical_url", "source_uri"))
    article = dict(article_id=identity, title=cleaned["title"], body=cleaned["body"],
                   published_at=published, source_url=url)
    provenance = {key: row.get(key) for key in SOURCE_FIELDS if key not in ("title", "body")}
    provenance.update(source_path=row.get("source_path"), raw_title_sha256=digest(title), raw_body_sha256=digest(body),
                      source_title_was_null=row.get("title") is None, source_body_was_null=row.get("body") is None,
                      publication_field=publication_field, url_field=url_field,
                      canonical_article_sha256=digest(article), sanitizer="news_content.sanitize_article",
                      text_changed=(title, body) != (cleaned["title"], cleaned["body"]),
                      original_text_preserved_in_frozen_source=True)
    return article, provenance


def candidate(article, mention):
    ticker = text(mention.get("ticker"), "ticker")
    name = text(mention.get("name_official"), "name_official")
    if mention.get("record_id") != article["article_id"]:
        raise ValueError("Mention identity differs from article")
    row = {**article, "ticker": ticker, "company_name": name}
    validate_input(row)
    return row, dict(article_id=row["article_id"], ticker=ticker,
                     input_sha256=canonical_input_sha256(row), canonical_article_sha256=digest(article))


def validate_output_path(value):
    uri = urlsplit(value)
    if uri.scheme not in ("", "hdfs") or uri.netloc not in ("", "localhost:9000") or uri.query or uri.fragment:
        raise ValueError("Output must be a new HDFS trial path")
    path = uri.path
    if (not path.startswith(OUTPUT_ROOT) or path == OUTPUT_ROOT or
            any(part in ("", ".", "..") for part in path[len(OUTPUT_ROOT):].split("/"))):
        raise ValueError("Output must stay under " + OUTPUT_ROOT)
    return path


def inventory_fingerprint(entries):
    ordered = sorted(entries, key=lambda x: x["path"])
    if len({x["path"] for x in ordered}) != len(ordered):
        raise ValueError("Duplicate source inventory path")
    for item in ordered:
        if (set(item) != {"path", "bytes", "modified_ms", "checksum"} or
                not isinstance(item["path"], str) or not item["path"] or
                type(item["bytes"]) is not int or item["bytes"] < 0 or
                type(item["modified_ms"]) is not int or not isinstance(item["checksum"], str) or not item["checksum"]):
            raise ValueError("Invalid frozen source inventory")
    return digest(ordered)


def assert_counts(counts):
    """Never label a reduced join as a complete all-candidate export."""
    for key in ("invalid_mention_rows", "unresolved_company_name_rows", "missing_source_article_ids",
                "ambiguous_source_article_ids", "ambiguous_pair_names", "empty_cleaned_articles"):
        if counts.get(key, 0):
            raise ValueError(f"Full export blocked: {key}={counts[key]}")
    if counts["exported_unique_pairs"] != counts["mention_unique_pairs"]:
        raise ValueError("Export does not cover every unique source mention pair")


def source_map(rows):
    """Spark mapPartitions returning only strings; bounded to one article at once."""
    for value in rows:
        row = value.asDict(recursive=True) if hasattr(value, "asDict") else value
        article, provenance = normalize_source(row)
        yield (article["article_id"], digest(article), canonical(article), canonical(provenance),
               not bool(article["title"].strip() or article["body"].strip()))


def candidate_map(rows):
    for value in rows:
        row = value.asDict(recursive=True) if hasattr(value, "asDict") else value
        article = json.loads(row["article_json"])
        item, provenance = candidate(article, row)
        yield (item["article_id"], item["ticker"], provenance["input_sha256"], canonical(item), canonical(provenance))
