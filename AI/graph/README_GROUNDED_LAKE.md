# 새 선별 결과의 HDFS 적재

이 작업은 `kg_grounded_full_20260918_v1`의 뉴스·기업 연결·기업별 문맥 감성과
`kg_integrated_20260919_v3`의 검토된 뉴스·DART 관계를 새 HDFS 실행 버전으로 저장한다.
기존 `lake-recent90-20260916-v1`의 관계·감성·점수를 새 계산에 섞지 않는다.
기존 뉴스 ID 대응표는 동일 기사의 UUID를 유지하기 위해 재사용한다.

**2026-09-19 17:18 KST HDFS 적재·최종 독립 검증 완료 (`HDFS_VERIFIED_ONLY`).**
16종 데이터셋을 135개 Parquet 파일로 저장했다. 총 2,221,706,265바이트이며 빈 데이터 파일은 없다.
전체 행 양방향 대조, 키·건수·스키마, 최종 경로의 135개 체크섬과 전역 완료 표식을 확인했다.
[게시 영수증](artifacts/lake_grounded_20260919_v1/hdfs_verified/receipt.json),
[최종 감사 결과](artifacts/lake_grounded_20260919_v1/hdfs_verified/final_hdfs_audit.json).
기존 90일본은 보존했고, 서비스 DB Loader는 실행하지 않았다.

## 범위와 시간

- 실행 ID: `lake-grounded-20260919-v1`.
- 집계 기준: 동결 입력과 동일한 `2026-09-16T00:00:00Z`.
- 뉴스 보관 범위는 선택된 과거 기사 전체이며 기업·관계 지표는 UTC `[기준-N일, 기준)`이다.
- 시차 미확인 날짜는 실제 UTC 발행 시각을 만들어내지 않는다. `published_at=NULL`,
  원래 문자열은 `published_at_raw`, 집계용 보수적 가용 시점은 해당 날짜 다음날12:00UTC다.
  원천 시간 감사가 무효로 판정한 날짜는 집계에서 제외하고 원본 상태를 보존한다.
- 단일 SHA 검증 주가 스냅샷의 수정종가로 계산한다. 가격 최신일은 KOSPI9/11·NASDAQ9/10이며
  기업 특징의 `price_calendar_days_since_last`가 오래된 정도를 표시한다.
- 실시간 갱신·GNN 재추론·재학습·모델 교체·서비스 DB 반영은 이 적재에 포함되지 않는다.

## 저장 경로

각 경로에 `/run_id=lake-grounded-20260919-v1/data`가 붙는다.

| 경로 | 데이터 |
|---|---|
| `/data-lake/cleaned/news` | 기존 UUID를 유지하며 중복을 합친 정제 뉴스 대표본 |
| `/data-lake/analyzed/news/company-mentions` | 원본 뉴스–기업 연결, 관련성 근거, 감성, 출처 버전 |
| `/data-lake/analyzed/news/relationships` | 검토를 통과한 뉴스 관계 근거 |
| `/data-lake/features/company` | 기업별 뉴스·공시·수정주가 특징 |
| `/data-lake/features/relationship` | 기간별 관계 지지 근거와 가격 상관 특징 |
| `/data-lake/aggregated/company-metrics` | 기업별7D·30D·90D지표 |
| `/data-lake/aggregated/relationship-scores` | 기간별 뉴스·공시 구성 점수와 기본 점수 |
| `/data-lake/analyzed/news/sentiment` | 기업별 문맥 감성 결과·미분석 상태·모델 출처 |
| `/data-lake/analyzed/disclosures/company-mentions` | DART·SEC 보고 기업 관측 |
| `/data-lake/analyzed/disclosures/relationships` | DART 관계와 정확한 원문 근거 |
| `/data-lake/metadata/id-maps/news` | 기존 키를 보존한 전체 뉴스 ID 대응표 |

그래프는 `/data-lake/snapshots/graph/run_id=lake-grounded-20260919-v1/` 아래 다음으로 나눈다.

- `nodes`: 기업 노드.
- `edges`: 7D·30D·90D 안에 실제 근거가 있는 관계와 점수.
- `historical_relationships`: 과거 근거에서 확인한 유형별 관계. 현재 계약의 유효성을 뜻하지 않는다.
- `evidence`: 뉴스·공시의 관계 근거, 방향, 인용 위치와 출처 버전 ID.
- `documents`: 인용이 참조하는 원문 버전. 정제 대표본으로 인용 위치를 바꾸지 않는다.

## ID와 의미

이번 HDFS 적재본의 검증된 건수는 다음과 같다.

