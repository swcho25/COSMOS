"""Period/source-specific relationship evidence support, independent of GNN impact.

rel-v0.3-components uses the SAME bounded support formula for NEWS and DISCLOSURE:
100 * (1 - exp(-sum(per-document confidence) / 5)). Five confidence-1 documents
score 63.212056. This is a transparent display heuristic, not a calibrated probability
or measured price impact. Ownership percentage is NOT comparable to article counts,
so it is not used as the disclosure score. Price/sector signals are not mixed in.

Windows are [as_of - N calendar days, as_of), UTC. Date-only evidence is available at
the END of that UTC date; no accounting date is substituted for DART receipt date.
Old disclosures expire from a window. Persistent ownership is a different policy.
Each (relationship type, oriented pair, source, document ID) contributes once, using
the maximum sentence confidence. PARTNER/COMPETE are canonicalized; SUPPLY/INVEST
remain directed. Components are NULL only when no evidence exists (zero confidence
is a real zero). Missing both sources omits the row; one source uses its own score.

Input must cover the full 90-day extraction, not the local rel_hits inspection sample.
Malformed evidence fails before writing output; unknown companies are reported and
skipped. Separate exports are required for each completed batch, including empty files
when extraction succeeded with no hits. Missing files are errors, not empty evidence.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import re
from collections import defaultdict
from pathlib import Path

UTC = dt.timezone.utc
WINDOWS = (7, 30, 90)
FORMULA_VERSION = "rel-v0.3-components"
TYPES = {"SUPPLY", "INVEST", "PARTNER", "COMPETE"}
NATURAL_FIELDS = ["source_market", "source_stock_code", "target_market",
                  "target_stock_code", "relationship_type_code"]
SCORE_FIELDS = NATURAL_FIELDS + ["window_type", "news_score", "disclosure_score", "score",
    "impact_direction", "confidence", "evidence_count", "formula_version", "as_of_at"]


def timestamp(value: str, *, evidence: bool = False) -> dt.datetime:
    value = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        result = dt.datetime.combine(dt.date.fromisoformat(value), dt.time(), UTC)
        # Availability is represented by the final microsecond of the date.
        return result + dt.timedelta(days=1, microseconds=-1) if evidence else result
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("시간을 지정할 때는 UTC offset이 필요합니다")
    return result.astimezone(UTC)


def json_rows(path: Path):
    if not path.exists():
        raise FileNotFoundError(path)
    files = sorted(path.glob("part-*")) if path.is_dir() else [path]
    if path.is_dir() and not (path / "_SUCCESS").exists():
        raise ValueError(f"완료되지 않은 Spark export: {path}")
    for file in files:
        if not file.is_file():
            continue
        with file.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def normalize(row: dict, source: str) -> dict:
    if source not in {"NEWS", "DISCLOSURE"}:
        raise ValueError("지원하지 않는 근거 출처")
    for field in ("src_ticker", "dst_ticker", "record_id"):
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"{field}는 비어 있지 않은 문자열이어야 합니다")
    src, dst = str(row["src_ticker"]).strip(), str(row["dst_ticker"]).strip()
    rel = row["rel_type"]
    if rel not in TYPES or not src or not dst or src == dst:
        raise ValueError(f"잘못된 관계: {src}/{dst}/{rel}")
    if rel in {"PARTNER", "COMPETE"}:
        src, dst = sorted((src, dst))
    document = str(row["record_id"]).strip()
    if not document:
        raise ValueError("빈 근거 ID")
    confidence = float(row["confidence"])
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("confidence는 0~1 유한수여야 합니다")
    return {"key": (src, dst, rel), "source": source, "document": document,
            "at": timestamp(row["published_date"], evidence=True), "confidence": confidence}


def ownership_evidence(path: Path):
    """Adapt current ownership edges. rcept_no is publication; as_of_date is accounting.

    An explicit disclosure JSONL is preferable when historical filings are available.
    The latest-only ownership CSV cannot reconstruct past filing histories.
    """
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            receipt = re.search(r"(?:^|;)dart:(\d{14})(?:;|$)", row["evidence"])
            if not receipt:
                raise ValueError("DART 접수번호 없는 지분 근거: 날짜를 임의 추정하지 않습니다")
            rid = receipt[1]
            day = dt.datetime.strptime(rid[:8], "%Y%m%d").date().isoformat()
            yield {"src_ticker": row["src_ticker"], "dst_ticker": row["dst_ticker"],
                   "rel_type": "INVEST", "record_id": rid, "published_date": day,
                   "confidence": 1.0}


def component(documents: list[dict]) -> float | None:
    if not documents:
        return None
    return round(100 * -math.expm1(-math.fsum(d["confidence"] for d in documents) / 5), 6)


def blend(news: float | None, disclosure: float | None) -> float | None:
    if news is None:
        return disclosure
    if disclosure is None:
        return news
    return round((news + disclosure) / 2, 6)


def aggregate(evidence, markets: dict, as_of: dt.datetime, overrides=None):
    docs = {}
    unknown = set()
    for item in evidence:
        src, dst, rel = item["key"]
        if src not in markets or dst not in markets:
            unknown.add(item["key"])
            continue
        if not as_of - dt.timedelta(days=90) <= item["at"] < as_of:
            continue
        key = (*item["key"], item["source"], item["document"])
        previous = docs.get(key)
        if previous and previous["at"] != item["at"]:
            raise ValueError(f"동일 문서의 발행 시각 불일치: {key}")
        if previous is None or previous["confidence"] < item["confidence"]:
            docs[key] = item
    groups = defaultdict(list)
    for item in docs.values():
        if as_of - dt.timedelta(days=90) <= item["at"] < as_of:
            groups[item["key"]].append(item)
    rows = []
    for (src, dst, rel), items in sorted(groups.items()):
        for days in WINDOWS:
            present = [d for d in items if d["at"] >= as_of - dt.timedelta(days=days)]
            news = component([d for d in present if d["source"] == "NEWS"])
            disclosure = component([d for d in present if d["source"] == "DISCLOSURE"])
            score = blend(news, disclosure)
            if score is None:
                continue
            rows.append(dict(zip(NATURAL_FIELDS, [markets[src], src, markets[dst], dst, rel])) | {
                "window_type": f"{days}D", "news_score": news, "disclosure_score": disclosure,
                "score": score, "impact_direction": (overrides or {}).get((tuple(sorted((src, dst))), rel)),
                "confidence": round(math.fsum(d["confidence"] for d in present) / len(present), 6),
                "evidence_count": len(present), "formula_version": FORMULA_VERSION,
                "as_of_at": as_of.isoformat().replace("+00:00", "Z")})
    return rows, sorted(unknown)


def build_seed(args, out: Path):
    from itertools import chain
    from build_relationship_seed import read_csv, RELATIONSHIP_TYPES, MODEL_VERSION, HDFS_URI
    as_of = timestamp(args.as_of)
    markets = {r["ticker"]: r["market"] for r in read_csv(args.companies)}
    if not markets:
        raise ValueError("기업 사전이 비어 있습니다")
    overrides = {(tuple(sorted((r["source_stock_code"], r["target_stock_code"]))),
                  r["relationship_type_code"]): r["impact_direction"]
                 for r in read_csv(args.impact_override)}
    disclosure = (json_rows(args.disclosure_evidence) if args.disclosure_evidence
                  else ownership_evidence(args.ownership))
    evidence = chain((normalize(r, "NEWS") for r in json_rows(args.news_hits)),
                     (normalize(r, "DISCLOSURE") for r in disclosure))
    rows, unknown = aggregate(evidence, markets, as_of, overrides)
    relationships = {tuple(r[f] for f in NATURAL_FIELDS): {f: r[f] for f in NATURAL_FIELDS} for r in rows}
    out.mkdir(parents=True, exist_ok=True)
    def write(name, fields, values):
        with (out / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(values)  # None -> unquoted empty field -> PostgreSQL NULL
    stamp = as_of.isoformat().replace("+00:00", "Z")
    write("relationship_type.csv", ["code", "name", "directionality"],
          [dict(zip(("code", "name", "directionality"), r)) for r in RELATIONSHIP_TYPES])
    write("graph_snapshot.csv", ["as_of_at", "formula_version", "model_version", "status", "hdfs_uri", "published_at"],
          [{"as_of_at": stamp, "formula_version": FORMULA_VERSION, "model_version": MODEL_VERSION,
            "status": "PUBLISHED", "hdfs_uri": HDFS_URI, "published_at": stamp}])
    write("company_relationship.csv", NATURAL_FIELDS, relationships.values())
    write("relationship_score_current.csv", SCORE_FIELDS, rows)
    loader = Path(__file__).with_name("load_relationship_components.sql")
    (out / "load_relationships.sql").write_text(loader.read_text(encoding="utf-8"), encoding="utf-8")
    summary = {"formula_version": FORMULA_VERSION, "as_of_at": stamp, "rows": len(rows),
               "by_window": {f"{d}D": sum(r["window_type"] == f"{d}D" for r in rows) for d in WINDOWS},
               "unknown_relationships": unknown, "disclosure_policy": "receipt_in_window",
               "news_input": str(args.news_hits),
               "disclosure_input": str(args.disclosure_evidence or args.ownership)}
    (out / "component_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
