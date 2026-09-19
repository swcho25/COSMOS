"""스토리 3 관계 확장 — 통합본을 DB 시드(company_relationship / relationship_score_current)로 낸다.

  PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe build_expanded_relation_seed.py
  PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe build_expanded_relation_seed.py --no-news-required

입력 세 갈래를 합친다. 기존 운영본은 읽기만 하고 수정하지 않는다.
  1) artifacts/kg_integrated_20260919_v3   현재 운영 중인 119관계 (유지)
  2) artifacts/kg_disclosures_expanded_*   DART 대량보유·단일판매 확장분
  3) artifacts/kg_rescan_deferred_*, kg_pilot_unprocessed_*   뉴스 재처리·파일럿 신규분

════════════════════════════════════════════════════════════════════════════
적용한 제약 (사용자 합의)
════════════════════════════════════════════════════════════════════════════
- 보고서 2종만: 주식등의대량보유상황보고서, 단일판매ㆍ공급계약체결.
  특수관계인 내부거래·동일인 출자계열회사 거래는 **일부러 제외**했다. 계열사 간 일상
  거래라 관계로 보기 약하고, 재벌 계열이 전부 서로 연결돼 그래프가 뭉개진다.
- 양쪽 끝이 모두 202종목 유니버스 안.
- 근거 원문 인용 필수.
- 지분 5% 이상이고 **보유목적이 지배(경영권 영향·경영참가·영향력 행사)** 인 것만.
  단순투자/일반투자는 제외 — 자산운용·신탁 보유를 관계로 올리면 금융지주 한 곳이
  수십 개 기업과 연결된다 (실제로 166건이 이렇게 걸러졌다).
- 제출인명은 **정확 일치**만 인정한다. 접두어 근사매칭은 '삼양사'를 '삼양식품'으로
  붙이는 오류를 냈다(근거 8건). 이 8건은 제거했다.

════════════════════════════════════════════════════════════════════════════
관계 유형과 영향 방향은 다른 축이다 — 섞지 말 것
════════════════════════════════════════════════════════════════════════════
relationship_type(SUPPLY/INVEST/PARTNER/COMPETE)은 **근거 문장이 말하는 관계**다.
impact_direction(POSITIVE/NEGATIVE)은 **가격이 실제로 같이 움직인 방향**이다.
둘은 독립이다. 협력 관계인데 주가가 반대로 가는 쌍이 있고, 그 반대도 있다.

특히 COMPETE 를 가격에서 만들지 않는다. "같은 업종에서 한쪽 오르고 한쪽 내린 날"은
같은 업종 730쌍의 평균 43.7% 에서 일어나고(98%의 쌍이 30% 이상), train 상관이 음수인
쌍을 골라도 test 에서 부호가 유지되는 비율이 21%(r<=-0.05) / 11%(r<=-0.10) / 0%(r<=-0.15)
였다. 재현되지 않는다. 그래서 COMPETE 는 근거 문장이 있는 8개만 둔다.

impact_direction 은 estimate_edge_direction.py 의 쌍별 추정을 쓴다 (market 잔차,
train 추정 -> val 선택 -> test 보고, 부호 적중률 63.5%(전부POSITIVE) -> 71.9%).
추정이 없는 쌍은 유형 기본값이 아니라 **전부 POSITIVE** 로 둔다 — 유형 기본값은 test
적중률 57.4% 로 파라미터 0개 기준선(63.5%)보다 나쁘다.

한계
  - 사람 검수 0건이다. production_approved 를 세우지 않는다.
  - 보유목적 서술이 판정 규칙 어느 쪽에도 안 걸린 481건을 버렸다. 일부는 실제 지배
    관계일 수 있으므로 이 관계 수는 **하한**이다.
  - 공시 근거만 있는 관계는 뉴스 근거가 없다. --no-news-required 가 기본인 이유다.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = DATA / "db_expanded"
COMPANIES = HERE.parent / "ner" / "data" / "companies.csv"
ALIASES = HERE.parent / "ner" / "data" / "aliases.csv"
DIRECTION_CSV = DATA / "db" / "relationship_impact_direction.csv"

BASE = HERE / "artifacts" / "kg_integrated_20260919_v3" / "temporal_evidence.jsonl"
DISC = HERE / "artifacts" / "kg_disclosures_expanded_20260919_v1" / "disc_expanded_v3.jsonl"
NEWS = [HERE / "artifacts" / "kg_rescan_deferred_20260919_v1" / "rescued_relations.jsonl",
        HERE / "artifacts" / "kg_pilot_unprocessed_20260919_v1" / "pilot_relations.jsonl"]

TYPES = [("SUPPLY", "공급", "DIRECTED"), ("INVEST", "투자", "DIRECTED"),
         ("PARTNER", "협력", "UNDIRECTED"), ("COMPETE", "경쟁", "UNDIRECTED")]
WINDOW = "30D"                      # BackEnd GraphService.GRAPH_WINDOW
FORMULA = "rel-v0.3-expanded"


def norm(s: str) -> str:
    return re.sub(r"[()（）\[\]주식회사㈜\s\.,'\"-]", "", s or "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT, type=Path)
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    ap.add_argument("--min-evidence", type=int, default=1,
                    help="관계당 최소 근거 수")
    ap.add_argument("--hdfs-uri",
                    default="local://AI/graph/artifacts/kg_disclosures_expanded_20260919_v1",
                    help="graph_snapshot.hdfs_uri. PUBLISHED 면 NOT NULL 이어야 한다. "
                         "아직 HDFS 게시를 하지 않았으므로 로컬 산출물 경로를 적어 둔다")
    args = ap.parse_args()
    as_of_at = f"{args.as_of}T00:00:00Z"

    market = {}
    with COMPANIES.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            market[r["ticker"]] = r["market"]

    exact = {}
    with ALIASES.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            n = norm(r["alias"])
            if len(n) >= 2:
                exact.setdefault(n, r["ticker"])

    ev = collections.defaultdict(list)      # (rel_type, src, dst) -> 근거들
    src_kind = collections.defaultdict(set)

    for line in BASE.open(encoding="utf-8"):
        e = json.loads(line)
        k = (e["rel_type"], e["src_ticker"], e["dst_ticker"])
        ev[k].append({"kind": e["source_kind"], "quote": e.get("quote"),
                      "url": e.get("source_url"), "at": e.get("published_at")})
        src_kind[k].add(e["source_kind"])

    dropped_fuzzy = 0
    for line in DISC.open(encoding="utf-8"):
        r = json.loads(line)
        # 제출인명 정확 일치만 인정한다 ('삼양사' -> 삼양식품 오매칭 차단)
        if r["rel_type"] == "INVEST" and exact.get(norm(r.get("filer"))) != r["src_ticker"]:
            dropped_fuzzy += 1
            continue
        k = (r["rel_type"], r["src_ticker"], r["dst_ticker"])
        ev[k].append({"kind": "DISCLOSURE", "quote": r.get("quote"),
                      "url": r.get("source_url"), "at": r.get("published_at")})
        src_kind[k].add("DISCLOSURE")

    for p in NEWS:
        if not p.exists():
            continue
        for line in p.open(encoding="utf-8"):
            r = json.loads(line)
            k = (r["rel_type"], r["src_ticker"], r["dst_ticker"])
            ev[k].append({"kind": "NEWS", "quote": r.get("sentence"),
                          "url": r.get("source_url"), "at": r.get("published_at")})
            src_kind[k].add("NEWS")

    # 영향 방향: 쌍별 가격 추정이 있으면 그걸, 없으면 전부 POSITIVE
    direction = {}
    if DIRECTION_CSV.exists():
        with DIRECTION_CSV.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                key = frozenset((r["source_stock_code"], r["target_stock_code"]))
                direction[key] = r["impact_direction"]

    rels, scores, skipped = [], [], collections.Counter()
    for (rt, a, b), evs in sorted(ev.items()):
        if a not in market or b not in market:
            skipped["유니버스 밖"] += 1
            continue
        if a == b:
            skipped["자기 자신"] += 1
            continue
        if len(evs) < args.min_evidence:
            skipped["근거 부족"] += 1
            continue
        nat = {"source_market": market[a], "source_stock_code": a,
               "target_market": market[b], "target_stock_code": b,
               "relationship_type_code": rt}
        rels.append(nat)
        kinds = src_kind[(rt, a, b)]
        # 점수: 근거 수를 로그 압축해 0~100. 공시는 법정 근거라 뉴스보다 가중을 준다.
        n_disc = sum(1 for e in evs if e["kind"] == "DISCLOSURE")
        n_news = sum(1 for e in evs if e["kind"] == "NEWS")
        import math
        score = min(100.0, 100.0 * (math.log1p(n_disc * 1.5 + n_news) / math.log1p(40)))
        scores.append({**nat, "window_type": WINDOW, "score": round(score, 6),
                       "impact_direction": direction.get(frozenset((a, b)), "POSITIVE"),
                       "confidence": round(min(1.0, 0.5 + 0.1 * len(evs)), 6),
                       "evidence_count": len(evs),
                       "formula_version": FORMULA, "as_of_at": as_of_at})

    args.out.mkdir(parents=True, exist_ok=True)

    def write(name, fields, rows):
        with (args.out / name).open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"  {name:36} {len(rows):>5}행")

    write("relationship_type.csv", ["code", "name", "directionality"],
          [{"code": c, "name": n, "directionality": d} for c, n, d in TYPES])
    # 스키마 제약: status='PUBLISHED' 면 published_at 과 hdfs_uri 가 둘 다 NOT NULL 이어야 한다
    # (chk_graph_snapshot_published). 빈 문자열로 두면 적재가 막힌다.
    write("graph_snapshot.csv",
          ["as_of_at", "formula_version", "model_version", "status", "hdfs_uri", "published_at"],
          [{"as_of_at": as_of_at, "formula_version": FORMULA, "model_version": "dict-v1.3",
            "status": "PUBLISHED", "hdfs_uri": args.hdfs_uri, "published_at": as_of_at}])
    write("company_relationship.csv",
          ["source_market", "source_stock_code", "target_market", "target_stock_code",
           "relationship_type_code"], rels)
    write("relationship_score_current.csv",
          ["source_market", "source_stock_code", "target_market", "target_stock_code",
           "relationship_type_code", "window_type", "score", "impact_direction",
           "confidence", "evidence_count", "formula_version", "as_of_at"], scores)

    by_type = collections.Counter(r["relationship_type_code"] for r in rels)
    by_src = collections.Counter(
        "+".join(sorted(src_kind[(r["relationship_type_code"], r["source_stock_code"],
                                  r["target_stock_code"])])) for r in rels)
    neg = sum(1 for s in scores if s["impact_direction"] == "NEGATIVE")
    print(f"\n관계 {len(rels)}개  {dict(by_type)}")
    print(f"근거 출처별 관계 수: {dict(by_src)}")
    print(f"impact_direction NEGATIVE {neg}개 / POSITIVE {len(scores)-neg}개")
    print(f"제출인 근사매칭으로 제거한 공시 근거 {dropped_fuzzy}건 | 기타 제외 {dict(skipped)}")
    print(f"  -> {args.out}")
    print("\n주의: 사람 검수 0건. production_approved 아님. 운영 DB 전환은 별도 승인 필요.")


if __name__ == "__main__":
    main()
