# 뉴스 선별 기준과 평가 참고자료

실제 판정 계약은 `news_selection_rubric_v1.md`, 실행 가능한 검증·집계 코드는
`news_selection_rubric.py`다. 원래 실험의 ‘AI 채택19건’을 확정된 주가예측 학습 정답으로
취급하지 않도록 관련성·신규성·경제 내용·증권 범위·주장 상태·중요성을 분리했다.

## 출력의 의미

| 필드/값 | 의미 |
|---|---|
| `candidate_status=candidate` | 원문상 해당 대상의 새 경제 정보가 확인되는 의미상 후보 |
| `candidate_status=review` | 귀속·신규성·구체성·증권 대응 등이 불명확해 재검토 |
| `candidate_status=exclude` | 단순언급·경제정보 없음·대상 사건의 과거 회고/반복만 존재 |
| `materiality=not_established` | 중요성 근거 부족. 무관·주가 무변동을 뜻하지 않음 |
| `agreement_status=agreed_*` | 두 AI의 **축 값** 일치. 사건·근거 해석이나 정답의 일치를 보장하지 않음 |
| `agreement_status=disputed` | 축 값에 이견이 있어 그대로 보존하고 재검토 |
| `training_readiness=pending_checks` | 시각·증권·중복·가격 구간 등의 학습 적격성 검증 전 |

모든 기록은 `human_gold=false`, `usable_as_training_gold=false`,
`ready_for_gnn_training=false`다. 새 참고자료가 기존 사람 선택이나 GNN 학습 라벨을 대체하지 않는다.
불명을 임의로 제외로 바꾸지 않고, 모델 성능에 맞춰 참고판정을 고치지 않는다.

## 검토 파일 생성

1. 입력은 원문을 보존한 `{article_id,ticker,company_name,published_at,title,body,company_context,
   source_url,source_split,case_kind}`이다. 이 버전의 `company_context`는 null이다.
2. 검토자는 전체 제목·본문을 읽고 동일 대상 사건에 대해 6축과 정확한 인용을 기록한다.
   출력 스키마는 `news_selection_rubric.FIELDS`, 각 축 값은 `AXES`를 따른다.
3. `validate(review, input)`는 계약·입력 SHA·원문 인용·노출 상태 등을 검증한다.
   최종 의미상 후보는 코드가 계산하며, 단순 인용 일치가 의미적 정당성의 증명은 아니다.
4. 이미 본 자료를 다시 판단하면 `review_context=development_relabel`로 기록한다.
   노출 여부를 정직하게 표시하고 새로운 blind 평가로 취급하지 않는다.
5. 새로운 평가에서는 두 AI가 **같은 기사 전부**를 각각 읽는다. 모델 출력과 상대 판정을 숨긴다.
   같은 모델 계열의 두 검토자가 공통 오류를 낼 수 있다는 한계를 명시한다.

## 집계

저장소 루트 PowerShell, 기존 Python 환경을 사용한다.

```powershell
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/news_selection_rubric.py --inputs AI/graph/artifacts/relevance_rubric_v1_20260917_v1/evaluation_data/new_inputs.jsonl --first AI/graph/artifacts/relevance_rubric_v1_20260917_v1/fresh_reviews_a.jsonl --second AI/graph/artifacts/relevance_rubric_v1_20260917_v1/fresh_reviews_b.jsonl --out AI/graph/artifacts/relevance_rubric_v1_20260917_v1/paired_evaluation
```

출력 디렉터리가 이미 있으면 덮어쓰기를 거부한다.
`paired_reviews.jsonl`은 두 원판정과 모든 이견을 보존하고,
`human_review_queue.jsonl`은 이견이 있는 우선 검토 대기 목록이다.
이 목록에는 AI 답이 들어 있으므로 **독립 사람 정답 수집 화면에 그대로 노출하면 안 된다.**
독립 검토에는 실험 루트의 `human_blind_inputs.jsonl`(새32건 전체 원문만)을 사용한다.
이견 사례만 골라 사람에게 보여주면 표본이 편향될 수 있으므로 전체32건을 보존했다.
일치 사례도 독립 사람 검토가 없으므로 학습 gold는 아니다.
`summary.json`은 파생 후보 수와 축별 일치 수만 집계하고 정확도를 계산하지 않는다.

## 이번 작업의 범위

`artifacts/relevance_rubric_v1_20260917_v1/plan.json`으로 규약과 코드를 먼저 동결했다.
기존32건은 별도 개발 참고로 재판정하고, 과거검토686기사/가격val-test11969기사를
배제한 새로운32건에 두 AI 판정을 수집했다. 지역별16건의 본문200~6000자 표본이며
전체 데이터의 대표 표본이나 독립 무작위 반복실험으로 해석하지 않는다.

모델 재학습·기존 생성 프롬프트 덮어쓰기·GNN 번들 교체·서비스/HDFS 갱신을 수행하지 않는다.
기존 프롬프트와 실험은 재현을 위해 보존하며, 새 기준을 모델에 적용할 실험은 이 자료의
정답 출처와 적격성을 검토한 후 별도 버전으로 해야 한다. 실측 결과는 `NEWS_SELECTION_RUBRIC_RESULTS.md`에 기록한다.
