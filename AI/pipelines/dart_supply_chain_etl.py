"""Open DART table normalization -> graph triples.

The production adapter calls Open DART upstream and passes parsed disclosure rows
to ``normalize_relationship``. Network access and API keys are intentionally kept
outside this module so the transformation remains deterministic and testable.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation


CORPORATE_SUFFIX = re.compile(r"\s*(주식회사|\(주\)|㈜|Co\.,?\s*Ltd\.?|Inc\.?)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class SupplyTriple:
    source: str
    relation: str
    target: str
    weight: float
    item: str
    evidence: str


def clean_company_name(value: str) -> str:
    return re.sub(r"\s+", " ", CORPORATE_SUFFIX.sub("", value or "")).strip()


def parse_ratio(value: str | float | int | None) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (float, int)):
        raw = Decimal(str(value))
    else:
        cleaned = re.sub(r"[^0-9.\-]", "", value)
        try:
            raw = Decimal(cleaned)
        except InvalidOperation:
            return 0.0
    ratio = float(raw / 100 if abs(raw) > 1 else raw)
    return max(0.0, min(1.0, ratio))


def normalize_relationship(row: dict[str, object], reporting_company: str) -> SupplyTriple | None:
    counterparty = clean_company_name(str(row.get("counterparty") or row.get("주요매출처") or ""))
    if not counterparty or counterparty in {"기타", "해외", "국내"}:
        return None
    item = str(row.get("item") or row.get("품목") or "미상").strip()
    weight = parse_ratio(row.get("revenue_share") or row.get("매출비중"))
    return SupplyTriple(
        source=clean_company_name(reporting_company),
        relation="SUPPLIES_TO",
        target=counterparty,
        weight=weight,
        item=item,
        evidence=str(row.get("evidence") or "Open DART 사업보고서 주요 매출처 주석"),
    )


def transform(rows: list[dict[str, object]], reporting_company: str) -> list[dict[str, object]]:
    triples = (normalize_relationship(row, reporting_company) for row in rows)
    return [asdict(triple) for triple in triples if triple is not None]


if __name__ == "__main__":
    example = [
        {"주요매출처": "LG에너지솔루션(주)", "품목": "양극재", "매출비중": "22.4%"},
        {"주요매출처": "기타", "품목": "양극재", "매출비중": "77.6%"},
    ]
    print(transform(example, "(주)포스코퓨처엠"))
