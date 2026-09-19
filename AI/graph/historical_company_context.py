"""Resolve dated company facts without inferring continuous historical ownership.

Registry facts must be independently sourced by the registry author. This module
validates their structure and time boundaries; it does not judge news relevance,
consult current company descriptions, retrieve sources, or run a model.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


SCHEMA = "cosmos-historical-company-context-1"
MAX_AGE_DAYS = 400
RECORD_FIELDS = {"record_id", "ticker", "entity_id", "observed_at", "published_at",
                 "source_url", "display_name", "aliases", "business", "relationships",
                 "limitations", "not_before", "not_after"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def row_sha256(row):
    return sha256(canonical(row).encode("utf8"))


def nonempty(value, field):
    require(isinstance(value, str) and bool(value.strip()) and value == value.strip(),
            f"{field} must be nonempty text without outer whitespace")
    return value


def iso_day(value, field):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None,
            f"{field} must be YYYY-MM-DD")
    return date.fromisoformat(value)


def utc_midnight(value):
    return datetime.combine(value, datetime.min.time(), timezone.utc)


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def available_at(record):
    return utc_midnight(iso_day(record["published_at"], "published_at") + timedelta(days=1))


def validate_registry(registry):
    """Validate the entire inventory, including records unrelated to any input row."""
    require(isinstance(registry, dict) and set(registry) == {"schema", "records"}, "Invalid registry envelope")
    require(registry["schema"] == SCHEMA and isinstance(registry["records"], list), "Invalid registry schema")
    seen = set()
    for record in registry["records"]:
        require(isinstance(record, dict) and set(record) == RECORD_FIELDS, "Invalid registry record fields")
        for field in ("record_id", "ticker", "entity_id", "display_name"):
            nonempty(record[field], field)
        require(record["record_id"] not in seen, "Duplicate registry record_id")
        seen.add(record["record_id"])
        observed = iso_day(record["observed_at"], "observed_at")
        published = iso_day(record["published_at"], "published_at")
        require(observed <= published, "Observation cannot follow source publication")
        # Also rejects the date maximum, whose required next-day availability cannot be represented.
        available_at(record)
        url = nonempty(record["source_url"], "source_url")
        require(not any(c.isspace() or ord(c) < 32 for c in url), "Invalid source URL whitespace")
        parsed = urlsplit(url)
        require(parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
                and not parsed.password and parsed.port in (None, 443), "Source URL must be HTTPS without credentials")
        require(isinstance(record["business"], str), "business must be text")
        for field in ("aliases", "relationships", "limitations"):
            require(isinstance(record[field], list), f"{field} must be a list")
            for item in record[field]:
                nonempty(item, field)
            require(len(set(record[field])) == len(record[field]), f"Duplicate {field} value")
        bounds = {key: iso_day(record[key], key) if record[key] is not None else None
                  for key in ("not_before", "not_after")}
        require(bounds["not_before"] is None or bounds["not_after"] is None
                or bounds["not_before"] < bounds["not_after"], "Activity bounds must form a nonempty interval")
    canonical(registry)
    return registry


def validate_row(row):
    require(isinstance(row, dict), "Article must be an object")
    for key in ("article_id", "ticker", "published_at"):
        nonempty(row.get(key), key)
    for key in ("title", "body"):
        require(isinstance(row.get(key), str), f"Article {key} must be text")
    require(row.get("company_context") is None, "Article already contains company_context")
    published = row["published_at"]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
        article_at = utc_midnight(iso_day(published, "article published_at"))
    else:
        require("T" in published, "Article timestamp must be ISO with an explicit timezone")
        article_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
        require(article_at.tzinfo is not None and article_at.utcoffset() is not None,
                "Article timestamp must have an explicit timezone")
        article_at = article_at.astimezone(timezone.utc)
    canonical(row)
    return article_at


def _resolve(row, records, article_at):
    original = deepcopy(row)
    audit = {"article_id": row["article_id"], "ticker": row["ticker"],
             "article_published_at": row["published_at"], "article_at_utc": utc_text(article_at),
             "original_row_sha256": row_sha256(row), "context_added": False,
             "reason": None, "max_age_days": MAX_AGE_DAYS, "record_ids": [],
             "continuous_ownership_assumed": False}

    def finish(reason, result=original):
        audit["reason"] = reason
        audit["context_row_sha256"] = row_sha256(result)
        return result, audit

    candidates = [record for record in records if record["ticker"] == row["ticker"]]
    if not candidates:
        return finish("ticker_not_in_registry")
    published = [record for record in candidates if available_at(record) <= article_at]
    audit["unavailable_record_ids"] = sorted(record["record_id"] for record in candidates
                                               if available_at(record) > article_at)
    if not published:
        return finish("no_source_available_by_article")
    latest_publication = max(record["published_at"] for record in published)
    latest = [record for record in published if record["published_at"] == latest_publication]
    audit["latest_source_published_at"] = latest_publication
    audit["latest_publication_record_ids"] = sorted(record["record_id"] for record in latest)
    # Activity/age failure of this newest published cohort must never restore older records.
    if len({record["entity_id"] for record in latest}) != 1:
        return finish("ambiguous_latest_entity")
    latest_observation = max(record["observed_at"] for record in latest)
    latest = sorted((record for record in latest if record["observed_at"] == latest_observation),
                    key=lambda record: record["record_id"])
    audit["record_ids"] = [record["record_id"] for record in latest]
    audit["observed_at"] = latest_observation
    audit["entity_id"] = latest[0]["entity_id"]
    # Multiple URLs may independently attest the same fact; conflicting latest facts are refused.
    comparable = [{key: value for key, value in record.items() if key not in {"record_id", "source_url"}}
                  for record in latest]
    if len({canonical(record) for record in comparable}) != 1:
        return finish("conflicting_latest_records")
    record = latest[0]
    age = (article_at.date() - iso_day(record["observed_at"], "observed_at")).days
    audit["snapshot_age_days"] = age
    audit["activity_bounds"] = {key: record[key] for key in ("not_before", "not_after")}
    if age > MAX_AGE_DAYS:
        return finish("snapshot_expired")
    if record["not_before"] is not None and article_at < utc_midnight(iso_day(record["not_before"], "not_before")):
        return finish("before_confirmed_activity_boundary")
    if record["not_after"] is not None and article_at >= utc_midnight(iso_day(record["not_after"], "not_after")):
        return finish("at_or_after_confirmed_activity_end")
    names = list(dict.fromkeys([record["display_name"], *record["aliases"]]))
    relationships = [
        f"자료 관측일은 {record['observed_at']}, 공개일은 {record['published_at']}이다. "
        "해당 관측사실만 제공하며 이후 소유·사업 관계가 계속 유지되었다고 확인한 것은 아니다.",
        "해당 관측 자료에서 동일 법인으로 확인된 사명만 제공한다: " + "; ".join(names)
        + ". 유사 명칭이나 계열사를 같은 법인으로 간주하지 않는다.",
        *record["relationships"], *record["limitations"],
    ]
    if record["not_before"] is not None:
        relationships.append(f"확인된 법인 활동 시작 경계는 {record['not_before']}이다.")
    if record["not_after"] is not None:
        relationships.append(f"확인된 법인 활동 종료 경계는 {record['not_after']}이며 그 날짜는 적용 범위에서 제외한다.")
    result = deepcopy(row)
    result["company_name"] = record["display_name"]
    result["company_context"] = {"source": "\n".join(dict.fromkeys(r["source_url"] for r in latest)),
                                 "as_of": utc_text(available_at(record)), "business": record["business"],
                                 "relationships": relationships}
    audit["context_added"] = True
    audit["company_name_before"] = row.get("company_name")
    audit["company_name_after"] = result["company_name"]
    audit["as_of"] = result["company_context"]["as_of"]
    return finish("matched", result)


def resolve(row, registry):
    """Return a separate row and audit; never mutate either supplied argument."""
    validate_registry(registry)
    article_at = validate_row(row)
    return _resolve(row, registry["records"], article_at)


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Non-JSON constant: {value}")


def read_json(raw):
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_duplicate_keys,
                      parse_constant=_invalid_constant)


def company_coverage(raw, registry):
    """Current CSV identities are inventory only, never historical evidence."""
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    require(reader.fieldnames and "ticker" in reader.fieldnames, "Companies CSV requires ticker")
    require(len(set(reader.fieldnames)) == len(reader.fieldnames), "Duplicate CSV column")
    grouped = defaultdict(list)
    for record in registry["records"]:
        grouped[record["ticker"]].append(record)
    rows, seen = [], set()
    for company in reader:
        require(None not in company, "Malformed companies CSV row")
        ticker = nonempty(company.get("ticker"), "company ticker")
        require(ticker not in seen, "Duplicate companies CSV ticker")
        seen.add(ticker)
        records = grouped[ticker]
        rows.append({"ticker": ticker, "current_name_inventory_only": company.get("name_official", company.get("company_name")),
                     "registry_record_count": len(records), "entity_ids": sorted({r["entity_id"] for r in records}),
                     "observed_dates": sorted({r["observed_at"] for r in records}),
                     "published_dates": sorted({r["published_at"] for r in records}),
                     "status": "records_present_time_filter_still_required" if records else "no_registry_record"})
    return {"company_count": len(rows), "with_records": sum(r["registry_record_count"] > 0 for r in rows),
            "current_csv_name_is_historical_evidence": False,
            "limitations": "현재 CSV는 종목 목록 점검용이며 사명·소유·사업의 과거 증거가 아니다. 기록 존재는 모든 기사 시점의 적용 가능성을 뜻하지 않는다.",
            "companies": rows}


def _snapshot(path, snapshots):
    path = Path(path).resolve()
    raw = path.read_bytes()
    checksum = sha256(raw)
    require(path not in snapshots or snapshots[path] == checksum, "Input changed while reading")
    snapshots[path] = checksum
    return raw


def _unchanged(snapshots):
    for path, checksum in snapshots.items():
        require(sha256(path.read_bytes()) == checksum, f"Input changed before publication: {path}")


def _write_new(path, raw):
    with Path(path).open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def apply(*, inputs, registry, out, companies=None):
    snapshots = {}
    input_raw = _snapshot(inputs, snapshots)
    registry_raw = _snapshot(registry, snapshots)
    producer_sha = sha256(Path(__file__).read_bytes())
    data = validate_registry(read_json(registry_raw))
    rows = [read_json(line.encode("utf8")) for line in input_raw.decode("utf-8-sig").splitlines() if line.strip()]
    require(bool(rows), "Input JSONL is empty")
    contextual, audits, identities = [], [], set()
    for row in rows:
        at = validate_row(row)
        key = (row["article_id"], row["ticker"])
        require(key not in identities, "Duplicate article/ticker input")
        identities.add(key)
        result, audit = _resolve(row, data["records"], at)
        contextual.append(result)
        audits.append(audit)
    coverage = company_coverage(_snapshot(companies, snapshots), data) if companies is not None else None
    output_dir = Path(out).resolve()
    require(not output_dir.exists(), "Output must be a new directory; refusing to overwrite")
    output_raw = ("".join(canonical(row) + "\n" for row in contextual)).encode("utf8")
    coverage_raw = (json.dumps(coverage, ensure_ascii=False, indent=2) + "\n").encode("utf8") if coverage is not None else None
    files = {"context_inputs.jsonl": {"sha256": sha256(output_raw), "rows": len(contextual)}}
    if coverage_raw is not None:
        files["company_coverage.json"] = {"sha256": sha256(coverage_raw)}
    manifest = {"schema": "cosmos-historical-company-context-application-1", "complete": True,
                "registry_schema": SCHEMA, "max_age_days": MAX_AGE_DAYS,
                "inputs": {"path": str(Path(inputs).resolve()), "sha256": sha256(input_raw)},
                "registry": {"path": str(Path(registry).resolve()), "sha256": sha256(registry_raw)},
                "producer_sha256": producer_sha, "files": files,
                "records": len(rows), "context_added": sum(a["context_added"] for a in audits),
                "reason_counts": dict(sorted(Counter(a["reason"] for a in audits).items())), "rows": audits,
                "coverage": {"input_tickers": len({r["ticker"] for r in rows}),
                             "matched_tickers": len({a["ticker"] for a in audits if a["context_added"]}),
                             "companies_inventory": coverage and {key: coverage[key] for key in ("company_count", "with_records")}},
                "policy": {"source_date_availability": "next_day_00_00_UTC",
                           "article_date_availability": "date_only_articles_use_start_of_day_UTC",
                           "latest_order": "published_at_then_observed_at",
                           "fallback_after_latest_failure": False, "not_after_exclusive": True,
                           "ttl_is_legal_activity_interval": False, "continuous_ownership_assumed": False,
                           "semantic_adoption_performed": False, "current_csv_is_historical_evidence": False}}
    if companies is not None:
        manifest["companies"] = {"path": str(Path(companies).resolve()), "sha256": snapshots[Path(companies).resolve()]}
    _unchanged(snapshots)
    require(sha256(Path(__file__).read_bytes()) == producer_sha, "Producer changed during preparation")
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_new(output_dir / "context_inputs.jsonl", output_raw)
    if coverage_raw is not None:
        _write_new(output_dir / "company_coverage.json", coverage_raw)
    _unchanged(snapshots)
    for name, expected in files.items():
        require(sha256((output_dir / name).read_bytes()) == expected["sha256"], "Output verification failed")
    # Manifest publication is the completion marker. Failed partial outputs are not resumed or overwritten.
    _write_new(output_dir / "manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf8"))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--companies", type=Path)
    args = parser.parse_args()
    manifest = apply(inputs=args.inputs, registry=args.registry, out=args.out, companies=args.companies)
    print(json.dumps({"records": manifest["records"], "context_added": manifest["context_added"],
                      "reason_counts": manifest["reason_counts"], "out": str(args.out.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
