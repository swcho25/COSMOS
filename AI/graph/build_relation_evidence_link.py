"""확장 관계 193개의 근거를 운영 DB 에 붙일 수 있게 문서·연결 행을 만든다.

  PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe build_relation_evidence_link.py

왜 필요한가
────────────────────────────────────────────────────────────────────────────
load_into_analysis_run.sql 은 relationship_score_current 만 넣었다. 화면은 근거 개수를
점수 행의 evidence_count 에서 읽고, 근거 목록은 relationship_evidence 에서 읽는다.
그래서 "근거 문서 16건" 이라고 떠 놓고 목록은 비는 상태가 됐다. 내 적재 누락이다.

근거 문서를 어떻게 잇는가 — 세 갈래다
────────────────────────────────────────────────────────────────────────────
  뉴스   source_url 로 current_source_document.original_url 과 맞춘다 (356/356 맞음)
  공시   document_id 안에 UUID 가 박힌 것은 그대로 쓴다 (364건)
  공시   DART 접수번호(14자리)만 있는 것은 dart URL 을 조립해 맞춘다

여기까지 해도 공시 872건은 원문 문서가 운영 DB 에 없다. 운영에 적재된 공시는 797건인데
확장분은 그 밖의 DART 공시를 썼기 때문이다. 그 문서들을 이 스크립트가 만들어 낸다.

만들어 내는 공시 문서에 대하여
────────────────────────────────────────────────────────────────────────────
지어내는 값은 없다. 전부 DART 원본에서 온 것이다.
  document_id   접수번호로부터 uuid5 (결정적 — 다시 돌려도 같은 값)
  original_url  https://dart.fss.or.kr/dsaf001/main.do?rcpNo=<접수번호>  (실제 열린다)
  title         공시 보고서명 그대로
  published_at  공시 접수일
본문(hdfs_raw_uri 등)은 채우지 않는다. 우리가 갖고 있지 않다. NULL 로 둔다.
"""
from __future__ import annotations

import csv
import json
import re
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ART = HERE / "artifacts"
OUT = HERE / "data" / "relation_evidence"

BASE = ART / "kg_integrated_20260919_v3" / "temporal_evidence.jsonl"
DISC = ART / "kg_disclosures_expanded_20260919_v1" / "disc_expanded_v3.jsonl"
NEWS = [ART / "kg_rescan_deferred_20260919_v1" / "rescued_relations.jsonl",
        ART / "kg_pilot_unprocessed_20260919_v1" / "pilot_relations.jsonl"]

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RCEPT_RE = re.compile(r"^\d{14}$")
DART_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"
# 접수번호 -> document_id 를 결정적으로 만든다. 다시 돌려도 같은 UUID 가 나와야 멱등이다.
NS = uuid.UUID("6f1b6f6e-1f9a-5c3a-9d2f-0a1b2c3d4e5f")
FORMULA = "rel-v0.3-expanded"


def iso(v) -> str | None:
    """공시는 20260413, 뉴스는 ISO 문자열로 온다."""
    s = str(v or "").strip()
    if RCEPT_RE.match(s) or (len(s) == 8 and s.isdigit()):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}T00:00:00Z"
    return s or None


def main() -> None:
    ev, docs = [], {}

    def add(rt, src, dst, kind, did, url, title=None, at=None):
        did = str(did or "")
        m = UUID_RE.search(did)
        rc = did if RCEPT_RE.match(did) else None
        doc_uuid = m.group(0) if m else None
        if doc_uuid is None and rc:
            # 운영에 없으면 우리가 만들 문서의 id. 접수번호 기반이라 재실행에도 같다.
            doc_uuid = str(uuid.uuid5(NS, rc))
            docs.setdefault(doc_uuid, {
                "document_id": doc_uuid, "document_type": "DISCLOSURE",
                "title": title or "", "original_url": DART_URL.format(rc),
                "published_at": iso(at) or "", "rcept_no": rc})
        ev.append({"rel_type": rt, "src": src, "dst": dst, "source_kind": kind,
                   "uuid": doc_uuid or "", "rcept_no": rc or "", "source_url": url or ""})

    for line in BASE.open(encoding="utf-8"):
        e = json.loads(line)
        add(e["rel_type"], e["src_ticker"], e["dst_ticker"], e["source_kind"],
            e.get("document_id"), e.get("source_url"), e.get("title"), e.get("published_at"))
    for line in DISC.open(encoding="utf-8"):
        r = json.loads(line)
        add(r["rel_type"], r["src_ticker"], r["dst_ticker"], "DISCLOSURE",
            r.get("document_id"), r.get("source_url"), r.get("title"), r.get("published_at"))
    for p in NEWS:
        if not p.exists():
            continue
        for line in p.open(encoding="utf-8"):
            r = json.loads(line)
            add(r["rel_type"], r["src_ticker"], r["dst_ticker"], "NEWS",
                r.get("document_id"), r.get("source_url"), r.get("title"), r.get("published_at"))

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "evidence_link.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["rel_type", "src", "dst", "source_kind",
                                          "uuid", "rcept_no", "source_url"])
        w.writeheader()
        w.writerows(ev)
    with (OUT / "new_disclosure_documents.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["document_id", "document_type", "title",
                                          "original_url", "published_at", "rcept_no"])
        w.writeheader()
        w.writerows(docs.values())

    kinds = {}
    for r in ev:
        kinds[r["source_kind"]] = kinds.get(r["source_kind"], 0) + 1
    print(f"근거 {len(ev):,}  {kinds}")
    print(f"  문서 id 확보 {sum(1 for r in ev if r['uuid']):,}")
    print(f"  URL 만 있음(뉴스) {sum(1 for r in ev if not r['uuid'] and r['source_url']):,}")
    print(f"새로 만들 공시 문서 {len(docs):,}건")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
