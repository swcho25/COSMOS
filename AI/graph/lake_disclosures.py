"""Normalize observed DART/SEC filings without inventing corporate relationships.

DART reuses the existing largest-shareholder parser only for periodic reports.
Evidence is a literal seven-cell substring of an original files[].text value,
with offsets into that file. Collapsed tables/matrices are never reconstructed.
SEC currently supplies filing/company observations, not relationship evidence.

Document identities depend only on the filing authority and stable filing ID.
These UUID namespace/name rules are a permanent data contract: never change
them merely because an extractor version, ticker or input path changes.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import uuid
from collections import defaultdict

from extract_ownership import NUM, norm_name, parse_holders
from relationship_score_components import timestamp

DOCUMENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:cosmos:disclosure-document")
HEADINGS = ("최대주주 및 그 특수관계인의 주식소유 현황", "최대주주 및 특수관계인의 주식소유 현황")
PERIODIC_REPORT = re.compile(r"^(?:\[[^\]]+\]\s*)*(?:사업|반기|분기)보고서(?:\s|\(|$)")


def _identifier(value, digits: int) -> str | None:
    value = str(value or "").strip()
    if value.endswith(".0"):
        value = value[:-2]
    if not re.fullmatch(r"\d{1," + str(digits) + r"}", value) or int(value) == 0:
        return None
    return value.zfill(digits)


def _day(value) -> dt.date | None:
    value = str(value or "").strip()
    if re.fullmatch(r"\d{8}", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _end_of_day(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.max, dt.timezone.utc)


def _stamp(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _path(report: dict):
    return report.get("source_path") or report.get("_source_path")


def _document_id(source: str, filing_id: str) -> str:
    return str(uuid.uuid5(DOCUMENT_NAMESPACE, f"{source}:{filing_id}"))


def _holder_aliases(companies: list[dict], aliases: list[dict]) -> dict:
    tickers = {str(c["ticker"]) for c in companies}
    names = defaultdict(set)
    for company in companies:
        name = norm_name(str(company.get("name_official") or ""))
        if name:
            names[name].add(str(company["ticker"]))
    for alias in aliases:
        ticker = str(alias.get("ticker") or "")
        name = norm_name(str(alias.get("alias") or ""))
        if name and ticker in tickers and alias.get("ambiguity") == "safe":
            names[name].add(ticker)
    return {name: next(iter(tickers)) for name, tickers in names.items() if len(tickers) == 1}


def _table_segment(text: str):
    # Match the exact heading selection and maximum extent of parse_holders.
    start = text.find(HEADINGS[0])
    if start < 0:
        start = text.find(HEADINGS[1])
    if start < 0:
        return None
    return start, text[start:start + 20000]


def _literal_row(text: str, holder: dict):
    """Locate the parser's 3 text + 4 numeric cells without recreating any text."""
    segment = _table_segment(text)
    if segment is None:
        return None
    base, value = segment
    cells = []
    for match in re.finditer(r"[^|\n]+", value):
        cell = match.group().strip()
        if cell:
            offset = len(match.group()) - len(match.group().lstrip())
            cells.append((cell, base + match.start() + offset,
                          base + match.start() + offset + len(cell)))
    for i in range(len(cells) - 6):
        row = cells[i:i + 7]
        if [r[0] for r in row[:3]] != [holder["holder_name"], holder["relation"], holder["share_kind"]]:
            continue
        if not all(NUM.fullmatch(r[0].replace("△", "-")) for r in row[3:]):
            continue
        if float(row[6][0].replace(",", "")) != holder["share_pct_end"]:
            continue
        start, end = row[0][1], row[-1][2]
        # A gigantic row is not a short, safely inspectable evidence passage.
        if end - start <= 1000:
            return {"evidence_text": text[start:end], "evidence_start": start, "evidence_end": end}
    return None


