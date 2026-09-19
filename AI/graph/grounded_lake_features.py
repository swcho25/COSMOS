"""Windowed lake features from reviewed, target-grounded KG observations.

This adapter deliberately preserves the existing measured-data feature formula,
but feeds *available_at* into it rather than guessing a timezone for raw dates.
The integrated graph's conservative publication availability and its source
record IDs are therefore retained exactly. Unknown mention/filing availability
is counted in coverage and excluded from time windows, never assigned to today.

Sentiment describes target-grounded financial wording; it is neither GNN price
direction nor economic relevance confidence. Pending/conflicting sentiment stays
NULL. Relationships use document support units, not a probability of truth; prices
do not enter those scores. Current-window graph edges and all-history assertions
are separate: an old contract assertion does not establish a current contract.

One hash-verified adjusted-price snapshot may be used, with explicit freshness and
basis metadata. Mixing adjusted histories from different snapshots is unsupported.
All sources are retrospective observations, not a certified historical backtest.
This module never writes HDFS, a service DB, or an existing artifact.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import uuid

from lake_window_features import build_features
from relationship_score_components import NATURAL_FIELDS, SCORE_FIELDS, TYPES, timestamp


FEATURE_VERSION = "grounded-lake-window-v1"
SENTIMENT_SCOPE = "target_grounded_context_financial_polarity"
SCORE_MEANING = "document_support_weight_not_prediction_probability"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _aware(value, field="available_at"):
    _require(isinstance(value, str) and len(value) > 10, field + " must have an explicit UTC offset")
    result = timestamp(value)
    return result.isoformat().replace("+00:00", "Z")


def _number(value, low=None, high=None):
    _require(not isinstance(value, bool), "Boolean cannot be a numeric observation")
    number = float(value)
    _require(math.isfinite(number) and (low is None or number >= low)
             and (high is None or number <= high), "Invalid numeric observation")
    return number


def _companies(rows):
    result = {}
    for company in rows:
        ticker, market = company.get("ticker"), company.get("market")
        _require(isinstance(ticker, str) and ticker.strip() == ticker and ticker,
                 "Invalid company ticker")
        _require(isinstance(market, str) and bool(market), "Missing company market")
        _require(ticker not in result, "Duplicate/ambiguous company ticker: " + ticker)
        result[ticker] = dict(company)
    _require(bool(result), "Empty company universe")
    return result


def _sentiment(row):
    status = row.get("sentiment_status")
    _require(isinstance(status, str) and bool(status), "Missing sentiment processing status")
    label, score = row.get("sentiment_label"), row.get("sentiment_score")
    probabilities = row.get("sentiment_probabilities")
    if status != "analyzed":
        _require(label is None and score is None and probabilities is None,
                 "Pending/conflicting sentiment must retain NULL values")
        return None, None
    _require(label in {"POSITIVE", "NEGATIVE", "NEUTRAL"} and score is not None,
             "Analyzed target sentiment requires a label and score")
    score = _number(score, -1, 1)
    if probabilities is not None:
        _require(isinstance(probabilities, dict)
                 and set(probabilities) == {"positive", "negative", "neutral"},
                 "Sentiment probabilities require three named categories")
        values = {key: _number(value, 0, 1) for key, value in probabilities.items()}
        _require(abs(math.fsum(values.values()) - 1) <= 1e-5, "Invalid sentiment probability sum")
        _require(abs(values["positive"] - values["negative"] - score) <= 1e-5,
                 "Sentiment score disagrees with probabilities")
        _require(values[label.lower()] >= max(values.values()) - 1e-7,
                 "Sentiment label disagrees with probabilities")
    return label, score


def normalize_canonical_mentions(rows):
    """Conservatively reduce source revisions to one document/company observation.

    Source edges must remain in a separate audit dataset. This operation never
    chooses the most confident revision or averages contradictory predictions.
    Any disagreement in availability, status, label, score or probabilities makes
    the canonical sentiment NULL. When all revision times are known the latest
    time bounds the combined observation; one unknown revision time makes the
    canonical time unknown. Counts and conflict reasons are returned for coverage.
    """
    groups = defaultdict(list)
    for row in rows:
        key = row.get("document_id"), row.get("ticker")
        _require(all(isinstance(value, str) and bool(value) for value in key),
                 "Missing canonical document/company identity")
        _sentiment(row)  # malformed source results must not become valid via dedup
        available = _aware(row["available_at"]) if row.get("available_at") else None
        groups[key].append({"document_id": key[0], "ticker": key[1], "available_at": available,
                            **{field: row.get(field) for field in ("sentiment_status", "sentiment_label",
                                      "sentiment_score", "sentiment_probabilities")}})
    output, report = [], Counter()
    for (document, ticker), items in sorted(groups.items()):
        report["input_rows"] += len(items)
        report["canonical_company_documents"] += 1
        report["duplicate_source_rows"] += len(items) - 1
        if len(items) > 1:
            report["multi_revision_company_documents"] += 1
        times = {item["available_at"] for item in items}
        sentiment_signatures = {_json({key: value for key, value in item.items()
                                       if key.startswith("sentiment_")}) for item in items}
        temporal_conflict, sentiment_conflict = len(times) > 1, len(sentiment_signatures) > 1
        conflict = temporal_conflict or sentiment_conflict
        if temporal_conflict:
            report["availability_conflict_groups"] += 1
        if sentiment_conflict:
            report["sentiment_conflict_groups"] += 1
        if conflict:
            report["canonical_revision_conflict_groups"] += 1
        available = None if None in times else max(times, key=timestamp)
        if available is None:
            report["unknown_availability_groups"] += 1
        result = dict(items[0])
        result.update(available_at=available, source_row_count=len(items),
                      canonical_revision_conflict=conflict,
                      source_sentiment_statuses_json=_json(sorted({item["sentiment_status"] for item in items})))
        if conflict:
            result.update(sentiment_status="canonical_revision_conflict", sentiment_label=None,
                          sentiment_score=None, sentiment_probabilities=None)
        output.append(result)
    for key in ("input_rows", "canonical_company_documents", "duplicate_source_rows",
                "multi_revision_company_documents", "availability_conflict_groups",
                "sentiment_conflict_groups", "canonical_revision_conflict_groups", "unknown_availability_groups"):
        report.setdefault(key, 0)
    return output, dict(report)


def _adapt_mentions(rows, companies, coverage, *, news):
    result = []
    kind = "news" if news else "disclosure"
    seen = {}
    for row in rows:
        coverage[kind + "_input_rows"] += 1
        ticker, document = row.get("ticker"), row.get("document_id")
        _require(ticker in companies, "Unknown observed company: " + str(ticker))
        _require(isinstance(document, str) and bool(document), "Missing observation document identity")
        label, score = _sentiment(row) if news else (None, None)
        if not row.get("available_at"):
            coverage[kind + "_unknown_availability_rows"] += 1
            continue
        available = _aware(row["available_at"])
        key = ticker, document
        value = (available, label, score)
        _require(key not in seen or seen[key] == value,
                 "Conflicting canonical document/company observations: " + str(key))
        seen[key] = value
        item = {"ticker": ticker, "document_id": document, "published_at": available}
        if news:
            # Presence is observed. This internal compatibility field never claims
            # calibrated entity-linking or relevance confidence in the lake output.
            item.update(confidence=1.0, sentiment_label=label, sentiment_score=score)
        result.append(item)
    coverage[kind + "_unique_timed_company_documents"] = len(seen)
    return result


def _adapt_relations(rows, companies):
    result, original, doc_times = [], [], {}
    for row in rows:
        src, dst, rel = row.get("src_ticker"), row.get("dst_ticker"), row.get("rel_type")
        _require(src in companies and dst in companies and src != dst, "Invalid relation endpoints")
        _require(rel in TYPES, "Invalid relation type")
        source = row.get("source_kind", row.get("source"))
        _require(source in {"NEWS", "DISCLOSURE"}, "Invalid relation source")
        record = row.get("record_id")
        _require(isinstance(record, str) and bool(record), "Missing frozen relation source record")
        available = _aware(row.get("available_at"))
        weight = _number(row.get("support_weight"), 0, 1)
        identity = source, record
        _require(identity not in doc_times or doc_times[identity] == available,
                 "One source record has inconsistent availability")
        doc_times[identity] = available
        result.append({"src_ticker": src, "dst_ticker": dst, "rel_type": rel,
                       "record_id": record, "published_at": available,
                       "confidence": weight, "source": source})
        original.append({**row, "source_kind": source, "available_at": available})
    return result, original


def _check_scores(actual, expected):
    fields = NATURAL_FIELDS + ["window_type"]
    def index(rows):
        result = {}
        for row in rows:
            key = tuple(row[name] for name in fields)
            _require(key not in result, "Duplicate expected/actual window score")
            result[key] = row
        return result
    left, right = index(actual), index(expected)
    _require(left.keys() == right.keys(), "Frozen integrated score keys differ")
    for key in left:
        for name in SCORE_FIELDS:
            a, b = left[key].get(name), right[key].get(name)
            if isinstance(a, (float, int)) and not isinstance(a, bool):
                _require(isinstance(b, (float, int)) and math.isfinite(b) and abs(a - b) <= 1.1e-6,
                         "Frozen integrated score differs: " + str((key, name)))
            else:
                _require(a == b, "Frozen integrated score differs: " + str((key, name)))


def _historical_relations(relations, companies, end, snapshot_id):
    groups = defaultdict(list)
    for row in relations:
        if timestamp(row["available_at"]) >= end:
            continue
        src, dst = row["src_ticker"], row["dst_ticker"]
        if row["rel_type"] in {"PARTNER", "COMPETE"}:
            src, dst = sorted((src, dst))
        groups[src, dst, row["rel_type"]].append(row)
    result = []
    for (src, dst, kind), rows in sorted(groups.items()):
        natural = dict(zip(NATURAL_FIELDS, (companies[src]["market"], src,
                                           companies[dst]["market"], dst, kind)))
        documents = {(row["source_kind"], row["record_id"]) for row in rows}
        result.append({**natural, "relationship_key": ":".join(natural[name] for name in NATURAL_FIELDS),
                       "snapshot_id": snapshot_id, "directed": kind in {"SUPPLY", "INVEST"},
                       "evidence_count": len(documents), "evidence_row_count": len(rows),
                       "news_evidence_count": sum(source == "NEWS" for source, _ in documents),
                       "disclosure_evidence_count": sum(source == "DISCLOSURE" for source, _ in documents),
                       "first_evidence_available_at": min((row["available_at"] for row in rows), key=timestamp),
                       "last_evidence_available_at": max((row["available_at"] for row in rows), key=timestamp),
                       "evidence_ids_json": _json(sorted({row["evidence_id"] for row in rows if row.get("evidence_id")})),
                       "current_contract_state": "not_established",
                       "temporal_scope": "assertions_available_before_as_of_all_history"})
    return result


def build_grounded_features(companies, mentions, relations, disclosures, prices, as_of,
                            *, run_id, provenance, expected_scores=None):
    """Return {datasets, coverage, snapshot_id} from compact normalized observations.

    Inputs use canonical document IDs for company observations; relation record_id
    remains the frozen integrated scoring identity. Root assembly separately audits
    canonical duplicate relation identities. Mentions require sentiment_status,
    sentiment_label/score (NULL when not analyzed), and optional named probabilities.
    Dates are explicit available_at; missing mention dates are reported and skipped.
    ``provenance`` must identify source files/snapshot hashes and price provenance.
    """
    _require(isinstance(run_id, str) and bool(run_id), "Missing run ID")
    _require(isinstance(provenance, dict) and bool(provenance), "Missing source provenance")
    stamp = _aware(as_of, "as_of")
    end = timestamp(stamp)
    universe = _companies(companies)
    coverage = Counter()
    news = _adapt_mentions(mentions, universe, coverage, news=True)
    filings = _adapt_mentions(disclosures, universe, coverage, news=False)
    adapted_relations, source_relations = _adapt_relations(relations, universe)
    prices = list(prices)
    for row in prices:
        _require(row.get("ticker") in universe, "Unknown price company")
        if row.get("market"):
            _require(row["market"] == universe[row["ticker"]]["market"], "Price market differs")
    data = build_features(list(universe.values()), news, adapted_relations, filings, prices, stamp)
    if expected_scores is not None:
        _check_scores(data["relationship_scores"], list(expected_scores))
    snapshot_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:cosmos:graph-snapshot:" + run_id))
    provenance_json = _json(provenance)
    metadata = {"run_id": run_id, "as_of_at": stamp, "coverage_complete": False,
                "source_provenance_json": provenance_json, "price_effect_established": False,
                "production_approved": False, "graph_quality_validated": False,
                "live_historical_collection_availability_verified": False}
    for name, rows in data.items():
        for row in rows:
            row.update(metadata)
            if "feature_version" in row:
                row["feature_version"] = FEATURE_VERSION
            if "metric_version" in row:
                row["metric_version"] = FEATURE_VERSION
            if name.startswith("company_"):
                row.update(sentiment_scope=SENTIMENT_SCOPE, is_stock_price_prediction=False,
                           news_count_scope="unique_canonical_documents_with_grounded_company_observations",
                           disclosure_count_scope="observed_reporting_company_documents")
            else:
                row.update(confidence_meaning=SCORE_MEANING,
                           score_meaning="window_document_support_not_price_impact",
                           support_identity_basis="frozen_integrated_source_record_id")
    for row in data["company_features"]:
        last = row["last_price_date"]
        row["price_calendar_days_since_last"] = (end.date() - dt.date.fromisoformat(last)).days if last else None
        row["price_basis"] = "single_verified_adjusted_snapshot" if prices else "no_price_source"
    data["graph_nodes"] = [{"market": company["market"], "stock_code": ticker,
                             "name": company.get("name_official", company.get("display_name", ticker)),
                             "name_en": company.get("name_en"), "sector": company.get("sector"),
                             "snapshot_id": snapshot_id, **metadata}
                            for ticker, company in sorted(universe.items())]
    data["graph_edges"] = [{**row, "snapshot_id": snapshot_id,
                             "relationship_key": ":".join(row[field] for field in NATURAL_FIELDS),
                             "temporal_scope": "evidence_inside_window_not_persistent_contract_state"}
                            for row in data["relationship_scores"]]
    data["graph_relationships"] = [{**row, **metadata} for row in _historical_relations(
        source_relations, universe, end, snapshot_id)]
    coverage.update(company_universe=len(universe), relation_input_rows=len(source_relations),
                    timed_news_after_or_at_as_of=sum(timestamp(row["published_at"]) >= end for row in news),
                    historical_relationships=len(data["graph_relationships"]),
                    window_relationship_rows=len(data["relationship_scores"]))
    for key in ("news_input_rows", "news_unknown_availability_rows", "disclosure_input_rows",
                "disclosure_unknown_availability_rows"):
        coverage.setdefault(key, 0)
    return {"datasets": data, "coverage": dict(coverage), "snapshot_id": snapshot_id}


def load_verified_price_snapshot(parquet_path, manifest_path, companies, as_of):
    """Read one immutable daily-price basis and retain 90 days plus prior anchors.

    Returns (compact_rows, provenance). Raw prices are retained only for display;
    returns/correlations require adjusted_close. Date-only daily closes are available
    at the end of their date, as in the original lake price-feature contract.
    """
    import pandas as pd

    path, manifest_path = Path(parquet_path), Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    digest = _sha(path)
    _require(digest == manifest.get("sha256"), "Price snapshot checksum differs")
    if "bytes" in manifest:
        _require(path.stat().st_size == manifest["bytes"], "Price snapshot byte count differs")
    universe = _companies(companies)
    end = timestamp(_aware(as_of, "as_of"))
    start = end - dt.timedelta(days=90)
    frame = pd.read_parquet(path)
    _require(len(frame) == manifest.get("counts", {}).get("rows"), "Price manifest row count differs")
    wanted = {"ticker", "market", "trading_at", "interval_type", "close_price", "adj_close", "trading_volume"}
    _require(wanted <= set(frame.columns), "Price schema lacks required fields")
    by_ticker = defaultdict(dict)
    skipped_nonpositive = Counter()
    for raw in frame[list(wanted)].to_dict("records"):
        ticker = raw["ticker"]
        if ticker not in universe:
            continue
        _require(raw["market"] == universe[ticker]["market"], "Snapshot price market differs")
        _require(raw["interval_type"] == "1d", "Snapshot contains non-daily prices")
        day = str(raw["trading_at"])
        _require(len(day) == 10, "Daily price date must be YYYY-MM-DD")
        at = timestamp(day, evidence=True)
        if at >= end:
            continue
        close = _number(raw["close_price"], 0)
        adjusted = None if pd.isna(raw["adj_close"]) else _number(raw["adj_close"], 0)
        # pykrx includes zero OHLC placeholders on halted dates. They are not
        # observed closes and cannot generate -100% or divide-by-zero returns.
        if close == 0 or adjusted == 0:
            skipped_nonpositive[ticker] += 1
            continue
        volume = None if pd.isna(raw["trading_volume"]) else _number(raw["trading_volume"], 0)
        row = {"ticker": ticker, "market": raw["market"], "date": day,
               "close": close, "adjusted_close": adjusted, "volume": volume}
        old = by_ticker[ticker].get(day)
        _require(old is None or old == row, "Conflicting duplicate daily price")
        by_ticker[ticker][day] = row
    rows = []
    for ticker in sorted(by_ticker):
        series = sorted(by_ticker[ticker].values(), key=lambda row: row["date"])
        prior = [row for row in series if timestamp(row["date"], evidence=True) < start]
        rows.extend(prior[-1:])
        rows.extend(row for row in series if timestamp(row["date"], evidence=True) >= start)
    latest = {ticker: max(values) for ticker, values in sorted(by_ticker.items()) if values}
    provenance = {"snapshot_id": manifest.get("snapshot_id"), "parquet_sha256": digest,
                  "manifest_sha256": _sha(manifest_path), "retained_rows": len(rows),
                  "latest_date_by_ticker": latest,
                  "nonpositive_placeholder_rows_excluded": dict(skipped_nonpositive),
                  "basis": "single_adjusted_snapshot_no_cross_snapshot_filling",
                  "availability_policy": "daily_close_end_of_utc_date_display_features_only",
                  "historical_adjustment_availability_verified": False,
                  "is_point_in_time_backtest_source": False}
    return rows, provenance
