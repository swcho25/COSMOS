"""Pure news normalization and literal relationship evidence for the data lake.

No snapshot/training selection is applied here. Callers preserve raw input, dedup
the cleaned rows, and assign document_id afterward. All offsets below index the
cleaned ``title + '\\n' + body`` in Python Unicode characters, with exclusive ends.
Only the existing Korean relation rules are supported; English mentions remain
available even when English relationship extraction is unsupported.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
import unicodedata
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from extract_relations import classify, find_pair
from news_content import sanitize_article


UTC = dt.timezone.utc
OFFSET_BASIS = "cleaned_title_newline_body"
DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
CLOCK = re.compile(r"(?:T|\s)\d{1,2}:\d{2}")
HANGUL = re.compile(r"[가-힣]")
SENT_BOUNDARY = re.compile(
    r"\n+|(?<![0-9])[.!?]+[\s\u00a0]+"
    r"|(?<=[다요음임죠])\.(?=[가-힣A-Za-z0-9])"
    r"|(?<=[a-z])\.(?=[A-Z])"
)
BAD_TIME_METHODS = {
    "source_epoch_placeholder", "date_only_invalid_time", "unparsed_or_missing",
}


def _string(value) -> str:
    return value if isinstance(value, str) else ""


def _day(value) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    value = _string(value).strip()
    if not DATE_ONLY.fullmatch(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _timestamp(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        stamp = value
    else:
        value = _string(value).strip()
        if not CLOCK.search(value):
            return None
        try:
            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return None
    try:
        return stamp.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _publication(row: dict, flags: list[str]):
    # A normalized midnight must not silently upgrade date-only source input.
    raw_day = _day(row.get("published_at_raw"))
    forced_date = (
        raw_day is not None or row.get("published_at_precision") == "date"
        or row.get("date_normalization_method") in BAD_TIME_METHODS
    )
    if forced_date:
        flags.append("source_time_precision_not_timestamp")
    else:
        for key in ("published_at", "published_at_raw"):
            stamp = _timestamp(row.get(key))
            if stamp is not None:
                return stamp, "timestamp", key

    for key in ("published_at", "published_at_raw"):
        value = row.get(key)
        if isinstance(value, dt.datetime) or CLOCK.search(str(value or "")):
            if _timestamp(value) is None:
                flags.append("publication_time_without_valid_timezone")
    # Prefer the source date field; a naive clock is never assigned a guessed zone.
    for key in ("published_date", "published_at_raw", "published_at"):
        value = row.get(key)
        day = _day(value)
        if day is None and (isinstance(value, dt.datetime) or CLOCK.search(str(value or ""))):
            day = _day(str(value).strip()[:10])
        if day is not None:
            flags.append("date_only_end_of_day")
            return dt.datetime.combine(day, dt.time.max, UTC), "date", key
    return None


def canonicalize_url(value) -> str | None:
    """Normalize HTTP URL identity without rewriting meaningful query values.

    Query order, duplicate keys, encoding and non-UTM parameters are retained.
    Only utm_* parameters and the fragment are removed. No HTTP/HTTPS or www
    equivalence is assumed, and embedded credentials are not accepted.
    """
    value = _string(value).strip()
    if not value or any(ch.isspace() for ch in value):
        return None
    try:
        url = urlsplit(value)
        scheme = url.scheme.lower()
        if scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            return None
        host = url.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        port = url.port
        if port is not None and (scheme, port) not in (("http", 80), ("https", 443)):
            host += f":{port}"
        query = "&".join(
            part for part in url.query.split("&")
            if not unquote_plus(part.partition("=")[0]).lower().startswith("utm_")
        )
        return urlunsplit((scheme, host, url.path or "/", query, ""))
    except (ValueError, UnicodeError):
        return None


def normalize_article(row: dict) -> dict | None:
    """Return a cleaned copy, or None when no text or publication date is usable.

    content_hash is SHA256 of NFC/whitespace-normalized cleaned body (title when
    body is empty). identity_candidate prefers canonical URL, then content_hash;
    these are dedup candidates, never an assertion that a final document exists.
    Existing metadata, including a caller-supplied document_id, is retained.
    """
    if not isinstance(row, dict):
        return None
    flags = [str(v) for v in (row.get("quality_flags") or [])] if isinstance(row.get("quality_flags"), (list, tuple)) else []
    publication = _publication(row, flags)
    if publication is None:
        return None
    title, body = _string(row.get("title")), _string(row.get("body"))
    cleaned = sanitize_article({"title": title, "body": body})
    title, body = cleaned["title"], cleaned["body"]
    if not title.strip() and not body.strip():
        return None
    if (title, body) != (_string(row.get("title")), _string(row.get("body"))):
        flags.append("article_text_sanitized")
    if not body.strip():
        flags.append("body_missing")
    canonical = None
    for key in ("canonical_url", "url", "original_url"):
        if row.get(key):
            canonical = canonicalize_url(row[key])
            if canonical is not None:
                break
            flags.append("invalid_" + key)
    if canonical is None:
        flags.append("canonical_url_missing")
    digest_text = " ".join(unicodedata.normalize("NFC", body if body.strip() else title).split())
    content_hash = hashlib.sha256(digest_text.encode("utf-8")).hexdigest()
    identity = (
        "url:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if canonical else "content:" + content_hash
    )
    stamp, precision, source = publication
    out = dict(row)
    out.update(
        title=title, body=body, canonical_url=canonical, content_hash=content_hash,
        identity_candidate=identity, published_at=stamp.isoformat().replace("+00:00", "Z"),
        published_date=stamp.date().isoformat(), published_at_precision=precision,
        published_at_source=source, quality_flags=sorted(set(flags)),
    )
    return out


def _sentences(text: str):
    """Bounded literal slices; trimming also adjusts offsets (unlike Spark v0)."""
    pos = 0
    for boundary in SENT_BOUNDARY.finditer(text):
        # Keep sentence punctuation in the quote, excluding the separating space.
        end = boundary.start()
        if text[end:end + 1] in ".!?":
            while end < boundary.end() and text[end] in ".!?":
                end += 1
        left, right = pos, end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            yield left, right, text[left:right]
        pos = boundary.end()
    left, right = pos, len(text)
    while left < right and text[left].isspace():
        left += 1
    while right > left and text[right - 1].isspace():
        right -= 1
    if left < right:
        yield left, right, text[left:right]


def analyze_article(row: dict, matcher, *, affiliate_pairs=()) -> dict:
    """Match cleaned text and return article/company aggregates and literal hits.

    affiliate_pairs is an iterable of ticker pairs derived from ownership facts.
    It prevents known affiliates from becoming COMPETE edges. With no supplied
    pairs the missing ownership check is explicit in analysis_flags. All company
    mentions are retained with confidence; relationship candidates require two
    distinct confirmed (>=1.0), non-ellipsis mentions and classifier confidence
    >=0.6. No publication-date, region, sports or training-cohort row gate applies.
    """
    title, body = _string(row.get("title")), _string(row.get("body"))
    text = title + "\n" + body
    result = matcher.match(title, body)
    common = {key: row.get(key) for key in ("record_id", "published_at", "published_date")}
    if row.get("document_id") is not None:
        common["document_id"] = row["document_id"]
    flags = []
    affiliates = {frozenset(pair) for pair in affiliate_pairs}
    if not affiliates:
        flags.append("affiliate_competition_filter_unavailable")
    spans, by_ticker = [], {}
    for mention in sorted(result.mentions, key=lambda m: (m.start, m.end, m.ticker)):
        start, end = mention.start, mention.end
        confidence = float(mention.confidence)
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)):
            flags.append("invalid_mention_offset")
            continue
        if not mention.ticker or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            flags.append("invalid_mention_identity_or_confidence")
            continue
        span = {
            "ticker": mention.ticker, "alias": mention.alias, "start": start, "end": end,
            "quote": text[start:end], "method": mention.method, "confidence": confidence,
        }
        spans.append(span)
        item = by_ticker.setdefault(mention.ticker, {
            **common, "ticker": mention.ticker, "name_official": mention.name_official,
            "n_mentions": 0, "first_pos": "title" if start < len(title) else "lead" if start < len(title) + 201 else "body",
            "confidence": 0.0, "method": set(), "aliases": set(), "spans": [],
            "is_sports": bool(result.is_sports), "offset_basis": OFFSET_BASIS,
        })
        item["n_mentions"] += 1
        item["confidence"] = max(item["confidence"], confidence)
        item["method"].add(mention.method)
        item["aliases"].add(mention.alias)
        item["spans"].append({k: v for k, v in span.items() if k != "ticker"})
    for item in by_ticker.values():
        item["method"] = "+".join(sorted(item["method"]))
        item["aliases"] = sorted(item["aliases"])

    relationships = []
    if not HANGUL.search(text):
        flags.append("relationship_language_unsupported")
    for sentence_start, sentence_end, sentence in _sentences(text):
        if not 10 <= len(sentence) <= 400:
            continue
        if not HANGUL.search(sentence):
            if any(s["start"] >= sentence_start and s["end"] <= sentence_end for s in spans):
                flags.append("relationship_language_unsupported")
            continue
        hits = [s for s in spans if s["start"] >= sentence_start and s["end"] <= sentence_end]
        # Reject lists even when the third company's mention was less confident.
        if len({s["ticker"] for s in hits}) != 2:
            continue
        selected = {}
        for span in hits:
            if span["confidence"] >= 1.0 and span["method"] != "ellipsis":
                selected.setdefault(span["ticker"], span)
        if len(selected) != 2:
            continue
        a, b = sorted(selected.values(), key=lambda s: s["start"])
        if a["end"] > b["start"]:
            continue
        aliases = a["quote"], b["quote"]
        positions = find_pair(sentence, *aliases)
        if positions != (a["start"] - sentence_start, b["start"] - sentence_start):
            flags.append("ambiguous_relation_alias_offset")
            continue
        decision = classify(sentence, *aliases)
        if decision is None:
            continue
        src_alias, dst_alias, rel_type, confidence, pattern = decision
        if confidence < 0.6:
            continue
        source, target = (a, b) if src_alias == aliases[0] else (b, a)
        if source["quote"] != src_alias or target["quote"] != dst_alias:
            flags.append("ambiguous_relation_direction")
            continue
        if rel_type == "COMPETE" and frozenset((source["ticker"], target["ticker"])) in affiliates:
            continue
        if rel_type in ("PARTNER", "COMPETE") and source["ticker"] > target["ticker"]:
            source, target = target, source
        relationships.append({
            **common, "src_ticker": source["ticker"], "dst_ticker": target["ticker"],
            "rel_type": rel_type, "confidence": float(confidence), "pattern": pattern,
            "sentence": sentence, "sentence_start": sentence_start, "sentence_end": sentence_end,
            "source_start": source["start"], "source_end": source["end"],
            "target_start": target["start"], "target_end": target["end"],
            "offset_basis": OFFSET_BASIS,
        })
    return {
        "mentions": list(by_ticker.values()), "relationships": relationships,
        "analysis_flags": sorted(set(flags)), "relationship_language_support": "ko_only",
    }
