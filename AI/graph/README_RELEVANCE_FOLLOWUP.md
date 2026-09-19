# 뉴스 관련성 선별 후속 비교

완료 결과는 [RELEVANCE_FOLLOWUP_RESULTS.md](RELEVANCE_FOLLOWUP_RESULTS.md)를 읽는다.
새32건에서 참고 AI 채택과 일치하는 선택이 기존12건→수정2건으로 감소했으며,
검증 실패는6건→22건으로 증가했다. 수정판을 대량 선별기로 채택하지 않는다.

기존4B 모델은 과거에 매각된 사업을 현재 기업에 귀속하거나 단순 언급까지 대부분
보류했다. 후속 작업은 모델 가중치를 재학습하지 않고, 같은 모델의 사실 추출과 선택
정책을 바꾸어 비교한다. 원래 파일럿 결과와 코드는 보존했다.

## 바뀐 판정 방식

`relevance_grounded_policy.py`는 모델에 다음 사실과 각각의 원문 구간 ID를 요구한다.

1. 대상 기업의 역할: 현재 당사자 / 현재 영향 대상 / 과거 소속 / 단순 언급 / 불명.
2. 새 사건인지, 과거 회고인지, 시점이 불명확한지.
3. 매출·비용·수요·공급·자금조달·규제·소송·경영권·사업 운영 중 근거 있는 범주.

코드가 귀속·시점·경제 근거를 확인하고 선택을 결정한다. 현재 역할+새 사건+구체 경제
범주가 모두 근거를 가질 때 채택한다. 과거 소속이나 단순 언급은 근거가 있을 때 제외한다.
잘못된 출력과 근거 미비는 보류한다. 보류를 무관한 기사라는 학습 정답으로 바꾸지 않는다.

당시 기업 정보가 없는 경우, 채택에 사용하는 귀속 근거에는 대상 기업명 또는 티커가
실제로 있어야 한다. Inc./Ltd. 등 법적 접미사·주식 종류 표기는 정규화하지만, 사명 변경이나
지주/자회사·합병 전후 기업을 자동 연결하지 않는다. 이 검사는 미확인 별칭의 관련 기사도
보류할 수 있다. 역사적 기업 사전이 완성된 것은 아니다.

경제 경로는 모델의 자유로운 미래 인과 설명을 받지 않고, 선택한 범주와 원문 근거를 코드로
묶어 보여준다. 요약과 사실 분류 자체에는 여전히 모델 오판 가능성이 있다. 존재하는 인용이
경제적 인과나 현재 소유관계를 입증하는 것은 아니다.

사용자 프롬프트 원문은 `news_relevance_prompt.txt`에 그대로 보존한다. 추가 지침·출력
구조·선택 규칙의 변경은 별도 정책 파일에 기록한다. 원문은 누락 없이 전체 입력하며,
입력 한도 초과도 기록과 평가 분모에 남긴다. 외부 API로 기사를 전송하지 않는다.

## 비교 범위와 동결

- 개발 사례: 이미 검토한 실제40건과 가상8건. 이전 v4 출력과 새 정책을 비교한다.
- 새 사례: 기존에 검토한448기사 및 기존 val/test11,969기사와 ID·정규 URL·raw/clean/
  저장 본문 해시 중복을 제외한2020–2022 기사 중32건. 국내16·해외16이다.
- 뉴스 사건 키워드나 이후 가격을 기준으로 뽑지 않았다. 200–6000자 본문 길이 범위를
  먼저 정한 뒤 기사와 대상 기업을 SHA256 순서로 선택했다. 따라서 전체 뉴스의 무작위
  모집단 표본이나 국내외 실제 구성비를 대표한다고 주장하지 않는다.
- 새 원천의 이 기간135,251행 중 국내120,159/해외12,163행이 추출 적격이었다.
  표본32건의 정제 본문은469–5535자이며 기사당 기업1개다.
- 다른 AI가 모델 결과를 보지 않고 전체 원문을 검토한 참고 판정을 따로 저장한다.
  사람 검수 정답이 아니며 AI끼리 일치해도 확정 학습 라벨이 아니다.
