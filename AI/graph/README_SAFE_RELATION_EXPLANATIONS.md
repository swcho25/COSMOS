# 현재 권장하는 관계 설명

2026-09-19 후속 v5 관계 확장 결과는 `artifacts/kg_integrated_20260919_v3`(119관계)다. 같은 설명기를 `directory=...`로 지정해 전체119관계 조회를 검증했다. 기존 호출의 기본값은 재현을 위해 v2로 유지하므로 **새 결과를 사용할 때 v3 경로를 명시**한다. [추가 관계의 실제 응답](artifacts/kg_integrated_20260919_v3/new_relationship_examples.json)과 [범위·한계](README_RELATION_COVERAGE_V5.md)를 참고한다.

`safe_integrated_relation_explanations.py`를 사용한다. 뉴스·공시에서 두 기업의 지정된 관계를 검색한 뒤, **고정된 관계 유형 안내와 정확한 원문 인용·출처**만 반환한다. 모델 다운로드나 GPU가 필요 없다.

```python
from safe_integrated_relation_explanations import SafeIntegratedRelationExplainer

explainer = SafeIntegratedRelationExplainer()  # 완료한 kg_integrated_20260919_v2
result = explainer.explain(
    "032640", "035720", "PARTNER", as_of="2026-09-16T00:00:00Z", limit=5
)
```

`SUPPLY`·`INVEST`는 방향을 유지하고 `PARTNER`·`COMPETE`만 양방향으로 검색한다. 근거의 `available_at`은 조회 기준보다 엄격하게 이전이어야 한다. 시간대가 불명인 자료의 보수적인 이용 가능 시각과 원래 발행 시각을 함께 보존한다. 과거 기록을 현재 계약·소유관계로 바꾸지 않는다.

근거가 있으면 `EXTRACTIVE_FALLBACK`, 없으면 `NO_EVIDENCE`다. 항상 `mode: extractive`, `generator_used: false`, `nli_used: false`이며, 원문 인용 외에 기업별 지분율·매출·수주 목표를 해석한 구조화 수치를 만들지 않는다. 공시의 보고기업·법인코드는 검증된 메타데이터로 표시하고 원문에 끼워 넣지 않는다. 회사명 대신 티커를 안내에 사용하므로 현재 사전을 과거 법인명의 근거로 사용하지 않는다.

기존 `GroundedRelationExplainer`와 `IntegratedRelationExplainer`의 자유 생성 방식은 **연구 비교용으로 보존**한다. 실제 로컬 Qwen/NLI 검증에서 “카카오와 동맹한 LG유플러스”를 “카카오와 함께 AIDC 건설 계획을 세웠다”로 확대했는데도 NLI가 통과시킨 사례가 확인됐다. NLI 통과는 정확도 인증이 아니다. 새 모듈은 특정 문구만 차단하는 대신 모든 관계 유형에서 자유 생성 요약을 채택하지 않는다.

실패 원문·생성문·NLI 점수는 `artifacts/kg_integrated_20260919_v2/graphrag_cpu_qa_20260919/`에 남긴다. 보완 검증 기록은 같은 그래프의 `safe_explanation_qa_20260919/`에 저장한다. 새 방식은 설명에서 주체·숫자를 만들어내는 경로를 제거하지만, 관계 추출 결과 전체의 의미적 정확도를 증명하지 않는다. 운영 서비스 연결·배포는 별도 작업이다.
