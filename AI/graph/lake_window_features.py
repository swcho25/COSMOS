"""Pure, observed-data features for canonical lake publication; no I/O or DB writes.

Every window is UTC [as_of - N days, as_of). Date-only observations become
available at the end of that date, matching relationship_score_components.
News/company counts deduplicate document IDs. Missing sentiment stays NULL;
zero means an observed, analyzed zero. Coverage describes observed mentions,
not completeness of the source archive (the publisher owns that manifest).

Price returns are fractions, using adjusted_close/adj_close only, with one prior observation as
an optional window anchor. Return dates must be inside the window. Volatility
is the sample standard deviation of simple returns, without annualization.
Correlation needs five aligned return dates and nonzero variance. Different
markets are deliberately not aligned until historical calendars are supplied.
Price features never contribute to relationship evidence support scores.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from collections import defaultdict

from relationship_score_components import WINDOWS, aggregate, normalize, timestamp

FEATURE_VERSION = "lake-window-v1"


def _stamp(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _number(value, field: str, low=None, high=None) -> float:
    result = float(value)
    if (not math.isfinite(result) or (low is not None and result < low)
            or (high is not None and result > high)):
        raise ValueError(f"Invalid {field}: {value}")
    return result


def _optional_number(value, field: str, low=None, high=None):
    return None if value is None or value == "" else _number(value, field, low, high)


def _documents(rows: list[dict], markets: dict, *, news: bool) -> dict:
    documents = {}
    for row in rows:
        ticker = _text(row["ticker"], "ticker")
        if ticker not in markets:
            continue
        key = ticker, _text(row["document_id"], "document_id")
        item = {"at": timestamp(row["published_at"], evidence=True)}
        if news:
            label = row.get("sentiment_label")
            label = _text(label, "sentiment_label").upper() if label not in (None, "") else None
            if label not in (None, "POSITIVE", "NEGATIVE", "NEUTRAL"):
                raise ValueError(f"Unsupported sentiment label: {label}")
            item.update(confidence=_number(row["confidence"], "confidence", 0, 1),
                        label=label, sentiment=_optional_number(row.get("sentiment_score"),
                                                               "sentiment_score", -1, 1))
        previous = documents.get(key)
        if previous:
            if previous["at"] != item["at"]:
                raise ValueError(f"Conflicting publication times for {key}")
            if news:
                # Duplicate mention spans may have different confidence. Sentiment
                # belongs to the document/company, so conflicting analyses fail.
                for field in ("label", "sentiment"):
                    if (previous[field] is not None and item[field] is not None
                            and previous[field] != item[field]):
                        raise ValueError(f"Conflicting {field} for {key}")
                    item[field] = item[field] if item[field] is not None else previous[field]
                item["confidence"] = max(previous["confidence"], item["confidence"])
        documents[key] = item
    grouped = defaultdict(list)
    for (ticker, _), item in sorted(documents.items()):
        grouped[ticker].append(item)
    return grouped


def _prices(rows: list[dict], markets: dict, end: dt.datetime) -> dict:
    observations = {}
    for row in rows:
        ticker = _text(row["ticker"], "ticker")
        if ticker not in markets:
            continue
        at = timestamp(row["date"], evidence=True)
        if at >= end:
            continue
        close = _number(row["close"], "close", 0)
        adjusted = row.get("adjusted_close")
        if adjusted in (None, ""):
            adjusted = row.get("adj_close")
        adjusted = _optional_number(adjusted, "adjusted_close", 0)
        if close == 0 or adjusted == 0:
            raise ValueError("Close prices must be positive")
        item = {"at": at, "close": close, "adjusted": adjusted,
                "volume": _optional_number(row.get("volume"), "volume", 0)}
        key = ticker, at.date()
        previous = observations.get(key)
        if previous is not None and previous != item:
            raise ValueError(f"Conflicting prices for {key}")
        observations[key] = item
    grouped = defaultdict(list)
    for (ticker, _), item in sorted(observations.items()):
        grouped[ticker].append(item)
    return grouped


def _price_window(observations: list[dict], start: dt.datetime, end: dt.datetime):
    present = [r for r in observations if start <= r["at"] < end]
    prior = [r for r in observations if r["at"] < start]
    anchor = prior[-1:] if present else []
    series = anchor + present
    result = {"price_observation_count": len(present), "price_return_observation_count": 0,
              "price_anchor_available": bool(anchor), "price_return": None,
              "price_volatility": None, "price_status": "no_observations",
              "last_close": present[-1]["close"] if present else None,
              "last_adjusted_close": present[-1]["adjusted"] if present else None,
              "last_price_date": present[-1]["at"].date().isoformat() if present else None}
    if not present:
        return result, {}
    if any(row["adjusted"] is None for row in series):
        result["price_status"] = "missing_adjusted_close"
        return result, {}
    returns = {after["at"].date().isoformat(): after["adjusted"] / before["adjusted"] - 1
               for before, after in zip(series, series[1:])}
    values = list(returns.values())
    result.update(price_return_observation_count=len(values),
                  price_status="ok" if values else "insufficient_observations",
                  price_return=present[-1]["adjusted"] / series[0]["adjusted"] - 1 if values else None,
                  price_volatility=statistics.stdev(values) if len(values) >= 2 else None)
    return result, returns


def _correlation(src, dst, markets, price_features, returns, window):
    result = {"price_correlation": None, "price_aligned_return_count": 0,
              "price_correlation_status": "cross_market_not_aligned"}
    if markets[src] != markets[dst]:
        return result
    if any(price_features[t, window]["price_status"] == "missing_adjusted_close" for t in (src, dst)):
        result["price_correlation_status"] = "missing_adjusted_close"
        return result
    left, right = returns[src, window], returns[dst, window]
    common = sorted(left.keys() & right.keys())
    result.update(price_aligned_return_count=len(common),
                  price_correlation_status="insufficient_aligned_returns")
    if len(common) < 5:
        return result
    x, y = [left[d] for d in common], [right[d] for d in common]
    mx, my = statistics.mean(x), statistics.mean(y)
    xx = math.fsum((v - mx) ** 2 for v in x)
    yy = math.fsum((v - my) ** 2 for v in y)
    if xx == 0 or yy == 0:
        result["price_correlation_status"] = "constant_returns"
        return result
    coefficient = math.fsum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(xx * yy)
    result.update(price_correlation=max(-1.0, min(1.0, coefficient)), price_correlation_status="ok")
    return result


def build_features(companies: list[dict], mentions: list[dict], relations: list[dict],
                   disclosures: list[dict], prices: list[dict], as_of: str) -> dict[str, list[dict]]:
    """Return company_features/company_metrics/relationship_features/relationship_scores.

    Company keys are (market, stock_code); stock_code is the input ticker.
    Every known company gets all three windows, including observed zero counts.
    Relationship rows exist only where evidence exists inside that window.
    Unknown tickers are excluded from this fixed company universe. Conflicting
    duplicate observations fail, rather than arbitrarily choosing an input row.
    """
    end = timestamp(as_of)
    markets = {}
    for company in companies:
        ticker = _text(company["ticker"], "ticker")
        market = _text(company["market"], "market")
        if ticker in markets and markets[ticker] != market:
            raise ValueError(f"Ambiguous ticker across markets: {ticker}")
        markets[ticker] = market
    if not markets:
        raise ValueError("Company universe is empty")
    news = _documents(mentions, markets, news=True)
    filings = _documents(disclosures, markets, news=False)
    observations = _prices(prices, markets, end)
    normalized = [normalize({**row, "published_date": row["published_at"]}, row["source"])
                  for row in relations]
    scores, _ = aggregate(normalized, markets, end)
    for score in scores:
        start = end - dt.timedelta(days=int(score["window_type"][:-1]))
        score.update(period_start=_stamp(start), period_end=_stamp(end))
    evidence = {}
    for item in normalized:
        src, dst, _ = item["key"]
        if src not in markets or dst not in markets or not end - dt.timedelta(days=90) <= item["at"] < end:
            continue
        key = (*item["key"], item["source"], item["document"])
        if key not in evidence or evidence[key]["confidence"] < item["confidence"]:
            evidence[key] = item
    price_features, returns = {}, {}
    for ticker in sorted(markets):
        for days in WINDOWS:
            key = ticker, f"{days}D"
            price_features[key], returns[key] = _price_window(observations[ticker],
                                                               end - dt.timedelta(days=days), end)
    output = {"company_features": [], "company_metrics": [],
              "relationship_features": [], "relationship_scores": scores}
    relation_counts = defaultdict(int)
    grouped_evidence = defaultdict(list)
    for item in evidence.values():
        grouped_evidence[item["key"]].append(item)
    for score in scores:
        src, dst, rel = score["source_stock_code"], score["target_stock_code"], score["relationship_type_code"]
        window = score["window_type"]
        start = end - dt.timedelta(days=int(window[:-1]))
        present = [r for r in grouped_evidence[src, dst, rel] if start <= r["at"] < end]
        features = dict(score)
        features.update(period_start=_stamp(start), period_end=_stamp(end), feature_version=FEATURE_VERSION)
        for source in ("NEWS", "DISCLOSURE"):
            items = [r for r in present if r["source"] == source]
            features[f"{source.lower()}_evidence_count"] = len(items)
            features[f"{source.lower()}_confidence_sum"] = math.fsum(r["confidence"] for r in items)
        features.update(_correlation(src, dst, markets, price_features, returns, window))
        output["relationship_features"].append(features)
        for ticker in (src, dst):
            relation_counts[ticker, window] += 1
    for ticker, market in sorted(markets.items()):
        for days in WINDOWS:
            window = f"{days}D"
            start = end - dt.timedelta(days=days)
            present = [r for r in news[ticker] if start <= r["at"] < end]
            labeled = [r for r in present if r["label"] is not None]
            scored = [r["sentiment"] for r in present if r["sentiment"] is not None]
            analyzed = sum(r["label"] is not None or r["sentiment"] is not None for r in present)
            metrics = {"market": market, "stock_code": ticker, "window_type": window,
                       "measured_at": _stamp(end), "period_start": _stamp(start), "period_end": _stamp(end),
                       "news_mention_count": len(present),
                       "disclosure_count": sum(start <= r["at"] < end for r in filings[ticker]),
                       "positive_count": sum(r["label"] == "POSITIVE" for r in labeled) if labeled else None,
                       "negative_count": sum(r["label"] == "NEGATIVE" for r in labeled) if labeled else None,
                       "neutral_count": sum(r["label"] == "NEUTRAL" for r in labeled) if labeled else None,
                       "sentiment_score": statistics.mean(scored) if scored else None,
                       "sentiment_analyzed_count": analyzed, "sentiment_labeled_count": len(labeled),
                       "sentiment_scored_count": len(scored),
                       "sentiment_coverage": analyzed / len(present) if present else None,
                       "relationship_count": relation_counts[ticker, window], "metric_version": FEATURE_VERSION}
            output["company_metrics"].append(metrics)
            output["company_features"].append({**metrics, **price_features[ticker, window],
                                                "feature_version": FEATURE_VERSION})
    return output
