# 두 단계 뉴스·기업 관련성 선별

**간단 선택 화면:** 이유·원문 근거를 AI가 먼저 작성하고 사용자가 채택/제외/잘 모르겠음만
고르는 방식은 [README_RELEVANCE_ASSISTED_REVIEW.md](README_RELEVANCE_ASSISTED_REVIEW.md)를 읽는다.
이는 AI 추천을 본 뒤의 선택이며, 아래의 예측을 가린 독립 검수와 구분해 저장한다.

구현·실측 결과는 [RELEVANCE_TWO_STAGE_RESULTS.md](RELEVANCE_TWO_STAGE_RESULTS.md)를 읽는다.
새32건에서 참고 AI도 채택한 선택8→10건, 충돌 채택2→1건, 출력 형식 오류7→0건이었다.
사람 기준 정확도나 GNN 성능 개선을 입증한 것은 아니며,100건 검수 양식을 준비한 상태다.

같은 로컬 Qwen3-4B-Instruct-2507 가중치로 뉴스 선별 절차를 나눈 실험이다.
GNN·LLM을 재학습하지 않으며 서비스 연결이나 HDFS 재적재도 하지 않는다.
모델 파일과 뉴스 원문·결과는 기존 Git 제외 경로에만 저장한다.

## 판정 방식

1. **경제 관련성 선별:** 대상 기업의 사업·실적·비용·위험에 연결되는 정보인지 판단한다.
2. **채택 후보 검토:** 1차가 유효한 채택일 때만 원문 전체를 다시 읽고 귀속·신규성·경제
   사건을 함께 판단한다. 1차 답·이유·근거 선택은 2차 입력에 전달하지 않는다.

각 단계의 생성 출력은 두 키뿐이다.

```json
{"decision":"adopt","evidence_ids":[2,5]}
```

`decision`은 adopt/exclude/hold다. 원문 구간ID를 코드가 정확한 인용으로 복원한다.
이전의 근거2개/3개 상한은 제거했고, 전체 카탈로그 안의 유효한 고유ID만 허용한다.
채택·제외는 근거가 있어야 하며, 파싱 실패·없는 ID·중복 ID·생성 중단은 보류한다.
1차 제외/보류/오류는 2차를 실행하지 않는다. 두 단계가 모두 유효한 채택일 때 최종 채택한다.
1차 채택 후 2차 제외는 최종 제외, 2차 보류/오류는 최종 보류다.

근거ID는 원문 존재만 보장한다. 기업 귀속·시점·경제 의미가 맞는지까지 코드가 증명하지는
못한다. 두 단계는 **같은 모델**이므로 답을 숨겼어도 독립된 전문가의 검증으로 볼 수 없다.
요약·주가 방향·자유로운 인과 경로를 생성하지 않으며 관련성·시점별 세부 점수도 만들지 않는다.
출력 형식은 바뀌었지만 사용자 원문 `news_relevance_prompt.txt`는 그대로 보존했다.

기업 정보가 없는 경우 기사에서 확인되는 사업·계약·노출은 판단할 수 있다. 그러나 현재
기업 사전으로 과거의 소유·그룹·사명 변경을 메우지 않는다. 역사적 기업 관계 사전을 새로
완성한 것은 아니며 자료가 부족한 연결은 보류 대상으로 남는다.

## 고정 비교와 범위

- 기존 검토480기사와 기존 val/test11,969기사의 ID·URL·raw/clean/저장 본문 해시 중복을 제외했다.
- 2020–2022년 기사, 정제 본문200–6000자 조건에서 해시 순서로 국내50/해외50을 뽑았다.
  선택에 사건 키워드·모델 예측·미래 가격을 사용하지 않았다. 전체 뉴스의 구성비를 대표하지 않는다.
- 100건 안의 국내16/해외16, 총32건을 비교용으로 별도 고정했다. 나머지68건은 이번 GPU 비교에
  쓰지 않는다. 검수 화면에 모두 포함하므로 앞으로 사용자에게 노출되지 않은 표본이라고 보장하지 않는다.
- 기존 방식 / 새1차만 / 새2단계 최종 결과를 동일32쌍에서 비교한다. 보류·오류도 분모에 포함한다.
- 모델 revision·NF4·본문 전체·T=0.7/top_p=0.8/top_k=20·단계당6144입력/768생성 제한을 맞췄다.
  1차 시드는 기존과 같고 2차는 기록된 별도 시드다. 재시도는 없다. 2차 호출 비용도 합산한다.
- 정책/런타임은 새 본문·참고판정·출력을 보기 전에 고정했다. 가상8건 smoke에서 오류0건을
  확인했으며 이8건을 새 표본 성능으로 취급하지 않는다.
- 다른 AI가 모델 출력 없이 전체 원문을 읽어 별도 참고판정을 작성했다. **사람 검수는0건이며
  AI 일치율은 사람 기준 정확도나 주가 예측력이 아니다.** 관련 뉴스라도 실제 주가가 안 움직일 수 있다.

## 실행

환경은 기존 `AI/ner/.venv/Scripts/python.exe -X utf8`와 RTX4050 Laptop6GB다.
기존 attention adapter와 모델 파일의 해시를 확인하고, 한 번에 GPU 프로세스 하나만 실행한다.

