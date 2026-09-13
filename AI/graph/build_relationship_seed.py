"""Knowledge graph step 8 (first cut) — DB seed for the relationship tables, from the ownership edges.

  PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe build_relationship_seed.py
  -> data/db/{relationship_type,graph_snapshot,company_relationship,relationship_score_current}.csv
  -> data/db/load_relationships.sql

왜 지분 엣지만 넣는가
--------------------
프론트(`FrontEnd/src/lib/meta.ts`)와 API 타입이 관계 유형을 SUPPLY·INVEST·PARTNER·COMPETE
4종으로 고정해 두었고, `RelationMap2D` 는 이 4종에 해당하는 엣지만 그룹으로 렌더링한다.
우리가 가진 엣지 4종 중 유형이 확정되는 건 ownership(→INVEST) 하나뿐이다.
sector·co_mention·correlation 은 "무슨 관계인지"를 말해주지 않는다 — 같이 언급됐다는 건
경쟁인지 협력인지 납품인지 구분하지 못한다. 그 3종은 유형이 아니라 **세기(score) 재료**이고,
SUPPLY·PARTNER·COMPETE 는 문장 단위 관계추출(Step 6)이 나와야 채워진다.
그래서 여기서는 유형이 확실한 44개로 DB→API→화면 경로를 먼저 관통시킨다.

자연키 (build_db_seed.py 와 같은 규약)
-------------------------------------
DB가 UUID를 생성하므로 CSV는 자연키를 싣고 load_relationships.sql 이 해석한다.
    company            (market, stock_code)   -- uk_company_market_stock_code
    relationship_type  (code)                 -- uk_relationship_type_code
    graph_snapshot     (as_of_at, formula_version)

스키마에서 확인한 제약 (V1__create_initial_schema.sql)
----------------------------------------------------
    relationship_type.directionality  IN ('DIRECTED','UNDIRECTED')
    company_relationship              UNIQUE(source, target, type), source <> target
    relationship_score_current        PK(relationship_id, window_type), confidence 0~1
    graph_snapshot                    status='PUBLISHED' 이면 published_at·hdfs_uri 필수
    relationship_evidence.document_id NOT NULL

마지막 줄이 중요하다. 근거 문서가 없는 엣지(지분·업종·상관)는 relationship_evidence 행을
아예 만들 수 없다. 스키마를 바꿀 필요는 없고 그냥 행이 없을 뿐이라, evidence_count 는
정직하게 0으로 둔다. DART 공시(rcept_no)는 edges_ownership.csv 의 evidence 컬럼에 남아
있으니, source_document 에 공시가 적재되면 그때 연결하면 된다.

조회 조건 (BackEnd GraphService / GraphQueryRepository)
------------------------------------------------------
    GRAPH_WINDOW = "30D"          -> window_type 은 반드시 '30D'
    graph_snapshot.status         = 'PUBLISHED' 인 것 중 as_of_at 최신 1건만 씀
    company.status                = 'ACTIVE' 인 회사만 엣지에 나옴
셋 중 하나라도 어긋나면 API가 빈 그래프를 돌려준다.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
from pathlib import Path

HERE = Path(__file__).resolve().parent
OWNERSHIP = HERE / "data" / "edges_ownership.csv"
COMPANIES = HERE.parent / "ner" / "data" / "companies.csv"
OUT = HERE / "data" / "db"

FORMULA_VERSION = "rel-v0.1-ownership"
MODEL_VERSION = "dict-v1.3"
WINDOW_TYPE = "30D"          # BackEnd GraphService.GRAPH_WINDOW
HDFS_URI = "hdfs://localhost:9000/data-lake/sandbox/news/junwoo/company-mentions/model_version=dict-v1.3"

# 프론트 RELATIONSHIP_ORDER 와 같은 4종. 지금 채워지는 건 INVEST 뿐이지만, 유형 테이블은
# 미리 다 넣어야 Step 6 결과가 나왔을 때 마이그레이션 없이 바로 꽂힌다.
RELATIONSHIP_TYPES = [
    ("SUPPLY", "공급", "DIRECTED"),      # A가 B에 납품 — 방향 있음
    ("INVEST", "투자", "DIRECTED"),      # A가 B 지분 보유 — 방향 있음
    ("PARTNER", "협력", "UNDIRECTED"),
    ("COMPETE", "경쟁", "UNDIRECTED"),
]


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"  {path.name:34} {len(rows):>4}행")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ownership", default=OWNERSHIP, type=Path)
    ap.add_argument("--companies", default=COMPANIES, type=Path)
    ap.add_argument("--out", default=OUT, type=Path)
    ap.add_argument("--as-of", default=None, help="기본값은 지분 엣지의 최신 as_of_date")
    args = ap.parse_args()

    with args.companies.open(encoding="utf-8-sig", newline="") as f:
        market = {r["ticker"]: r["market"] for r in csv.DictReader(f)}
    with args.ownership.open(encoding="utf-8-sig", newline="") as f:
        edges = list(csv.DictReader(f))

    as_of_date = args.as_of or max(e["as_of_date"] for e in edges)
    as_of_at = f"{as_of_date}T00:00:00Z"

    rels, scores, skipped = [], [], []
    seen: set[tuple] = set()
    for e in edges:
        src, dst = e["src_ticker"], e["dst_ticker"]
        if src not in market or dst not in market:
            skipped.append((src, dst, "유니버스에 없는 종목"))
            continue
        if src == dst:
            skipped.append((src, dst, "자기 지분"))       # chk_..._distinct_companies
            continue
        key = (market[src], src, market[dst], dst, "INVEST")
        if key in seen:
            skipped.append((src, dst, "중복 쌍"))          # uk_company_relationship_identity
            continue
        seen.add(key)
        nat = {"source_market": market[src], "source_stock_code": src,
               "target_market": market[dst], "target_stock_code": dst,
               "relationship_type_code": "INVEST"}
        rels.append(nat)
        # score 는 프론트가 0~100 으로 읽는다(GraphEdge.score). 지분 관계의 세기는 지분율 그대로가
        # 가장 해석하기 쉽다 — 79.4% 보유면 79.4점.
        pct = round(float(e["weight"]) * 100, 6)
        scores.append({**nat, "window_type": WINDOW_TYPE, "score": pct,
                       # 지분 보유는 좋은 소식도 나쁜 소식도 그대로 전달한다. 방향은 관계가 아니라
                       # 뉴스 감성이 정하는 것이라 여기서는 비워 둔다(컬럼 nullable).
                       "impact_direction": "",
                       "confidence": 1.0,        # DART 공시 원문에서 파싱한 사실
                       "evidence_count": 0,      # 공시가 source_document 에 없어 evidence 행을 못 만듦
                       "formula_version": FORMULA_VERSION, "as_of_at": as_of_at})

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"지분 엣지 {len(edges)} -> 관계 {len(rels)}")
    write_csv(args.out / "relationship_type.csv", ["code", "name", "directionality"],
              [{"code": c, "name": n, "directionality": d} for c, n, d in RELATIONSHIP_TYPES])
    write_csv(args.out / "graph_snapshot.csv",
              ["as_of_at", "formula_version", "model_version", "status", "hdfs_uri", "published_at"],
              [{"as_of_at": as_of_at, "formula_version": FORMULA_VERSION,
                "model_version": MODEL_VERSION, "status": "PUBLISHED",
                "hdfs_uri": HDFS_URI, "published_at": as_of_at}])
    write_csv(args.out / "company_relationship.csv",
              ["source_market", "source_stock_code", "target_market", "target_stock_code",
               "relationship_type_code"], rels)
    write_csv(args.out / "relationship_score_current.csv",
              ["source_market", "source_stock_code", "target_market", "target_stock_code",
               "relationship_type_code", "window_type", "score", "impact_direction",
               "confidence", "evidence_count", "formula_version", "as_of_at"], scores)
    (args.out / "load_relationships.sql").write_text(LOADER_SQL, encoding="utf-8")
    print(f"  {'load_relationships.sql':34}")
    if skipped:
        print(f"제외 {len(skipped)}건:")
        for s, d, why in skipped:
            print(f"  {s} -> {d}  {why}")
    print(f"\nwindow_type={WINDOW_TYPE} | snapshot status=PUBLISHED | as_of={as_of_at}")
    print("적재: psql -f data/db/load_relationships.sql (CSV 4개를 같은 폴더에 두고 실행)")


LOADER_SQL = r"""-- 관계 시드 적재. 자연키를 UUID로 해석한다. 여러 번 돌려도 안전하다(ON CONFLICT).
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f load_relationships.sql
-- CSV 4개가 이 파일과 같은 디렉터리에 있어야 한다(\copy 는 클라이언트 경로 기준).
BEGIN;

