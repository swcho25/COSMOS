"""영문 뉴스에서 뽑은 관계를 운영 DB 적재용 CSV 로 만든다.

  PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe build_en_relation_seed.py

입력은 extract_relations_en.py 의 --dump 결과다. 표본 20건 판정 정밀도 95%.

점수는 기존 119관계와 **같은 공식**을 쓴다 (relationship_score_components.component).
새 공식을 만들면 같은 화면에서 국내 관계와 크기를 비교할 수 없다.

근거 문서는 이미 운영 DB 에 있다. 영문 기사도 같은 뉴스 적재 경로를 탔기 때문에
document_id 가 그대로 유효하다 — 공시 때처럼 문서를 새로 만들 필요가 없다.

한계
  - 사람 검수 0건. production_approved 아님.
  - SUPPLY 방향은 어순으로 정했다 ("Apple-supplier Lumentum" 은 뒤가 공급자).
    표본에서는 맞았지만 전수 검증은 아니다.
  - INVEST 가 없다. SEC 지분보고서(13D/13G)가 수집 대상에서 빠져 있어
    영문 쪽 지분 관계는 만들 수 없다.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import math
from pathlib import Path

from relationship_score_components import blend, component

HERE = Path(__file__).resolve().parent
SRC = HERE / "data" / "en_rel_hits.jsonl"
OUT = HERE / "data" / "db_en_relations"
COMPANIES = HERE.parent / "ner" / "data" / "companies.csv"

WINDOW = "30D"
FORMULA = "rel-v0.4-en-news"
UNDIRECTED = {"PARTNER", "COMPETE"}


# 신뢰도: 근거가 많을수록 100%에 수렴하되 포화되지 않게 한다.
# 이전 식(0.5 + 0.1n)은 근거 5건에서 이미 100% 라 245개 중 28%가 만점에 몰렸다.
# 근거 5건과 150건이 같은 값이 되어 변별력이 없었다. 이 식은 만점이 2.4% 다.
# 하한 0.5 는 의도한 것이다 — 근거 1건이어도 원문 인용이 있는 확인된 관계라
# 18% 같은 값을 주면 화면에서 '틀린 관계'로 읽힌다.
# 여전히 확률이 아니다. 근거량을 0.5~1.0 으로 옮긴 값이며 검증된 적 없다.
# 최종 값은 data/relation_evidence/recompute_confidence.sql 이 덮어쓴다 —
# 국내분과 영문분에 같은 식을 쓰려면 한 곳이어야 한다. 여기 값은 적재 직후의 임시값이다.
CONF_SCALE = 8.0


def confidence_of(n_docs: int) -> float:
    return round(0.5 + 0.5 * (1.0 - math.exp(-n_docs / CONF_SCALE)), 6)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC, type=Path)
    ap.add_argument("--out", default=OUT, type=Path)
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    args = ap.parse_args()
    as_of_at = f"{args.as_of}T00:00:00Z"

    market = {}
    with COMPANIES.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            market[r["ticker"]] = r["market"]

    ev = collections.defaultdict(dict)      # (rel, src, dst) -> {document_id: sentence}
    for line in args.src.open(encoding="utf-8"):
        h = json.loads(line)
        rt, a, b = h["rel_type"], h["src_ticker"], h["dst_ticker"]
        if rt in UNDIRECTED and a > b:      # 무방향은 티커 순으로 정규화한다
            a, b = b, a
        ev[(rt, a, b)][h["document_id"]] = h["sentence"]

    rels, scores, links, skipped = [], [], [], collections.Counter()
    for (rt, a, b), docs in sorted(ev.items()):
        if a not in market or b not in market:
            skipped["유니버스 밖"] += 1
            continue
        nat = {"source_market": market[a], "source_stock_code": a,
               "target_market": market[b], "target_stock_code": b,
               "relationship_type_code": rt}
        # 뉴스 근거만 있으므로 news_score 만 채운다. 공시가 없으면 NULL 이 맞다.
        news = component([{"confidence": 1.0} for _ in docs])
        rels.append(nat)
        scores.append({**nat, "window_type": WINDOW, "score": blend(news, None),
                       "news_score": news, "disclosure_score": "",
                       # 영향 방향은 비운다(NULL). 국내는 estimate_edge_direction.py 의
                       # 쌍별 가격 상관으로 정하는데 그 표에 해외 종목이 없다.
                       # 재보지 않은 것을 POSITIVE 로 적으면 화면에 '긍정 전이' 로 떠서
                       # 측정된 값처럼 읽힌다 — 영문 54관계가 전부 그랬다.
                       # "없으면 POSITIVE" 규칙은 추정을 시도한 국내 쌍을 위한 것이지
                       # 애초에 재지 않은 쌍까지 덮으라는 뜻이 아니다.
                       "impact_direction": None,
                       "confidence": confidence_of(len(docs)),
                       "evidence_count": len(docs),
                       "formula_version": FORMULA, "as_of_at": as_of_at})
        for doc in docs:
            links.append({**nat, "document_id": doc})

    args.out.mkdir(parents=True, exist_ok=True)

    def write(name, fields, rows):
        with (args.out / name).open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"  {name:34} {len(rows):>5}행")

    nat_cols = ["source_market", "source_stock_code", "target_market",
                "target_stock_code", "relationship_type_code"]
    write("graph_snapshot.csv",
          ["as_of_at", "formula_version", "model_version", "status", "hdfs_uri", "published_at"],
          [{"as_of_at": as_of_at, "formula_version": FORMULA, "model_version": "en-rule-v1",
            "status": "PUBLISHED",
            "hdfs_uri": "local://AI/graph/data/en_rel_hits.jsonl", "published_at": as_of_at}])
    write("company_relationship.csv", nat_cols, rels)
    write("relationship_score_current.csv",
          nat_cols + ["window_type", "score", "news_score", "disclosure_score",
                      "impact_direction", "confidence", "evidence_count",
                      "formula_version", "as_of_at"], scores)
    write("relationship_evidence.csv", nat_cols + ["document_id"], links)

    by_type = collections.Counter(r["relationship_type_code"] for r in rels)
    s = sorted(x["score"] for x in scores)
    print(f"\n관계 {len(rels)}개  {dict(by_type)}")
    print(f"점수  최소 {s[0]:.1f}  중앙 {s[len(s)//2]:.1f}  최대 {s[-1]:.1f}")
    print(f"근거 연결 {len(links)}건 | 제외 {dict(skipped)}")
    print(f"  -> {args.out}")
    print("\n주의: 사람 검수 0건. 표본 20건 정밀도 95%.")


if __name__ == "__main__":
    main()
