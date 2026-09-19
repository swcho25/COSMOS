# 최근 90일 정식 HDFS 배치

현재 상태: **2026-09-16 16:11 KST HDFS 게시·최종 검증 완료**.
기존 GNN 학습/번들 업로드와 별도 작업이다.
사용자가 7개 빈 경로를 채우도록 요청했고 최근 90일 우선 처리를 선택했다.
기준 시각은 `2026-09-16T00:00:00Z`, 창은 UTC `[as_of-N일, as_of)`의 7D/30D/90D다.
새 모델 학습, PostgreSQL 적재, 서비스 연결, commit/push/MR는 범위에 없다.

## 이번 실행의 집계 결과

실행 ID: `lake-recent90-20260916-v1`. 전역 `_VERIFIED` 존재와 receipt를 재확인했다.
스냅샷 ID: `ab19d794-ad08-51b3-8a91-a060bcdfdafc`.

| 산출물 | 행 수 |
|---|---:|
| 정제·중복 제거 뉴스 | 126,310 |
| 기사×기업 언급 | 97,898 |
| 뉴스 관계 근거 | 611 |
| 공시×기업 언급 | 3,565 |
| 공시 관계 근거 | 48 |
| 기업 특징 / 기업 집계 | 각각 606 |
| 관계 특징 / 관계 점수 | 각각 373 |
| 그래프 노드 / 기간별 엣지 | 202 / 373 |
| 문서 ID 매핑 identity 행 | 393,501 |

그래프 373행은 3개 기간을 합한 수이며 고유 관계 373개라는 뜻이 아니다.
감성 대상인 기업 언급 기사 46,976건 중 언어가 확인된 12,679건(한국어 9,984, 영어 2,695)을
기존 FinBERT로 추론했다. 언어 미제공 34,297건은 NULL/status로 남겼다. 추론 시간 82.859초.

| 기간 | 기업 수 | 관계·기간 행 | 뉴스 근거 존재 | 공시 근거 존재 | 두 출처 모두 |
|---|---:|---:|---:|---:|---:|
| 7D | 202 | 65 | 65 | 0 | 0 |
| 30D | 202 | 127 | 125 | 2 | 0 |
| 90D | 202 | 181 | 137 | 44 | 0 |

**이번 관측 데이터에는 동일 관계·기간에 두 출처가 함께 존재하는 행이 없다.** 따라서
합의된 한쪽 NULL 정책을 적용하면 이번 배치에서는 뉴스/공시 가중치 슬라이더가 점수를
바꾸지 않는다. 개인화 반응을 보이려고 없는 근거를 만들거나 NULL을 0으로 바꾸지 않았다.
뉴스 관계는 한국어 규칙, 공시 관계는 인식된 DART 최대주주 표라는 현재 추출 범위의 결과다.

가격은 최신 완료 파일의 14,879행을 선택했다. 각 기간마다 100개 기업은 수정종가가 없어
수익률/변동성이 NULL, 100개는 계산 가능, WDAY/XEL 2개는 최신 선택 이력에 관측이 없다.
서로 다른 수정주가 기준이 섞일 수 있어 옛 파일의 가격으로 틈을 채우지 않았다.

## 기존 자료 확인

- `/datasets/news`: 24.7 GiB. prepared snapshot과 별도 live mirror가 존재한다.
- `/datasets/opendart`: 10.3 GiB. 기존 import, snapshot, gapfill, daily 자료가 있다.
- `/datasets/prices`: 11.2 MiB. 주가 Parquet snapshot이 있다. 최신 raw daily는 별도 경로다.
- `/datasets/sec-edgar`: 66.2 GiB. 해외 공시 본문 및 목록이 있다.
- `/data-lake/sandbox/news/junwoo/company-mentions`: 1.8 GiB, 여러 버전 포함.
  dict-v1.3 완료 manifest의 입력 합계는 9,878,282행,
  기업 포함 기사 합계 1,498,312건, 기사×기업 출력 2,656,806행이다.
