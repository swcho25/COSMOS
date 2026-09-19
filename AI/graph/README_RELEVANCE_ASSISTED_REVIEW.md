# AI 추천을 참고하는 간단 뉴스 검수

2026-09-17 사용자 선택100건을 수령했다. 채택69건·제외31건·보류/미선택0건이며,
원본과 선택별 원문은 `artifacts/relevance_assisted_results_20260917_v1/`에 보존했다.
AI추천과 다른13건 및 다음 검토사항은 해당 폴더의 `RESULTS.md`를 읽는다.

사용자가 판정 이유와 원문 인용을 직접 작성하기 어렵다는 피드백을 반영했다.
AI가 먼저 뉴스·대상 기업별 추천, 한국어 이유, 원문 근거를 작성한다.
사용자는 **채택 / 제외 / 잘 모르겠음** 중 하나만 선택한다.
선택은 해당 뉴스와 기업의 경제적 관련성에 관한 의견이다. 실제 주가 변동을 맞히는 문제가 아니다.

## 화면과 사용법

`artifacts/relevance_two_stage_data_20260917_v1/assisted_review_ko.html`을 연다.
원본 100건 중 영문 50건에는 한국어 번역도 제공한다.

1. 대상 기업, 기사, AI 추천 이유를 읽는다. 원문 근거는 펼쳐 확인할 수 있다.
2. 채택·제외·잘 모르겠음 중 하나를 누른다. 이유·원문 인용·검수자 이름은 필수가 아니다.
3. 필요하면 이전 기사로 돌아가 선택을 바꾸거나 미선택으로 되돌린다.
4. 창을 닫기 전 결과 파일을 내보낸다. 다음에는 파일을 불러와 이어간다.

기존 `human_review.html`에 작성 중인 내용이 있으면 그 창에서 먼저 내보낸다.
새 화면은 기존 검수 파일도 읽을 수 있다. 완료된 기존 검수의 선택을 가져오고,
기존의 이유·인용·검수자 등 원래 기록은 `prior_annotation`에 보존한다.
기존 화면과 `human_review_ko.html`은 그대로 남겨 둔다. 열린 탭을 강제로 새로고침하지 않는다.

## 채택의 뜻

- 기업의 신규 실적, 계약, 수요, 비용, 자금조달, 규제, 소송 등은 후보가 될 수 있다.
- 거시경제·산업·투자심리 기사도 대상 기업의 사업과 연결되는 근거가 있으면 후보가 될 수 있다.
- 다른 기업 이야기, 인물 약력의 과거 직장, 광고·행사 명단의 이름만으로는 채택하지 않는다.
- 기업 귀속이나 사건 시점이 불명확하면 보류한다. 사용자의 ‘잘 모르겠음’도 제외로 바꾸지 않는다.

관련 뉴스인지 판정하는 단계와 실제 수익률에서 학습 라벨을 만드는 단계는 다르다.
후자의 방향·강도는 이후 별도 가격 데이터와 시간 기준으로 계산해야 한다.

## AI 추천과 사람의 선택을 구분

새 출력은 `cosmos-assisted-selection-1` 형식이다.
AI 추천 전체와 해시, `human_selection`, `human_selected_at`을 따로 저장한다.
사용자가 버튼을 누르기 전 사람의 선택은 비어 있으며 AI 추천을 자동 확정하지 않는다.
`review_mode=ai_assisted_selection`, `independent_human_gold=false`를 기록한다.
추천을 본 뒤 고른 결과이므로 기존의 모델 예측을 가린 독립 검수와 동일하게 평가하지 않는다.
이 형식을 기존 strict 검수 검증기에 억지로 맞추거나 사람의 이유·인용을 만들어 채우지 않는다.

추천은 기존 32건의 원문 기반 AI 검토를 재사용하고 나머지 68건은 별도로 원문을 읽어 작성했다.
기사별 입력 해시와 정확한 원문 인용을 확인하지만, 그 검증 자체가 AI 판단의 정확성을 보장하지는 않는다.
실제 사람이 선택하지 않은 결과를 사람이 작성한 정답으로 주장하지 않는다.
이 작업은 GNN 재학습이나 서비스·HDFS 적재를 실행하지 않는다.

## 재생성

```powershell
$data = 'AI/graph/artifacts/relevance_two_stage_data_20260917_v1'
$translated = 'AI/graph/artifacts/relevance_review_translation_20260917_v2/translations.jsonl'
$suggestions = 'AI/graph/artifacts/relevance_assisted_review_20260917_v1/assessments.jsonl'
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/build_relevance_assisted_review.py --inputs "$data/human_inputs.jsonl" --translations "$translated" --assessments "$suggestions" --out "$data/assisted_review_ko_new.html"
```

원문·번역·추천 파일은 기존 Git 제외 경로에만 있다. 출력 파일은 덮어쓰지 않는다.
원문/추천 해시가 바뀌었거나 다른 기사의 결과 파일이면 불러오기를 거부한다.