```powershell
$data = 'AI/graph/artifacts/relevance_two_stage_data_20260917_v1'
$run = 'AI/graph/artifacts/relevance_two_stage_run_20260917_v1'
AI/ner/.venv/Scripts/python.exe -X utf8 -u AI/graph/run_relevance_followup.py --inputs "$data/eval_inputs.jsonl" --out "$run/baseline" --policy baseline
AI/ner/.venv/Scripts/python.exe -X utf8 -u AI/graph/run_relevance_two_stage.py --inputs "$data/eval_inputs.jsonl" --out "$run/two_stage"
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/compare_relevance_two_stage.py --inputs "$data/eval_inputs.jsonl" --reference "$data/independent_reference_01.jsonl" "$data/independent_reference_02.jsonl" --baseline "$run/baseline/results.jsonl" --two-stage "$run/two_stage/results.jsonl" --freeze "$run/experiment_freeze.json" --out "$run/comparison"
```

추론은 동일 해시·입력·옵션으로만 재개한다. 비교 폴더는 덮어쓰지 않는다.
`policy_freeze.json`과 `experiment_freeze.json`은 사후에 새 결과에 맞게 다시 쓰지 않는다.
검증 파일은 `tests/test_relevance_two_stage_policy.py`, `test_relevance_two_stage_runtime.py`,
`test_prepare_relevance_two_stage.py`, `test_relevance_human_review.py`다.

## 사람이 100건 검수하는 방법

한국어 번역을 함께 보려면
`artifacts/relevance_two_stage_data_20260917_v1/human_review_ko.html`을 로컬 브라우저에서 연다.
영문 기사는 한국어 읽기 보조와 영문 원문을 함께 표시한다. 국내 기사는 원문을 표시한다.
AI 번역이며, 근거 인용은 번역문이 아닌 원문에서 복사한다.
기존 `human_review.html`과 원문 입력 파일은 보존한다. 기존 창에 작성 중인 내용이 있으면
먼저 **검수 파일 내보내기**를 누르고 새 화면에서 그 파일을 불러온다. 원문 해시와
검수 파일 형식은 같아서 이전 작업을 이어갈 수 있다. 기존 탭을 자동 새로고침하지 않는다.

서버가 필요 없는 단일 파일이며 네트워크 호출과 브라우저 저장소 사용이 없다.
화면이 좁으면 기사 아래에 판정 양식이 나오고, 넓으면 기사와 양식이 나란히 나온다.

1. 대상 기업·발행 시각과 제목·본문 전체를 읽는다. 모델 예측은 화면에 없다.
2. 채택/제외/보류, 관련성, 신규/과거/불명, 이유, 원문 인용, 검수자 이름/식별자를 입력한다.
3. **직접 검수 완료**를 누른다. 채택·제외는 원문에 실제 있는 근거가 필요하다.
4. 작업 중 **검수 파일 내보내기**로 JSONL을 저장한다. 창을 닫으면 메모리 편집은 사라진다.
5. 다음 작업 시 **이전 검수 파일 불러오기**로 이어간다. 다른 기사·변경된 입력·잘못된 인용은 거부한다.

생성 또는 테스트만으로 검수 완료 표시를 만들지 않는다. 완료 필드는 실제 검수자가 직접
확정한 자기 신고이며, 코드의 형식 검증 자체가 판단의 정확성을 인증하는 것은 아니다.
미작성 기사·보류를 관련 없는 뉴스라는 학습 정답으로 바꾸지 않는다.

```powershell
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/build_relevance_human_review.py --inputs "$data/human_inputs.jsonl" --validate '검수자가 내보낸 파일의 경로.jsonl'
```

형식·원문·입력 해시를 확인하는 명령이며 서비스나 GNN 데이터를 수정하지 않는다.
실제 사람이 완료한 검수 파일을 확보한 뒤 별도로 평가해야 한다. 현재까지의 모든 모델
출력은 학습 후보이고 GNN 라벨이 아니다.

## 한국어 읽기 보조 재생성

`build_relevance_human_review.py --translations`는 원문 입력과 별도로 저장한 번역 JSONL을
받는다. 기사 ID·기업 코드·원문 해시가 같아야 하며, 중복·다른 기사·변경된 원문은 거부한다.
번역 파일을 입력 해시에 섞거나 GNN/관련성 선별 입력으로 대체하지 않는다.
번역은 사람 검수 정답을 만들지 않으며, 기존 관련성 모델 판정도 화면에 노출하지 않는다.

```powershell
$translations = 'AI/graph/artifacts/relevance_review_translation_20260917_v2/translations.jsonl'
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/build_relevance_human_review.py --inputs "$data/human_inputs.jsonl" --translations "$translations" --out "$data/human_review_ko_new.html"
```

출력 파일이 이미 있으면 덮어쓰지 않는다. 번역 출처와 원문 해시는
`artifacts/relevance_review_translation_20260917_v2/source_manifest.json`에 기록한다.
이번 번역은 Codex가 작성한 AI 번역이다. 로컬 Qwen의 최초 번역 파일럿에서는
국가명·개장 전/마감 전·예상 손실/과거 손실의 의미 오류가 발견돼 중단했고,
그 출력은 검수 화면에 사용하지 않았다. `translate_relevance_review.py`는 실험용으로,
생성 종료(EOS)·원문 처리 범위 검사만으로 번역 의미의 정확성을 보장하지 않는다.