CREATE TEMP TABLE _rel_type (code text, name text, directionality text) ON COMMIT DROP;
CREATE TEMP TABLE _snapshot (as_of_at timestamptz, formula_version text, model_version text,
                             status text, hdfs_uri text, published_at timestamptz) ON COMMIT DROP;
CREATE TEMP TABLE _rel (source_market text, source_stock_code text, target_market text,
                        target_stock_code text, relationship_type_code text) ON COMMIT DROP;
CREATE TEMP TABLE _score (source_market text, source_stock_code text, target_market text,
                          target_stock_code text, relationship_type_code text, window_type text,
                          score numeric, impact_direction text, confidence numeric,
                          evidence_count int, formula_version text, as_of_at timestamptz) ON COMMIT DROP;

\copy _rel_type     FROM 'relationship_type.csv'          WITH (FORMAT csv, HEADER true)
\copy _snapshot     FROM 'graph_snapshot.csv'             WITH (FORMAT csv, HEADER true)
\copy _rel          FROM 'company_relationship.csv'       WITH (FORMAT csv, HEADER true)
\copy _score        FROM 'relationship_score_current.csv' WITH (FORMAT csv, HEADER true)

-- 1) 관계 유형 4종
INSERT INTO relationship_type (code, name, directionality)
SELECT code, name, directionality FROM _rel_type
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name,
                                 directionality = EXCLUDED.directionality;

