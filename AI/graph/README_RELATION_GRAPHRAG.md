# 뉴스 근거를 사용하는 관계 GraphRAG

`grounded_relation_graphrag.py`는 완료한 로컬 KG에서 **두 기업의 지정된 관계를 실제로 뒷받침하는 뉴스 문장**을 검색하고 설명한다. 기존 GNN 번들의 통계 그래프나 기업 공동 등장 기사를 설명 근거로 추가하지 않는다. 공시는 검색하지 않으며 응답에 `source_kind: NEWS`, `disclosures_searched: false`를 표시한다.

지원 관계는 `SUPPLY`, `INVEST`, `PARTNER`, `COMPETE` 네 가지다. 공급·투자는 방향을 유지하고 협력·경쟁만 역순 조회를 허용한다. `INVEST`의 세부 유형이 있으면 `relation_subtype`을 보존한다. 과거 인수 근거를 현재 지분 보유 사실로 바꾸지 않는다.

```python
from grounded_relation_graphrag import GroundedRelationRetriever, GroundedRelationExplainer

reader = GroundedRelationRetriever(
    "AI/graph/artifacts/kg_grounded_full_20260918_v1",
    relation_db="AI/graph/artifacts/kg_grounded_relations_20260919_v2/state.sqlite3",
)
explainer = GroundedRelationExplainer(
    reader, bundle="AI/graph/artifacts/news_impact_v2_bundle", device="cuda"
)
result = explainer.explain("005930", "047040", "PARTNER", as_of="2026-09-18")
```

초기화 시 부모 manifest·protocol·DB 및 선택한 관계 수정본의 부모 연결/DB SHA를 검증한다. 완료 DB의 WAL은 비어 있어야 한다. 대용량 DB를 요청마다 해시하지 않도록 reader를 재사용하고, 산출물이 바뀌면 새 reader를 만든다. 관계 수정본은 `cosmos-grounded-relations-revision-1` 계약을 사용하며 원문 기사는 부모 DB에서 읽는다.

CLI의 `--retrieval-only`는 모델을 실행하지 않는다.

```powershell
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/grounded_relation_graphrag.py --kg-dir AI/graph/artifacts/kg_grounded_full_20260918_v1 --relation-db AI/graph/artifacts/kg_grounded_relations_20260919_v2/state.sqlite3 --src 005930 --dst 047040 --type PARTNER --retrieval-only
```

각 근거는 관계 `edge_id`, 기사 ID, 원문 인용·문자 위치, 제목, 발행 시각, 원본 URL, `[E1]` 형태의 인용 ID를 포함한다. 같은 기사와 같은 문장의 중복만 제거한다. 기준일 이후·발행일 불명 근거를 제외하며, 시각 단위 cutoff는 명시적 시간대를 요구한다. 시간대가 없는 기사는 그 시각 cutoff 조회에서 제외한다. 발행 시각은 사건 발생일이나 현재 계약 유효성의 증명이 아니다.

근거가 없으면 `NO_EVIDENCE`와 빈 목록을 반환하며 LLM을 호출하지 않는다. 근거가 있으면 로컬 Qwen과 기존 인용·숫자·계획 표현/NLI 검사로 1~2개 설명을 만든다. 두 기업의 원문 명칭을 보존해야 하며 검증 실패, 모델 실패, 입력 길이 초과에는 `EXTRACTIVE_FALLBACK`으로 정확한 원문을 반환한다. 긴 입력을 자르지 않는다. `GENERATED`도 사실·정확도 인증을 뜻하지 않으며 `production_approved`, `graph_quality_validated`, `price_effect_established`는 false다.

이 모듈은 GNN 점수·감성 결과를 계산하거나 변경하지 않는다. HTTP/프론트 연동·서비스 배포·HDFS/DB 적재도 수행하지 않는다. 공시를 추가하려면 실제 공시 ID·발행 시각·URL·원문 위치를 갖춘 별도 근거 계약과 검증이 필요하다.

## 2026-09-19 실제 실행

관계 수정본은 공급4·투자/지분14·협력6·경쟁1의25개 관계와72개 근거 문장을 생성했다. 투자/지분 근거57건은 기사에 적힌 기존 최대주주 지분 보유이며 새로운 투자 거래57건이 아니다. 보수적인 추출 문법의 재현율과 과거 법인명 귀속 검증에 한계가 남는다.

CPU15개 검사와 실제 Qwen/CUDA·NLI/CPU 실행으로 네 종류를 확인했다. 공급·경쟁은 검증된 요약, 투자/지분·협력은 정확한 원문으로 대체해 반환했다. `artifacts/kg_relation_contract_20260919/real_graphrag_smoke.json`과 `GRAPHRAG_EXAMPLES.md`에 원문·URL·모델 결과·검증 상태를 보존했다. 기사별 기존 감성은 수정하지 않았다. 이는 로컬 기능과 출처 정합성 검사이며 정확도 평가나 운영 반영 완료가 아니다.
