# 과거 기업 정보와 관련성 선별 v3

사용자 요청의 두 작업은 과거 사명·분할·사업 관계의 근거를 보강하고, 뉴스 선별을 개선해
새 표본에서 검증하는 것이다. 결과와 적용 여부는 `RELEVANCE_IDENTITY_V3_RESULTS.md`에 기록한다.

## 기사 시점의 기업 정보

`historical_company_context.py`는 `data/historical_company_context_v1.json`의 공개 자료를
기사 발행일로 조회한다. 이번 사전은 8개 발행인, 9개 종목 경로의 44개 관측 기록이다.
GOOG와 GOOGL은 같은 Alphabet 발행인의 두 주식 종류다. 전체 202개 종목을 완성한 사전이 아니다.

- 국내: 대우조선해양, 현대건설기계, 분할 후 현대중공업, 신한금융지주.
- 해외: Alphabet, Facebook/Meta, 합병 후 Marvell, Starbucks.
- 하나금융의 색인만 확인된 2건, 현재 CIK와 다른 합병 전 Marvell 자료 3건은 입력에서 제외했다.
- Google 사업/자회사, Facebook 제품, 한국 Starbucks 운영사를 각각 모회사와 같은 법인명으로 등록하지 않는다.
- DART 코드의 과거 공식 대응은 미확인이다. 국내 식별 키는 KRX 공시에서 확인한 종목 대응이다.

관측일과 공개일을 따로 저장한다. 공개 시각을 모르는 자료는 공개 다음 날 UTC 00:00부터
사용한다. 기사 이후 공개자료를 소급하지 않으며, 최신 공개 자료가 충돌하거나 관측 후 400일이
지났으면 예전 자료로 되돌아가지 않는다. 400일은 실험의 노후 자료 차단 기준이지 법적 유효기간이 아니다.
자료를 추가해도 관측일부터 기사일까지 소유관계가 계속 유지되었음을 증명하지는 않는다.

원문 제목·본문·발행일·종목·기사 ID는 그대로 두고 `company_name`, `company_context`만 바꾼다.
변환 시 원본/결과/연구자료 SHA와 이용한 기록을 보존한다. 미지원 종목·시점은 문맥을 만들지 않는다.
상세 출처와 보류한 자료는 `artifacts/relevance_identity_v3_20260917_v1/`의 두 research JSON,
`build_registry.py`, `registry_receipt.json`, `company_coverage.json`에서 확인한다.
웹 원문 HTML 바이트는 보존하지 못했으며 그 파일의 SHA를 검증했다고 주장하지 않는다.

## 선별 후보 v3

`relevance_policy_v3.py`는 현재 경제 사건과 대상 기업의 구체 연결을 우선 확인한다.
약력·과거 투자자 목록·회고·추천·일정에만 나온 이름은 제외, 실제 사건의 귀속이 불명확하면
보류하도록 지시한다. 이것은 프롬프트 규칙이며 의미 판단을 보장하는 코드 필터가 아니다.

원문 전체를 `[id, source, text]`로 보내 중복 위치 메타데이터만 줄인다. 인용문은 원본 ID로
복원하고 검사한다. 출력에 불필요한 JSON 키가 붙으면 원문을 보존하고 그 키를 감사 기록에
남긴 뒤 `decision`, `evidence_ids`만 검사한다. 잘못된 ID·중복 키·잘린 JSON은 보류한다.
생성된 요약이나 근거 목록을 원문 근거로 대신 쓰지 않는다.

v3는 greedy 생성으로 바꾸었으므로 v1과의 비교는 **프롬프트·입력 형식·파싱·생성 설정의
복합 변경 효과**다. 같은 Qwen의 2단계 판단은 독립된 두 전문가가 아니다. 2차 입력에는
1차 출력이 없으며, 본문 잘림·재시도 없이 입력 6144/출력 768 토큰 제한을 유지한다.

## 평가와 실행

정책과 평가계획을 새 기사 추출 전에 고정했다. 연구 사전과 변환기·추출기도 문맥 표본 전에 고정한다.
일반 32건은 국내16/해외16이다. 문맥 적용 가능 표본은 처음 지역별8건을 계획했으나
고정 해시 순서·종목당 최대2건 제한에서 채우지 못해, 입력 열람 전에 수량만 줄이는 변경을
기록했다. 최종 별도10건은 국내5/해외5·종목당 최대2건이다. 국내/해외는 뉴스 출처 기준이며
국내/해외 상장기업 수를 뜻하지 않는다. 문맥 지원 후보 자체는14,148건으로, 10건은 이 고정
균형 추출 규칙의 결과이지 사용 가능한 기사 총수가 아니다.
둘 다 2020–2022 train, 정제 본문200–6000자, 이전 검토612와 val/test11,969 및 표본 간
ID·본문·URL 중복을 배제한다. 기업 정보가 있는 표본은 무작위 모집단 대표 표본이 아니다.

일반 비교는 v1 대 v3, 둘 다 문맥 없음이다. 문맥 비교는 같은 v3의 정보 보강 전후이며,
참고판정은 정보가 보강된 전체 입력을 읽은 별도 AI의 판단이다. 미보강 모델에도 그 참고판정을
같이 사용하므로 입력 정보 차이를 포함한 비교다. 사람 정확도·실제 주가 영향·GNN 성능과 다르다.
길이 초과와 형식 오류도 전체 분모에 남긴다. 참고판정과 출력의 SHA·전체 행 수를 검증한다.

```powershell
$python = 'AI/ner/.venv/Scripts/python.exe'
$work = 'AI/graph/artifacts/relevance_identity_v3_20260917_v1'
& $python -X utf8 AI/graph/historical_company_context.py --inputs inputs.jsonl --registry AI/graph/data/historical_company_context_v1.json --out new_context_output --companies AI/ner/data/companies.csv
& $python -X utf8 AI/graph/run_relevance_policy_v3.py --inputs new_context_output/context_inputs.jsonl --out new_model_output
```

새 출력 폴더를 사용한다. 기존 결과는 덮어쓰지 않는다. 모델은 기존 로컬 Qwen 가중치와
NF4·attention adapter를 사용하며 별도 학습은 없다. 이번 요청에는 전체 뉴스 선별·GNN
재학습·서비스 연결·HDFS 변경·커밋·푸시·MR이 포함되지 않는다.
