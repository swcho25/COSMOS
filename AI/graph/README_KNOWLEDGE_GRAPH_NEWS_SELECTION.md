# 지식그래프용 뉴스·기업 선별

기업명이 등장한 모든 뉴스를 연결하면 인물 약력에 나온 과거 직장까지 관련 뉴스가 된다.
이 모듈은 **기사×기업**별 경제적 관련성을 판단해 뉴스→기업 연결 후보를 만든다.
가격예측 학습용 신규성·주가 라벨 조건을 일반 지식그래프 선정에 적용하지 않는다.

## 구성

- `knowledge_graph_relevance_policy.py`: 직접관련/간접관련/단순언급/불명확 판정과 원문 근거 ID 검증.
- `run_knowledge_graph_relevance.py`: 이미 설치한 Qwen3-4B-Instruct-2507로 두 차례 판정. 가중치 재학습 없음.
- `knowledge_graph_news_preview.py`: 채택한 기사×기업만 뉴스→기업 노드/엣지로 내보내는 로컬 도구.
- `knowledge_graph_lake_gate.py`: 기존 뉴스 분석 결과에서 채택한 기업 언급과 양 끝 기업이 채택된 관계 후보만 남기는 순수 함수. 기존 적재 코드에 자동 연결되어 있지 않음.
- `evaluate_knowledge_graph_relevance.py`: 같은 전체 표본에 대한 두 AI 참고판정과 실행 결과를 비교. 사람 정답 정확도 산출 도구가 아님.

같은 기사에서 A기업 실적은 채택하고 B기업이 등장한 약력은 제외할 수 있다.
과거 실적·계약도 관련 있으면 보존한다. 기사 발행일을 사건 발생일로 대입하지 않는다.
명시적 근거가 없는 산업 노출이나 그룹사 관계를 외부 상식으로 보충하지 않는다.

두 차례 판정은 같은 모델을 사용하므로 독립 검증이 아니다. 두 관계 값이 일치하고
원문 인용 검증을 통과해야 채택/제외하며, 불일치·형식 오류·길이 초과는 보류한다.
전체 제목·본문을 전달하며 긴 기사를 조용히 자르지 않는다. 본문 내 명령은 분석 대상이다.

## 입력·출력 계약

입력 필수 필드: `article_id`, `ticker`, `company_name`, `published_at`, `title`, `body`, `source_url`.
선택 필드: 위 쌍 ID, `input_sha256`, `decision`, `relation`, `evidence`, `reason`,
`policy_version`, `model_provenance`. 정확한 계약은 preview 모듈 docstring을 따른다.

`decision`은 `include`/`exclude`/`review`다. 증거는 title/body 원문 인용이며,
SHA256은 위 7개 입력 필드에 바인딩된다. 제외와 보류도 버리지 않고 감사 파일로 남긴다.

미리보기 출력:

- `news_nodes.jsonl`, `company_nodes.jsonl`: 채택 연결에 참여하는 노드.
- `selected_links.jsonl`: `HAS_ECONOMIC_INFORMATION_ABOUT` 뉴스→기업 연결.
- `selection_audit.jsonl`: 모든 입력 쌍의 판정과 근거.
- `review.jsonl`, `excluded.jsonl`: 보류/제외 기록.
- `manifest.json`: 모든 파일 기록 완료 후 게시하는 해시·전체 분모·선정 수 영수증.

미리보기의 기업 식별자는 ticker다. 복수 종류 주식의 발행인 통합이나 모든 과거 법인
대응을 완성한 것은 아니다. 사건 노드는 추출하지 않았으며 `event_date=null`로 보존한다.
기업 두 곳이 채택되었다고 공급·제휴·경쟁 관계가 입증되지는 않는다.
기존 관계 후보를 남길 때도 관계 술어·방향·근거의 별도 검증이 필요하다.

## 실행

저장소 루트 PowerShell, 기존 환경만 사용한다. 각 출력 경로는 없어야 한다.

```powershell
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/run_knowledge_graph_relevance.py --inputs AI/graph/artifacts/kg_news_selection_v1_20260917_v1/evaluation_data/new_inputs.jsonl --out AI/graph/artifacts/kg_news_selection_v1_20260917_v1/model_run
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/knowledge_graph_news_preview.py --inputs AI/graph/artifacts/kg_news_selection_v1_20260917_v1/evaluation_data/new_inputs.jsonl --selections AI/graph/artifacts/kg_news_selection_v1_20260917_v1/model_run/selections.jsonl --out AI/graph/artifacts/kg_news_selection_v1_20260917_v1/graph_preview
```

첫 실행기는 기존 파일럿 입력 경계를 유지해 실제 자료는 `case_kind=real`,
`source_split=train`, 2020~2022년, 비어 있지 않은 제목/본문을 요구한다.
전 기간 운영 데이터나 제목만 있는 기사까지 처리하는 운영 분류기가 아니다.
preview는 더 일반적인 7필드 입력을 지원하지만 전체 데이터를 메모리에 읽는 로컬 도구다.
HDFS 전체 처리는 별도의 분산 실행·실패 복구·증분 적재 설계가 필요하다.

## 기존 적재 코드에서 사용할 위치

기존 흐름은 정제 → `lake_news_normalize.analyze_article` → 기업·관계 집계다.
이 사이에 선별 결과를 적용해야 보류/제외 기업이 회사 지표와 관계 점수로 다시 유입되지 않는다.
정제 원문과 원래 언급 결과는 보존하고, 선정 결과를 별도로 조인한다.
lake의 정제 행 `record_id`를 이 모듈의 `article_id`에 매핑해야 한다.

실제 적재 입력은 `analysis["mentions"]`의 **모든 기업마다 한 행씩** 만들어야 한다.
이번 평가의 32개 기사×기업 쌍은 각 기사의 다른 모든 기업을 분류한 결과가 아니다.
gate는 원래 언급 기업 일부의 판정이 누락되면 적용을 거부한다. 후보 작성 예시는 다음과 같다.

```python
analysis = analyze_article(cleaned_row, matcher)
inputs = [dict(
    article_id=cleaned_row["record_id"], ticker=mention["ticker"],
    company_name=mention["name_official"], published_at=cleaned_row["published_at"],
    title=cleaned_row["title"], body=cleaned_row["body"],
    source_url=cleaned_row.get("canonical_url"),
) for mention in analysis["mentions"]]
# selections: 해당 전체 inputs와 SHA가 일치하는 별도 분류 결과
filtered = apply_selection(analysis, inputs, selections)
# filtered를 회사/관계 집계와 _write_analysis에 전달한다.
```

위 일반 후보 작성은 학습 구간 지정이 아니다. 파일럿 실행기에 넣으려고 임의로
`source_split=train`을 붙이면 안 된다. 기간·코호트 검증은 별도로 수행해야 한다.
원래 analyzer는 전체 원문 해시를 반환하지 않으므로 gate는 입력/판정 SHA와 원문 위치를
검사하되 analyzer의 전체 입력 동일성까지 입증하지는 않는다. 같은 cleaned row로
analyze와 후보 작성을 이어서 호출해야 하며, 저장된 분석을 재사용하려면 분석 시점 해시가 필요하다.

현재 추가한 것은 이 연결을 구현·시험할 수 있는 로컬 선별기·미리보기·선별 함수다.
기존 HDFS/서비스/DB를 변경하거나 전체 이력을 재적재하지 않는다.
분류 결과는 `human_gold=false`, `ready_for_gnn_training=false`, `production_approved=false`다.
실측 결과와 적용 판단은 `KNOWLEDGE_GRAPH_NEWS_SELECTION_RESULTS.md`를 확인한다.
