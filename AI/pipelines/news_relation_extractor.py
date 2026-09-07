"""Conservative Korean finance-news relation extraction baseline.

Replace ``extract_entities`` with a KoELECTRA/GLiNER model in production. The
fallback only emits relations that match an allow-listed financial pattern and
retains the source sentence as evidence for human review.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


RELATION_PATTERNS = {
    "SHORTAGE_RISK": re.compile(r"(?P<material>[가-힣A-Za-z0-9·\-]+)\s*(?:공급|수급).{0,22}?(?:차질|부족|중단)"),
    "SUPPLIES_TO": re.compile(r"(?P<supplier>[가-힣A-Za-z0-9·\-]+).{0,18}?(?P<buyer>[가-힣A-Za-z0-9·\-]+)(?:에|에게)\s*.{0,12}?(?:공급|납품)"),
    "OWNS_STAKE": re.compile(r"(?P<owner>[가-힣A-Za-z0-9·\-]+).{0,18}?(?P<target>[가-힣A-Za-z0-9·\-]+)\s*지분\s*(?P<stake>[0-9.]+)%"),
}


@dataclass(frozen=True)
class ExtractedRelation:
    source: str
    relation: str
    target: str
    confidence: float
    evidence: str
    requires_review: bool


def extract_relations(text: str) -> list[dict[str, object]]:
    relations: list[ExtractedRelation] = []
    sentences = re.split(r"(?<=[.!?다])\s+", text.strip())
    for sentence in sentences:
        for relation, pattern in RELATION_PATTERNS.items():
            for match in pattern.finditer(sentence):
                groups = match.groupdict()
                if relation == "SHORTAGE_RISK":
                    source, target, confidence = groups["material"], "UNKNOWN_COMPANY", 0.62
                elif relation == "SUPPLIES_TO":
                    source, target, confidence = groups["supplier"], groups["buyer"], 0.74
                else:
                    source, target = groups["owner"], groups["target"]
                    confidence = min(0.96, 0.72 + float(groups["stake"]) / 500)
                relations.append(
                    ExtractedRelation(source, relation, target, round(confidence, 3), sentence, confidence < 0.8)
                )
    return [asdict(item) for item in relations]


if __name__ == "__main__":
    sample = "포스코퓨처엠은 LG에너지솔루션에 양극재를 공급했다. 리튬 공급 차질 우려가 커졌다."
    print(extract_relations(sample))