- `/data-lake/sandbox/news/junwoo/models`: 기존 모델 전달본 49파일, 5,139,775,140바이트.

용량은 HDFS 복제본을 제외한다. 파일 수나 원천 행 수를 중복 없는 기사 수로 해석하지 않는다.
원천이 비어 있던 것이 아니라 정식 정제/특징/집계/스냅샷 producer가 없던 상태다.
기존 사전·본문 정제·관계 분류·기간별 점수 공식을 재사용한다. 정제 후 위치가 바뀌므로
옛 원문 기준 mention span을 새 본문에 그대로 붙이지 않는다.

## 입력 고정 및 추출

원격 작업 디렉터리: `/home/ubuntu/ner-work/`.
로컬 감사 기록: `artifacts/lake_recent90_20260916/`.

1. `spark/audit_lake_inputs.py`: HDFS 파일 경로·길이·수정 시각·스키마·기간별 건수 점검.
   최초 감사 보고서 날짜 직렬화 오류는 v2에서 수정, 원본 자료 변경 없음.
2. `spark/export_lake_recent_inputs.py`: 준비본·live·종료한 실시간 배치·가격·공시를 읽어
   서버 로컬 새 디렉터리에 JSONL로 추출한다. 검증용 뉴스 fixture는 제외한다.
   UTC 전환 전 날짜 사전 필터에는 양쪽 하루 여유를 두고 정확한 창은 이후 적용한다.
3. 현재 완료 입력은 `lake_recent90_20260916_inputs_v2`.
   뉴스 prepared 64,660행, realtime 19,715행, live 59,400행,
   주가 51,780행, DART 3,245행, SEC 383행. **중복 제거·정확한 창 필터 전 행 수**다.
   원천 파일 길이/수정시각, 완료 표식, DART ready의 manifest/checksum 파일 SHA를 점검한다.
   HDFS 원천 데이터 전체 SHA를 새로 계산했다고 주장하지 않는다.

## 새 산출물의 의미

- `lake_news_normalize.py`: UTC 정규화, 주가 위젯 등 본문 정제, 실제 본문 기준 기업·관계 근거.
- `lake_document_ids.py`: URL/충분한 본문 해시로 중복 집합을 만들고 문서 UUID와 id-map 생성.
  이후 실행은 기존 id-map을 반드시 전달한다. 90일 slice로 전역 중복 인증을 주장하지 않는다.
- `lake_disclosures.py`: DART 접수일·원문 최대주주 표에 근거한 INVEST. SEC는 기업 공시 언급.
  DART는 가장 완전한 사본의 문서와 근거를 함께 선택한다. SEC는 정확한 acceptance 우선.
- `prepare_lake_recent.py`: 재수신 기사 병합, 기업 재매칭, 근거·출처·가격 이력 준비.
  시장별 최신 완료 가격 파일 하나만 사용하여 서로 다른 수정주가 기준을 섞지 않는다.
- `lake_sentiment.py`: 기존 한국어/영어 FinBERT의 제목·첫 200자 감성. GNN 주가 방향이 아니다.
  언어 미제공/미지원은 NULL로 남긴다. region으로 언어를 단정하지 않는다.
- `lake_window_features.py`: 7/30/90일 회사·관계 특징과 집계. 뉴스/공시 각각 동일한 근거 지지
  공식 `100*(1-exp(-문서별 최대 confidence 합/5))`, 둘 다 있으면 50:50,
  하나만 있으면 해당 값, 모두 없으면 관계 행 없음. 가격을 최종 점수에 섞지 않는다.
- `finalize_lake_recent.py`: 감성 연결·자연키/문서 참조 검증·독립 점수/기사 수 교차 확인.
- `spark/publish_lake_recent.py`: 명시 스키마 JSONL→Snappy Parquet, 재읽기 전체 내용 대조,
  건수/키/스키마/파일 체크섬 검증 후 신규 run으로 게시한다.

## 게시 계약

아래 경로에 `run_id=lake-recent90-20260916-v1/data` 게시를 완료했다.