- 두 방식은 모델·4비트 설정·난수 시드 규칙·생성 옵션·전체 입력·토큰 제한을 동일하게
  유지한다. 프롬프트 구조와 선택 정책을 묶어 바꾼 비교이므로 개별 변경 하나의 효과를
  분리해 입증하는 실험은 아니다.
- 정책 작성자는 새 기사 본문·참고 판정·모델 결과를 열기 전에
  `experiment_freeze.json`에 입력·코드·원문 프롬프트 해시와 비교 계획을 고정했다.

새 후보라도 이후 GNN 학습에서 사용할 가격 라벨·상장 시점·시각 정합성 검사는 별도다.
2025년 사전학습 모델에 후대 지식이 있을 가능성도 남으므로 당시 배포 가능한 모델의
성능이라고 주장하지 않는다. 보류·오류·미채택을 모두 포함해 같은 쌍에서 비교한다.

## 실행 파일과 경로

- `prepare_relevance_followup.py`: 새32건 표본·출처·중복 제외 검사.
- `relevance_grounded_policy.py`: 사실 추출 입력·출력 계약과 선택 규칙.
- `run_relevance_followup.py`: 기존 방식/새 정책의 별도 GPU 실행·해시 검증·재개.
- `compare_relevance_followup.py`: 같은 쌍 전체 비교·검토 후보·사람 검수 입력 생성.

새 입력·독립 참고 판정: `artifacts/local_relevance_followup_20260916_v2/`.
실행·비교 기록: `artifacts/local_relevance_policy_20260916_v2/`.
디렉터리 날짜는 작업 시작일이며 자정 이후 기록은 실제 ISO 시각을 사용한다.

```powershell
$data = 'AI/graph/artifacts/local_relevance_followup_20260916_v2'
$run = 'AI/graph/artifacts/local_relevance_policy_20260916_v2'
AI/ner/.venv/Scripts/python.exe -X utf8 -u AI/graph/run_relevance_followup.py --inputs "$data/new_inputs.jsonl" --out "$run/fresh_baseline" --policy baseline
AI/ner/.venv/Scripts/python.exe -X utf8 -u AI/graph/run_relevance_followup.py --inputs "$data/new_inputs.jsonl" --out "$run/fresh_grounded" --policy grounded
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/compare_relevance_followup.py --inputs "$data/new_inputs.jsonl" --reference "$data/independent_reference_01.jsonl" "$data/independent_reference_02.jsonl" --baseline "$run/fresh_baseline/results.jsonl" --grounded "$run/fresh_grounded/results.jsonl" --cohort fresh --freeze "$run/fresh_reference_freeze.json" --out "$run/comparison_fresh"
```

동일 해시일 때만 추론을 재개한다. 이미 생성한 비교 폴더는 덮어쓰지 않는다.
GPU 실행은 하나씩 한다. 모델 파일·결과·뉴스 본문은 기존 Git 제외 경로에만 저장한다.
새 표본 비교에는 참고판정·입력·정책의 동결 파일 검증이 필수다. 생성한 비교·후보·사람
검수 파일의 SHA256도 summary에 남긴다. 비교기만 보강한 개발 재계산은
`comparison_development_v2/`에 저장하며 최초 `comparison_development/`도 보존한다.

가상 smoke 첫 실행에서는 경제 범주 none에 부재의 근거 ID를 붙인 것을 불필요하게 오류로
보류했고, 회사 정보 없는 산업 사건을 채택했다. 원출력을 보존하고, 부재 근거 허용과
대상 이름/티커 또는 당시 정보 검사를 추가한 뒤 정책을 동결했다. 실제 새 비교 결과를 보고
재튜닝한 수정이 아니다. `smoke/`와 `smoke_v2/`는 개발 기록이며 독립 검증 점수가 아니다.

사람 검수는 비교 폴더의 `human_review_packet.jsonl`에서 원문을 읽고 비어 있는
`human_selection`, `human_label`, `human_time_status`, `human_evidence_quotes`, `human_reason`을
작성한다. 검수 파일에는 두 모델의 예측을 넣지 않았다. 현재 사람 검수 완료는0건이다.