def normalize_dart(report: dict, companies: list[dict], aliases: list[dict], *, as_of: str) -> dict | None:
    """Return a filing document plus recognized INVEST evidence, or reject identity/time.

    Unsupported periodic parsing leaves the filing document intact with an
    explicit status. Invalid receipt IDs/dates, unknown filers and future
    publications return None. The caller applies the lower 90-day boundary.
    """
    cutoff = timestamp(as_of)
    receipt = str(report.get("rcept_no") or "").strip()
    day = _day(report.get("rcept_dt"))
    if not re.fullmatch(r"\d{14}", receipt) or day is None or receipt[:8] != day.strftime("%Y%m%d"):
        return None
    published = _end_of_day(day)
    if published >= cutoff:
        return None
    code = _identifier(report.get("corp_code"), 8)
    candidates = {str(c["ticker"]) for c in companies
                  if code is not None and _identifier(c.get("dart_corp_code"), 8) == code}
    if len(candidates) != 1:
        return None
    filer = next(iter(candidates))
    name = str(report.get("report_nm") or "").strip()
    document = {"document_id": _document_id("DART", receipt), "ticker": filer,
                "published_at": _stamp(published), "published_at_precision": "date",
                "source": "DART", "report_id": receipt, "report_name": name,
                "source_path": _path(report), "ownership_parse_status": "not_supported_report"}
    result = {"document": document, "relationships": []}
    if not PERIODIC_REPORT.match(name):
        return result
    lookup = _holder_aliases(companies, aliases)
    valid_table = False
    invalid_accounting = False
    seen = set()
    files = report.get("files") or []
    for index, file in enumerate(files):
        if not isinstance(file, dict) or not isinstance(file.get("text"), str):
            continue
        text = file["text"]
        accounting, holders = parse_holders(text)
        if not holders:
            continue
        accounting_day = _day(accounting)
        if accounting_day is None or accounting_day > day:
            invalid_accounting = True
            continue
        valid_table = True
        for holder in holders:
            percentage = holder["share_pct_end"]
            source = lookup.get(norm_name(holder["holder_name"]))
            if (holder["share_kind"] != "보통주" or percentage is None
                    or not math.isfinite(percentage) or not 0 < percentage <= 100
                    or not source or source == filer):
                continue
            literal = _literal_row(text, holder)
            if literal is None:
                continue
            # Duplicate copies of the same table in attachments are one fact.
            identity = source, filer, holder["relation"], percentage, accounting
            if identity in seen:
                continue
            seen.add(identity)
            result["relationships"].append({"record_id": document["document_id"],
                "published_at": document["published_at"], "src_ticker": source, "dst_ticker": filer,
                "rel_type": "INVEST", "confidence": 1.0, "source": "DISCLOSURE",
                "share_pct": percentage, "as_of_date": accounting, "report_id": receipt,
                "source_path": _path(report), "holder_name": holder["holder_name"],
                "holder_relation": holder["relation"], "evidence_file_index": index,
                "evidence_filename": file.get("filename"), "evidence_offset_basis": "file_text", **literal})
    if result["relationships"]:
        status = "parsed"
    elif valid_table:
        status = "parsed_no_universe_holder"
    elif invalid_accounting:
        status = "invalid_accounting_date"
    else:
        status = "no_recognized_table"
    document["ownership_parse_status"] = status
    return result


def normalize_sec(report: dict, companies: list[dict], *, as_of: str) -> list[dict]:
    """Return one document/company observation per CIK AND reported-symbol match.

    Acceptance timestamps must carry a timezone. Otherwise filingDate is the
    conservative date-only fallback; reportDate is never used as availability.
    Multiple shares from one CIK retain one stable filing UUID across tickers.
    """
    cutoff = timestamp(as_of)
    raw_accession = str(report.get("accessionNumber") or "").strip()
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}|\d{18}", raw_accession):
        return []
    accession = raw_accession.replace("-", "")
    accession_display = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"
    cik = _identifier(report.get("cik"), 10)
    symbols = report.get("symbols") or []
    if not isinstance(symbols, (list, tuple)):
        return []
    symbols = {s.strip().upper() for s in symbols if isinstance(s, str) and s.strip()}
    tickers = sorted({str(c["ticker"]) for c in companies
                      if cik is not None and _identifier(c.get("cik"), 10) == cik
                      and str(c["ticker"]).upper() in symbols})
    if not tickers:
        return []
    accepted = report.get("acceptanceDateTime")
    published = None
    if isinstance(accepted, str) and re.search(r"[T ]\d{2}:\d{2}", accepted):
        try:
            published = timestamp(accepted)
        except (ValueError, OverflowError):
            pass
    if published is None:
        filing_day = _day(report.get("filingDate"))
        if filing_day is None:
            return []
        published = _end_of_day(filing_day)
        precision = "date"
        time_status = "filing_date_end_of_day" if not accepted else "filing_date_end_of_day_invalid_acceptance"
    else:
        precision = "timestamp"
        time_status = "acceptance_timestamp"
    if published >= cutoff:
        return []
    return [{"document_id": _document_id("SEC", accession), "ticker": ticker,
             "published_at": _stamp(published), "published_at_precision": precision,
             "publication_time_status": time_status, "source": "SEC", "report_id": accession_display,
             "report_name": str(report.get("form") or ""), "source_path": _path(report),
             "relationship_extraction_status": "not_supported_sec_relationship_extraction"}
            for ticker in tickers]