| 항목 | 건수 | 구분 |
|---|---:|---|
| 정제 뉴스 | 219,662 | 원문 219,794개에서 중복 132개 통합 |
| 뉴스–기업 연결 | 246,264 | 원행 보존; 집계용 중복 제거 후 246,117쌍 |
| 기업별 감성 결과 | 246,264 | 분석 174,680행, 보류 71,584행 |
| 기업 특징 / 기업 집계 | 각각 606 | 202개 기업 × 7D·30D·90D |
| 관계 특징 / 관계 점수 | 각각 67 | 기간별 7D 1행·30D 9행·90D 57행 |
| 과거 기업 관계 | 119 | 공급 20·투자 54·협력 38·경쟁 7 |
| 관계 근거 | 541 | 뉴스 177·DART 364 |
| 공시 기업 관측 | 800 | 보고 기업 연결; 모두 기업 간 관계라는 뜻은 아님 |
| 원문 버전 | 220,048 | 뉴스 219,794·DART 254 |
| 전체 뉴스 ID 대응 키 | 1,045,234 | 이전 키 보존; 기사 수와 다름 |

과거 관계 119개와 기간별 관계 67행은 범위가 다르다. 67행에는 같은 관계가 여러 기간에
등장할 수 있다. 이번 기준 시점에는 같은 관계·기간에 뉴스와 공시 점수가 모두 존재하는
경우가 없어, 한쪽 구성 점수만으로 표시되는 관계는 뉴스/공시 가중치를 바꿔도 점수가 같다.

`document_id`는 중복 집계 방지용 문서 ID, `source_revision_id`는 인용의 정확한 원문 버전이다.
뉴스–기업 원행은 `edge_id`로 모두 보존하고 기업 지표는 `(document_id,ticker)`로 합친다.
중복 원행의 시간·감성 결과가 충돌하면 감성은 `NULL`로 보류하고 가용 시점을 보수적으로 처리한다.
원본 결과를 삭제하거나 다른 버전의 인용을 대표본에 붙이지 않는다.

원문 내 `title`/`body` 위치는 원래 기업 연결 JSON의 필드별 위치이고,
기업 관계의 위치는 `original_title_newline_body` 또는 공시 원문 문자 위치다.
`source_edge_json`, `sentiment_json`, `source_evidence_json`에는 동결된 상세 출처를 보존한다.

뉴스 관련성은 후보 판정이고, 감성은 기업 주변 금융 문맥의 긍정·부정·중립이다.
둘 모두 주가 상승·하락을 보증하지 않는다. 미분석을 중립으로 바꾸지 않는다.
관계 점수는 문서별 지지 근거량이며 확률·GNN 영향도가 아니다.
한쪽 구성 점수가 없으면 `NULL`, 한쪽만 있으면 그 점수, 양쪽이면50:50이다.
근거 문서 수와 독립 사건 수는 다르다.

## 검증과 재개

새 코드:

- `grounded_lake_identity.py`: 과거 ID 보존, 원문 버전과 중복 집합 검증.
- `grounded_lake_features.py`: 기간·감성·주가 특징과 과거/기간 관계 분리.
- `prepare_grounded_lake.py`: 동결 입력 SHA, 모든 원문 위치·참조·건수 확인 및 JSONL 분할 출력.
- `spark/publish_grounded_lake.py`: 정확한 필드·타입·NULL·SHA 검사 후 재개 가능한 게시기 실행.

JSONL과 Parquet 전체 행을 양방향 비교하고 키 중복·스키마·건수·HDFS 체크섬을 확인한다.
모든 데이터셋 검증 뒤 전역 `/data-lake/metadata/runs/run_id=lake-grounded-20260919-v1/_VERIFIED`
를 마지막에 만든다. 이 표식이 없는 run은 완성본으로 읽지 않는다.
`receipt.json`과 원래 입력·게시 manifest·검증 보고서를 같은 메타데이터 디렉터리에 보관한다.

게시 중단 시 동일 코드·동일 입력으로 같은 명령을 재실행한다. 체크포인트와 파일 체크섬을
검증해 완료된 분할을 재사용한다. 로컬 준비가 실패하면 부분 결과를 남기고 새 준비 폴더를 쓴다.
기존 게시 경로에 덮어쓰지 않는다.

## 소비자 주의

HDFS 상위 경로의 여러 run을 통째로 합쳐 읽으면 중복과 구판 결과가 섞인다.
반드시 완료 표식을 확인한 **하나의 run 경로**를 지정한다. 이 run은 확보된 후보의 부분집합이며
원천 전체 뉴스의 처리 완료를 뜻하지 않는다. 이전 데이터와 건수를 비교할 때 선별 기준과 기간을 구분한다.

`HDFS_VERIFIED_ONLY`는 서비스 공개 상태가 아니다. DB Loader의 자연키→DB UUID 매핑,
현재/과거 테이블 갱신 및 기존 연결 정리는 별도의 서비스 반영 작업이다.

기존 `lake-recent90-20260916-v1`은 보존한다. 소비자가 새 run을 명시적으로 선택하고
서비스 결과까지 확인하기 전에는 기존본을 삭제해도 화면이나 DB의 이전 값이 정리되지 않는다.
이전본 보존과 새 실행 버전 선택은 별개의 작업이다.
