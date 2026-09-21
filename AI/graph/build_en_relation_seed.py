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
from pathlib import Path

from relationship_score_components import blend, component

HERE = Path(__file__).resolve().parent
SRC = HERE / "data" / "en_rel_hits.jsonl"
OUT = HERE / "data" / "db_en_relations"
COMPANIES = HERE.parent / "ner" / "data" / "companies.csv"

WINDOW = "30D"
FORMULA = "rel-v0.4-en-news"
UNDIRECTED = {"PARTNER", "COMPETE"}


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
                       "impact_direction": "POSITIVE",
                       "confidence": round(min(1.0, 0.5 + 0.1 * len(docs)), 6),
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