- `/data-lake/cleaned/news`
- `/data-lake/analyzed/news/relationships`
- `/data-lake/features/company`
- `/data-lake/features/relationship`
- `/data-lake/aggregated/company-metrics`
- `/data-lake/aggregated/relationship-scores`

그래프는 `/data-lake/snapshots/graph/run_id=lake-recent90-20260916-v1/nodes`와 `edges`다.
문서 ID map, 기업 언급, 감성, 공시 근거도 출처 추적을 위해 별도 run으로 함께 보관한다.
모든 그룹이 검증된 뒤 `/data-lake/metadata/runs/run_id=<실행 ID>/_VERIFIED`를 마지막으로
만든다. 이 표식 없는 부분 실행을 완성본으로 읽지 않는다. 기존 폴더/파일은 덮어쓰지 않는다.

이번 완료 표식과 receipt:

```text
/data-lake/metadata/runs/run_id=lake-recent90-20260916-v1/_VERIFIED
/data-lake/metadata/runs/run_id=lake-recent90-20260916-v1/receipt.json
```

예시(프로젝트 서버에서):

```bash
/opt/hadoop/bin/hdfs dfs -cat /data-lake/metadata/runs/run_id=lake-recent90-20260916-v1/_VERIFIED
/opt/hadoop/bin/hdfs dfs -ls /data-lake/aggregated/relationship-scores/run_id=lake-recent90-20260916-v1/data
```

Spark 예시: `spark.read.parquet("hdfs:///data-lake/aggregated/relationship-scores/run_id=lake-recent90-20260916-v1/data")`.
`window_type`으로 7D/30D/90D를 선택한다. Parquet를 텍스트 `cat`으로 읽지 않는다.

회사 키는 `market + stock_code`, 관계 키는 양쪽 회사 자연키와 관계 유형이다.
기존 PostgreSQL UUID는 이후 Loader가 조회하여 연결해야 한다. HDFS 게시 상태는
`HDFS_VERIFIED_ONLY`; 운영 DB에 `PUBLISHED`로 반영했다는 의미가 아니다.

## 백엔드 전달 시 확인할 계약

이번 게시가 끝나도 기존 Loader를 그대로 실행하면 서비스 연결이 완료되는 것은 아니다.
현재 저장소의 Loader는 HDFS Parquet가 아닌 CSV를 입력받는다. 후속 연결에서는 다음을
맞춰야 하며, 이번 HDFS 작업에서 DB/Loader/서비스를 변경하지 않는다.

1. 전역 `metadata/runs/run_id=<id>/_VERIFIED`를 확인하고 그 run의 경로만 읽는다.
   부모 경로 전체를 읽으면 다른 run을 중복 집계할 수 있다.
2. `(market, stock_code)`를 DB `company_id`에, 유형 코드를 `relationship_type_id`에,
   방향을 유지한 기업 쌍·유형을 `relationship_id`에 매핑한다. 종목 코드의 앞자리 0을
   보존하고 매핑 누락을 실패 처리한다. `relationship_key`는 DB UUID가 아니다.
3. `news_score`/`disclosure_score`의 NULL을 보존한다. 현 component SQL은 이 계약을
   지원하지만 current/history의 nullable numeric 컬럼이 필요하다. 이 PC의 Flyway
   V1~V3에는 아직 해당 컬럼이 없으며 팀원의 미반영 변경 여부는 별도 확인해야 한다.
   `impact_direction`의 NULL을 감성으로 채우지 않는다.
4. 기업 감성 미분석은 positive/negative/neutral count가 NULL이며 coverage를 함께
   제공한다. 현 BE는 positive/negative를 NOT NULL 및 Java `int`로 정의하므로 바로
   호환되지 않는다. NULL을 0으로 바꾸지 말고 미분석을 표현할 DB/API 계약을 정한다.