-- 2) 그래프 스냅샷. 같은 (as_of_at, formula_version) 이면 재사용한다.
INSERT INTO graph_snapshot (as_of_at, formula_version, model_version, status, hdfs_uri, published_at)
SELECT s.as_of_at, s.formula_version, s.model_version, s.status, s.hdfs_uri, s.published_at
FROM _snapshot s
WHERE NOT EXISTS (SELECT 1 FROM graph_snapshot g
                  WHERE g.as_of_at = s.as_of_at AND g.formula_version = s.formula_version);

-- 3) 기업 관계
INSERT INTO company_relationship (source_company_id, target_company_id, relationship_type_id)
SELECT sc.company_id, tc.company_id, rt.relationship_type_id
FROM _rel r
JOIN company sc ON sc.market = r.source_market AND sc.stock_code = r.source_stock_code
JOIN company tc ON tc.market = r.target_market AND tc.stock_code = r.target_stock_code
JOIN relationship_type rt ON rt.code = r.relationship_type_code
ON CONFLICT (source_company_id, target_company_id, relationship_type_id) DO NOTHING;

-- 4) 현재 점수
INSERT INTO relationship_score_current (relationship_id, window_type, snapshot_id, score,
                                        impact_direction, confidence, evidence_count,
                                        formula_version, as_of_at)
SELECT cr.relationship_id, s.window_type, g.snapshot_id, s.score,
       NULLIF(s.impact_direction, ''), s.confidence, s.evidence_count, s.formula_version, s.as_of_at
FROM _score s
JOIN company sc ON sc.market = s.source_market AND sc.stock_code = s.source_stock_code
JOIN company tc ON tc.market = s.target_market AND tc.stock_code = s.target_stock_code
JOIN relationship_type rt ON rt.code = s.relationship_type_code
JOIN company_relationship cr ON cr.source_company_id = sc.company_id
                            AND cr.target_company_id = tc.company_id
                            AND cr.relationship_type_id = rt.relationship_type_id
JOIN graph_snapshot g ON g.as_of_at = s.as_of_at AND g.formula_version = s.formula_version
ON CONFLICT (relationship_id, window_type)
DO UPDATE SET snapshot_id = EXCLUDED.snapshot_id, score = EXCLUDED.score,
              impact_direction = EXCLUDED.impact_direction, confidence = EXCLUDED.confidence,
              evidence_count = EXCLUDED.evidence_count, formula_version = EXCLUDED.formula_version,
              as_of_at = EXCLUDED.as_of_at;

COMMIT;

-- 확인용: API가 실제로 보게 될 엣지 수 (company.status='ACTIVE' 조건 포함)
--   SELECT rt.code, count(*) FROM relationship_score_current rsc
--     JOIN company_relationship cr ON cr.relationship_id = rsc.relationship_id
--     JOIN relationship_type rt ON rt.relationship_type_id = cr.relationship_type_id
--     JOIN company sc ON sc.company_id = cr.source_company_id AND sc.status = 'ACTIVE'
--     JOIN company tc ON tc.company_id = cr.target_company_id AND tc.status = 'ACTIVE'
--     JOIN graph_snapshot g ON g.snapshot_id = rsc.snapshot_id AND g.status = 'PUBLISHED'
--    WHERE rsc.window_type = '30D' GROUP BY rt.code;
"""


if __name__ == "__main__":
    main()
