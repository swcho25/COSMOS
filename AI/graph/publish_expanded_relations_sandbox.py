"""확장 관계 193개를 HDFS 샌드박스에 낼 parquet 과 manifest 를 만든다.

  PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe publish_expanded_relations_sandbox.py

정식 존(/data-lake/analyzed/...)에 올리지 않는 이유
────────────────────────────────────────────────────────────────────────────
publish_grounded_lake.py 는 fail-closed 라 16개 목적지를 전부 요구한다. 뉴스·감성·
집계까지 통째로 다시 만들어야 하나의 run 이 된다. 관계만 따로 넣을 수 없다.
게다가 이 193개는 사람 검수 0건이라 _VERIFIED 정식 존에 올릴 자격이 아직 없다.
그래서 팀이 이미 쓰는 샌드박스(/data-lake/sandbox/...)에 별도 경로로 낸다.
기존 검증 run(lake-grounded-20260919-v1)은 읽지도 쓰지도 않는다.

두 데이터셋을 낸다
  relations  관계 1행씩 (193) — 유형·방향·구성 점수
  evidence   근거 1행씩      — 원문 인용·URL·발행시각. 관계당 여러 행이다.

구성 점수는 relationship_score_components.component 를 쓴다. 기존 119관계와 같은
공식이어야 화면에서 두 점수를 같은 축으로 비교할 수 있다.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from relationship_score_components import blend, component

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "hdfs_sandbox_relations"
COMPANIES = HERE.parent / "ner" / "data" / "companies.csv"
ALIASES = HERE.parent / "ner" / "data" / "aliases.csv"

BASE = HERE / "artifacts" / "kg_integrated_20260919_v3" / "temporal_evidence.jsonl"
DISC = HERE / "artifacts" / "kg_disclosures_expanded_20260919_v1" / "disc_expanded_v3.jsonl"
NEWS = [HERE / "artifacts" / "kg_rescan_deferred_20260919_v1" / "rescued_relations.jsonl",
        HERE / "artifacts" / "kg_pilot_unprocessed_20260919_v1" / "pilot_relations.jsonl"]

SANDBOX_ROOT = "/data-lake/sandbox/graph/junwoo/expanded-relations"
LIMITATIONS = [
    "사람 검수 0건. production_approved 아님. 정식 분석 존이 아니라 샌드박스다.",
    "보유목적 서술이 판정 규칙 어느 쪽에도 안 걸린 481건을 버렸다. 관계 수는 하한이다.",
    "공시 근거만 있는 관계는 뉴스 근거가 없다. 그 반대도 있다.",
    "impact_direction 은 가격 상관 추정이다. 관계 유형과 다른 축이며 test 적중률 71.9% 다.",
    "COMPETE 는 근거 문장이 있는 8개뿐이다. 가격에서 만들지 않았다.",
    "기존 검증 run lake-grounded-20260919-v1 을 읽거나 바꾸지 않았다.",
]


def norm(s: str) -> str:
    return re.sub(r"[()（）\[\]주식회사㈜\s\.,'\"-]", "", s or "")


def collect_evidence() -> dict:
    exact = {}
    with ALIASES.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            n = norm(r["alias"])
            if len(n) >= 2:
                exact.setdefault(n, r["ticker"])

    ev = collections.defaultdict(list)
    for line in BASE.open(encoding="utf-8"):
        e = json.loads(line)
        ev[(e["rel_type"], e["src_ticker"], e["dst_ticker"])].append(
            {"kind": e["source_kind"], "quote": e.get("quote"),
             "url": e.get("source_url"), "at": e.get("published_at")})
    for line in DISC.open(encoding="utf-8"):
        r = json.loads(line)
        # 제출인명 정확 일치만 인정한다 ('삼양사' -> 삼양식품 오매칭 차단)
        if r["rel_type"] == "INVEST" and exact.get(norm(r.get("filer"))) != r["src_ticker"]:
            continue
        ev[(r["rel_type"], r["src_ticker"], r["dst_ticker"])].append(
            {"kind": "DISCLOSURE", "quote": r.get("quote"),
             "url": r.get("source_url"), "at": r.get("published_at")})
    for p in NEWS:
        if not p.exists():
            continue
        for line in p.open(encoding="utf-8"):
            r = json.loads(line)
            ev[(r["rel_type"], r["src_ticker"], r["dst_ticker"])].append(
                {"kind": "NEWS", "quote": r.get("sentence"),
                 "url": r.get("source_url"), "at": r.get("published_at")})
    return ev


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT, type=Path)
    ap.add_argument("--run-id", default="rel-expanded-" + dt.date.today().strftime("%Y%m%d") + "-v1")
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    args = ap.parse_args()
    as_of_at = f"{args.as_of}T00:00:00Z"

    market = {}
    with COMPANIES.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            market[r["ticker"]] = r["market"]

    direction = {}
    dpath = HERE / "data" / "db" / "relationship_impact_direction.csv"
    if dpath.exists():
        with dpath.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                direction[frozenset((r["source_stock_code"], r["target_stock_code"]))] = \
                    r["impact_direction"]

    ev = collect_evidence()
    rel_rows, ev_rows = [], []
    for (rt, a, b), evs in sorted(ev.items()):
        if a not in market or b not in market or a == b:
            continue
        docs = collections.defaultdict(dict)
        for i, e in enumerate(evs):
            docs[e["kind"]][e.get("url") or f"_noid{i}"] = {"confidence": 1.0}
        n_sc = component(list(docs["NEWS"].values()))
        d_sc = component(list(docs["DISCLOSURE"].values()))
        score = blend(n_sc, d_sc)
        if score is None:
            continue
        rel_rows.append({
            "run_id": args.run_id, "rel_type": rt, "src_ticker": a, "dst_ticker": b,
            "src_market": market[a], "dst_market": market[b],
            "directed": rt in ("SUPPLY", "INVEST"),
            "score": score, "news_score": n_sc, "disclosure_score": d_sc,
            "impact_direction": direction.get(frozenset((a, b)), "POSITIVE"),
            "evidence_count": sum(len(v) for v in docs.values()),
            "as_of_at": as_of_at,
        })
        for e in evs:
            ev_rows.append({
                "run_id": args.run_id, "rel_type": rt, "src_ticker": a, "dst_ticker": b,
                "source_kind": e["kind"], "quote": e["quote"], "source_url": e["url"],
                "published_at": e["at"], "as_of_at": as_of_at,
            })

    args.out.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, rows in (("relations", rel_rows), ("evidence", ev_rows)):
        d = args.out / name
        d.mkdir(exist_ok=True)
        path = d / f"{name}.parquet"
        pq.write_table(pa.Table.from_pylist(rows), path, compression="snappy")
        written[name] = {"rows": len(rows), "bytes": path.stat().st_size,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        print(f"  {name:10} {len(rows):>6}행  {path.stat().st_size:>8,}B")

    both = sum(1 for r in rel_rows if r["news_score"] is not None
               and r["disclosure_score"] is not None)
    payload = hashlib.sha256(
        "".join(written[k]["sha256"] for k in sorted(written)).encode()).hexdigest()
    manifest = {
        "run_id": args.run_id,
        "status": "SANDBOX_UNVERIFIED",
        "sandbox": True,
        "publication_sha256": payload,
        "as_of": as_of_at,
        "target_root": SANDBOX_ROOT,
        "produced_by": "AI/graph/publish_expanded_relations_sandbox.py",
        "score_formula": "100 * (1 - exp(-sum(per-document confidence) / 5)); "
                         "기존 119관계와 동일 (relationship_score_components.component)",
        "counts": {"relations": len(rel_rows), "evidence": len(ev_rows),
                   "news_and_disclosure": both,
                   "by_type": dict(collections.Counter(r["rel_type"] for r in rel_rows))},
        "datasets": written,
        "limitations": LIMITATIONS,
    }
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n관계 {len(rel_rows)}개 / 근거 {len(ev_rows)}건 / 뉴스+공시 둘 다 {both}개")
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