5. HDFS `snapshot_id`는 run ID 기반 UUIDv5다. 기존 SQL은 DB UUID를 생성하므로
   ID 보존 또는 대응표가 필요하다. 기존 SQL의 current 교체는 전체 범위이므로
   일부 데이터만 변환하면 관계가 누락될 수 있다. 한 run의 전체 7D/30D/90D를
   검증한 뒤 후속 DB 적재에서 처리한다. HDFS 완료를 DB 공개로 간주하지 않는다.

참조: `load_relationship_components.sql`, `finalize_lake_recent.py`,
`BackEnd/.../CompanyMetricHistory.java`, `BackEnd/.../GraphQueryRepository.java`.
이번 배치의 기사 감성은 뉴스 입력 GNN의 기업별 주가 방향 예측과 별개다.

## 명시적 한계

- 확보된 원천 범위의 집계이며 해당 기간에 발행된 모든 기사/공시의 완전 수집을 보증하지 않는다.
- 공개 시각과 수집 시각은 다르다. 이번 작업은 과거 시점 백테스트가 아니다.
- 관계 규칙은 한국어만 지원한다. 영어 기업 언급은 제공하지만 영어 관계 추출 완성을 주장하지 않는다.
- 공시 관계는 인식한 DART 최대주주 표만 지원한다. SEC 기업 간 관계는 미지원이다.
- 감성은 기사 제목/앞부분의 감성을 언급 기업에 공유한 지표다. 기업별 주가 예측으로 읽지 않는다.
- 수정종가가 없으면 수익률/변동성/상관은 NULL. 교차시장 상관은 달력 정렬 전까지 NULL이다.
- 이번은 고정 시각 배치 1회이며 지속 갱신 스케줄러는 별도다.

## 실행 승인과 진행 기록

코드·기업/별칭 사전 업로드는 자동 승인 검토에서 추출 스크립트보다 범위가 넓다는 이유로
첫 시도가 거절되어 전송되지 않았다. 이후 사용자가 코드 업로드·실행 및 감성용 제목/첫200자
PC 전송·결과 반환을 명시 승인했다. 승인 후 준비 코드 v1을 전송·실행했다.
첫 준비 실행에서 139,626 원천 행을 정제·중복 제거하여 126,310개 대표 기사를 생성했다.
직렬 v1은 중단·보존하고 같은 결과 순서를 보장하는 2-worker v2를 완료했다.
그 뒤 PC 감성 추론, 서버 집계, HDFS 게시를 완료했다. 실제 사용한 경로는 다음과 같다.

- 코드: `/home/ubuntu/ner-work/lake_recent90_20260916_code_v2`
- 입력: `/home/ubuntu/ner-work/lake_recent90_20260916_inputs_v2`
- 준비본: `/home/ubuntu/ner-work/lake_recent90_20260916_prepared_v2`
- 집계본: `/home/ubuntu/ner-work/lake_recent90_20260916_final_v1`
- 게시 기록: `/home/ubuntu/ner-work/lake_recent90_20260916_publish_v1`

검증: 로컬 회귀 73개 PASS, 공시 관계 48건 원문 offset 일치, 뉴스 근거 offset·참조 무결성,
점수 공식과 기업별 기사 수의 독립 재계산 PASS. 13종 JSONL→Parquet를 전체 행 기준 양방향
`exceptAll`로 대조하고 건수·키·스키마·HDFS 체크섬을 기록했다. 승격 후 최종 경로의
29파일 목록·길이와 receipt를 다시 확인했다. 실제 Parquet 16파일은 모두 0바이트가 아니며
총 373,796,008바이트(복제본 제외)다. 0바이트 `_SUCCESS` 13개는 정상 완료 표식이다.

로컬 `artifacts/lake_recent90_20260916/`의 `hdfs_receipt.json`, `final_hdfs_audit.json`,
`result_summary.json`, `publication.json`, `prepared_manifest.json`에 검증 기록이 있다.
HDFS receipt SHA256: `990fa53a7a2a8fbe65a034b9eaf826a9d7e9870d3404598ce2bded69744f687f`.
이 작업에서 운영 DB 적재, 서버 API 실행, Spring/프론트 연결, Git commit/push/MR는 하지 않았다.
