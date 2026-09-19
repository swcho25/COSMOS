# 근거 검증 뉴스·관계 데이터의 서비스 버전 전달

**문서 상태: 2026-09-19 운영 DB 적재·분석 버전 게시·BE/FE 배포와 실제 API·화면 확인 완료.**
배포 영수증의 완료 시각은 **19:58:12 KST**, 앱 태그는 `grounded-20260919-v1`이다.
운영 18081 포트의 API 153건이 통과했으며, 67개 관계/기간·원문 인용·감성·NULL·버전 격리를 확인했다.
PostgreSQL/Redis 컨테이너와 기존 사용자 기록을 보존했다. 임시 검증 컨테이너는 제거했고 복제 DB는 보존했다.

## 전달 범위와 접근

- 서비스 서버: `ubuntu@j15c205.p.ssafy.io`. 승인된 개인키는 저장소 밖에서 관리한다.
- 서버 작업 폴더: `/home/ubuntu/cosmos-grounded-service-20260919-v1`.
- PostgreSQL 컨테이너: `cosmos-postgres-1`; 운영 DB `cosmos`, 격리 검증 DB `cosmos_grounded_review_20260919`.
- Loader는 서버의 Docker 권한으로 컨테이너 로컬 소켓에 접속한다. DB 비밀번호나 SSH 개인키를 코드·패치·로그에 넣지 않는다.
- HDFS 원본 버전: `lake-grounded-20260919-v1`; 스냅샷 UUID `a70eb366-22cd-59de-88a3-fc7bceac09ca`.
- 이번 배치는 **2026-09-16 00:00 UTC 기준**이다. 확인 가능한 뉴스의 최신 발행시각은 **2026-09-07 23:21:28 UTC = 2026-09-08 08:21:28 KST**다. 따라서 한국 시간 화면에는 9월 8일로 보인다. 9월 19일 실시간 뉴스 묶음이 아니다.

## 버전 선택과 사용자 기록

`cosmos_analysis`는 뉴스·기업 연결·감성·관계·근거·점수·기업 집계를 `run_id`별로 저장한다.
조회용 `current_*` 뷰는 `active_run`이 선택한 한 버전만 읽는다. 따라서 점수만 새 버전이고 근거는 옛 버전인 상태로 섞이지 않는다.

| 버전 | 의미 |
|---|---|
| `legacy-service-before-grounded-20260919-v1` | 전환 직전 공개 조회 데이터를 보존한 버전 |
| `lake-grounded-20260919-v1` | 이번 근거 검증 배치의 분석 결과 |

활성 버전 변경은 관리자가 검증 후 트랜잭션으로 수행한다. 사용자가 임의로 버전을 고르는 API나 화면은 이번 계약에 포함되지 않는다.
프런트엔드는 `GET /api/analysis-runs/active`로 현재 버전을 확인하고 `runId` 변경 시 분석 조회 캐시를 함께 무효화한다.

계정·스크랩·관심기업과 실시간 수집 테이블은 기존 `public` 영역에 보존한다.
새 분석 문서의 사용자 쓰기 외래키를 위해 필요한 **신규 안정 ID만** 공개 문서·관계 테이블에 추가한다.
기존 공개 문서의 분석값을 일괄 덮어쓰거나 기존 사용자 기록을 삭제하지 않는다.
기존 스크랩 목록의 제목·원문 링크는 유지되지만, 선택한 분석 버전에 없는 기사에 대한 새 분석 결과를 대신 만들어 주지는 않는다.

## 준비된 데이터와 의미

| 데이터 | 건수 | 설명 |
|---|---:|---|
| 뉴스 | 219,662 | 정제·중복 제거 후 canonical 문서 |
| 공개 뉴스–기업 연결 | 246,117 | 기업별 긍정·부정·중립 또는 NULL |
| 공시 | 797 | 공시–기업 관측은 800개; GOOG/GOOGL의 공시 3개는 같은 원문 공유 |
| 기업–기업 관계 | 119 | 공급·투자/지분·협력·경쟁의 과거 근거 관계 |
| 관계 근거 | 541 | 뉴스 177개 근거 + 공시 364개 근거 |
| 문장 근거 | 493,263 | 뉴스–기업 492,722개 + 관계 541개 |
| 기간별 관계 점수 | 67 | 7D 1개 + 30D 9개 + 90D 57개 |
| 기업 지표 | 606 | 기업 202개 × 7D·30D·90D |

**관계 119개는 과거 전체 근거의 관계 수이며, 기본 30D 화면의 관계선은 9개다.** 과거 근거를 현재 계약이나 현재 30일 관계 점수로 바꾸지 않는다.
기업별 감성은 긍정 95,479쌍, 부정 23,621쌍, 중립 55,437쌍, 보류/미판정 NULL 71,580쌍이다.
리비전 사이 감성·확률·이용 가능 시각이 충돌한 71개 그룹도 NULL로 유지했다.

관계 점수는 뉴스·공시 문서 지지량 기반이다. 한 출처의 근거가 없으면 그 구성 점수는 NULL이며, 존재하는 출처 점수를 사용한다.
두 출처가 모두 존재할 때 기본 점수는 50:50이다. 수치의 의미는 실제 주가 변동 확률이 아니다.
이번 67개 관계/기간 행에는 두 출처 점수가 함께 있는 행이 없으므로 뉴스·공시 가중치를 움직여도 해당 행의 점수는 바뀌지 않는다.
원문·인용·ID 일치 검증은 경제적 관련성이나 관계 정확도의 독립적인 사람 검수 완료를 뜻하지 않는다.
원본의 `economic_relevance_validated=false`·`graph_quality_validated=false` 등 품질 범위도 문맥에 보존한다.

## 근거와 GraphRAG 설명

관계 설명은 검증된 그래프에서 관계 유형·방향·기업 쌍·기준 시각에 맞는 뉴스와 공시 근거를 검색하고,
고정된 관계 소개 문장과 **정확한 원문 인용**을 조합한다. 생성형 LLM 재추론은 하지 않았다.
`explanationScope=HISTORICAL`은 이 설명이 과거 관계 근거를 다룬다는 뜻이다.

모든 문장 근거는 원래 기사/공시의 `source_revision_id`, 인용 구간 offset, 인용문 SHA256,
원본 JSONL 파일 경로·파일 SHA256·행 번호·행 SHA256으로 추적한다.
관계 근거에는 인용한 원문 리비전의 제목·URL·텍스트 SHA256 등도 보존한다.
canonical 뉴스의 본문이 다른 리비전이라고 해서 그 본문으로 인용을 바꾸지 않는다.

이 API 계약에서 관계 근거 조회의 `window=7D|30D|90D`는 문서의 보수적인 이용 가능 시각을 기준으로 한다.
선택한 기간의 근거 조회와 전체 과거 근거로 작성한 관계 설명의 범위를 구분해서 보여준다.

## NULL과 날짜를 다루는 규칙

- 기업별 감성은 금융 문맥의 긍정·부정·중립이며 **GNN 주가 방향 예측이 아니다**.
- 이번 데이터 실행에서 GNN 추론 영향도는 계산하지 않았다. `impactScore`, 관련도 확률, 보정되지 않은 모델 신뢰도는 NULL로 전달한다.
- 관계 추출의 지지 가중치 1.0을 “모델 신뢰도 100%”로 표시하지 않는다. 해당 가중치는 근거 기여도와 별도 문맥에 남기고 서비스 `confidence`는 NULL로 둔다.
- 뉴스 196,941건은 정확한 UTC 발행시각이 확인되지 않아 `publishedAt=NULL`이다. 원래 문자열·발행 날짜·정밀도는 별도 보존한다.
- 정렬용 `sort_at`은 선언된 보수적 이용 가능 시각이다. 이것을 실제 발행시각으로 이름만 바꿔 반환하지 않는다.
- 뉴스 원본의 수집시각은 알 수 없다. 버전 테이블의 `first_collected_at`·`last_collected_at`은 NULL이다.
- 공개 안정 ID를 새로 추가할 때 구형 NOT NULL 제약 때문에 기록하는 서비스 수신시각은 행정 기록이다. `document_context.payload.source_collected_at=NULL`과 구분한다.
- 자동 뉴스 갱신 작업은 이번 배치에 포함되지 않는다. 정적 스냅샷 응답의 `nextRefreshAt`은 NULL이다.

## 팀에 전달할 코드 패치

현재 개발 브랜치의 `BackEnd/`·`FrontEnd/`는 덮어쓰지 않았다. 운영 기준 커밋
`d2e482c043968865d4770010bd12f32481725f2a`를 기준으로 격리한 서비스 소스의 변경만 다음 파일에 보존했다.

- [`service_delivery/grounded_20260919.patch`](service_delivery/grounded_20260919.patch)
- [`service_delivery/grounded_20260919.manifest.json`](service_delivery/grounded_20260919.manifest.json)
- [`service_delivery/build_delivery_patch.py`](service_delivery/build_delivery_patch.py)
- [`service_delivery/manage_grounded_release.py`](service_delivery/manage_grounded_release.py): 서버 배포 계획·마이그레이션 확인·BE/FE 교체 보조 도구

패치에는 Flyway V7, `BackEnd/ANALYSIS_RUN_API.md`, BE 조회/DTO/테스트, FE 버전·NULL·원문 표시 변경이 포함된다.
빌드 산출물, node_modules, dist, Gradle 캐시, 프런트 검증 영수증·TypeScript 빌드 캐시, 로그는 제외한다.
베이스 파일들을 별도로 보존한 뒤 격리 Git 디렉터리에서 `git apply --check`와 실제 적용을 수행했고,
적용 후 모든 대상 파일의 바이트가 격리 서비스 소스와 같은지 검사한다.

추가 통합 테스트 수정 후 다시 생성한다. PowerShell에서 저장소 루트 기준:

```powershell
& AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/service_delivery/build_delivery_patch.py
```

**추후 재배포 전 이 패치를 팀의 실제 배포 브랜치에 반영해야 한다.** 격리 소스에서 이미지를 만들었다는 사실만으로
원래 배포 브랜치의 소스가 바뀌지는 않는다. 수정하지 않은 브랜치로 재배포하면 새 버전 조회 기능이 빠질 수 있다.
팀의 별도 검토 체크아웃에서 다음 명령을 먼저 사용하고, 충돌이 나면 기준 커밋 차이를 검토한다.

```bash
git apply --check /path/to/grounded_20260919.patch
git apply /path/to/grounded_20260919.patch
```

## 로컬 데이터 준비 명령

입력 원본을 수정하지 않고 새 출력 폴더만 만든다. 이미 완성된 출력이 있으면 재실행으로 덮어쓰지 않는다.

```powershell
& AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/prepare_grounded_service.py --source AI/graph/artifacts/lake_grounded_20260919_v1/prepared_v1 --verified AI/graph/artifacts/lake_grounded_20260919_v1/hdfs_verified --output AI/graph/artifacts/service_db_grounded_20260919_v1/projected_v1
& AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/build_grounded_service_payload.py --projection AI/graph/artifacts/service_db_grounded_20260919_v1/projected_v1 --references AI/graph/artifacts/service_db_grounded_20260919_v1/review_references.json --output AI/graph/artifacts/service_db_grounded_20260919_v1/payload_v1
```

`review_references.json`은 검증 DB의 공개 기업·기사·공시·관계 ID와 컬럼 계약이다. 사용자 개인정보를 입력으로 사용하지 않는다.
기존 UUID는 뉴스 canonical URL/hash, DART 접수번호 또는 SEC CIK/accession, 관계 유형과 방향을 확인해서 재사용한다.
이번 뉴스 원문은 기존 서비스 기사와 UUID·정규화 URL 교집합이 없어 모두 신규 ID이고, 공시 639개·관계 54개는 기존 ID를 재사용한다.

## 서버 게시와 검증 순서

1. 운영 DB를 백업하고 별도 검증 DB로 복원한다. 기존 백업을 덮어쓰지 않는다.
2. 일치하는 BE의 V7 마이그레이션을 검증 DB에 적용하고, 격리 API/FE 런타임에서 테스트한다.
3. `capture-legacy`로 기존 공개 조회 버전을 보존한다.
4. `stage`로 payload 해시·행 수·외래키·NULL 의미를 검증하며 새 버전을 적재한다.
5. `activate`로 선택 버전을 한 트랜잭션에서 전환한다.
6. 실제 API 결과와 DB를 교차 검사한다. 성공 후 같은 절차의 운영 적용과 게시 영수증을 별도로 남긴다.

다음은 **격리 검증 DB용** 호출 형태다. 해당 서버 작업 폴더에 게시 스크립트와 payload가 있어야 한다.
이미 완료한 불변 단계는 `audit`으로 확인하며 다시 덮어쓰지 않는다.

```bash
TASK_ROOT=/home/ubuntu/cosmos-grounded-service-20260919-v1
sudo -n python3 "$TASK_ROOT/publish_grounded_service.py" capture-legacy --database cosmos_grounded_review_20260919 --log "$TASK_ROOT/review-capture.log"
sudo -n python3 "$TASK_ROOT/publish_grounded_service.py" stage --database cosmos_grounded_review_20260919 --payload "$TASK_ROOT/payload_v1" --log "$TASK_ROOT/review-stage.log"
sudo -n python3 "$TASK_ROOT/publish_grounded_service.py" activate --database cosmos_grounded_review_20260919 --run-id lake-grounded-20260919-v1 --expected-active legacy-service-before-grounded-20260919-v1 --log "$TASK_ROOT/review-activate.log"
sudo -n python3 "$TASK_ROOT/publish_grounded_service.py" audit --database cosmos_grounded_review_20260919 --log "$TASK_ROOT/review-audit.log"
```

되돌리기는 데이터 삭제가 아니라 활성 포인터를 `legacy-service-before-grounded-20260919-v1`로 바꾸는 작업이다.
`activate`에 되돌릴 `--run-id`와 현재 예상 버전 `--expected-active`를 함께 지정하여 동시 변경을 감지한다.
운영 DB 이름만 바꾸어 임의 실행하지 말고, 백업·검증·배포 상태와 활성 버전을 확인한 후 수행한다.

API 검증기는 [`audit_grounded_service_api.py`](audit_grounded_service_api.py)이며, 선택 버전·스냅샷,
67개 관계/기간 응답과 근거 페이지, 기업별 감성·NULL, 커서 중복, 다른 버전 뉴스가 섞이지 않는지를 확인한다.
운영 완료 영수증과 실제 API 결과는 아래 기록에 보존했다.

배포 보조 도구 [`manage_grounded_release.py`](service_delivery/manage_grounded_release.py)의 기본 `plan`은 읽기 전용이다.
실행 가능한 `production-check`는 별도 루프백 컨테이너를 통해 운영 DB에 V7을 적용하므로 읽기 전용 점검으로 취급하지 않는다.
`deploy`는 BE/FE 이미지와 배포 상태를 전환하며, 분석 버전 선택은 게시 스크립트가 담당한다.
변경 동작에는 `--execute`가 필요하고, 실제 검증 영수증과 현재 배포 상태가 맞아야 실행된다.
이 도구를 저장소에 보존했다는 사실은 운영에서 실행했다는 뜻이 아니다.

## 복제 DB 뉴스 조회 성능 확인

실제 배치 데이터가 들어간 `cosmos_grounded_review_20260919`에서 `NewsQueryRepository`의 수정 쿼리를
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`으로 확인했다. 필터 후보와 페이지를 먼저 확정하고 해당 페이지의 상세 정보를 조회하도록 변경했다.

| 실행 사례 | PostgreSQL 실행 시간 | 계획 수립 시간 |
|---|---:|---:|
| 기업 + 부정 감성 필터, 페이지 크기 5 | 52.510 ms | 19.531 ms |
| 기본 뉴스 목록, 페이지 크기 20 | 63.768 ms | 9.627 ms |

근거 보고서: [`news_query_plan_result_v2.json`](artifacts/service_db_grounded_20260919_v1/news_query_plan_result_v2.json).
이 값은 복제 DB의 개별 SQL 실행 측정치다. 운영 HTTP 응답 시간, 동시 사용자 부하 성능이나 p95 지표를 뜻하지 않는다.
수정 SQL이 포함된 전달 패치를 다시 생성했고 격리 적용·전체 파일 바이트 비교를 통과했다.
수정한 최종 JAR로 복제 DB의 153개 HTTP 요청을 검사하여 모두 통과했다. 67개 관계/기간과 모든 근거 페이지,
기업별 감성 필터·NULL 발행시각·뉴스 커서·이전 버전 격리·기업 지표를 포함한다.
별도 PostgreSQL/Redis 환경의 기존 통합 테스트 43개와 최종 뉴스 통합 테스트 8개도 통과했다.
보고서: [`review_api_audit.json`](artifacts/service_db_grounded_20260919_v1/review_api_audit.json).

운영 적재 후 `runs`·`active_run`에 통계가 없어 실제 1행을 1,330행으로 추정하면서 근거 API에 5~10초 지연이 있었다.
19:57 KST에 두 메타 테이블의 `ANALYZE` 후 동일한 단건 조회가 57 ms로 확인됐다.
게시 스크립트의 `capture-legacy`·`stage`·`activate`에도 해당 통계 갱신을 포함했다. 이 조치는 BE JAR/전달 패치를 바꾸지 않는다.
최종 운영 API 153건의 최대 단건 응답은 3.957초였다. 동시 사용자 부하 테스트나 p95 측정치는 아니다.

## 운영 완료 증거

운영 새 버전 적재는 366.01초, 활성 전환은 15.64초로 확인됐으며 두 단계 모두 통과했다.
다음은 서버의 지정 영수증 7개만 읽어 보존한 파일이다. 개인키·환경변수 파일·DB 전체 덤프는 수집하지 않았다.

| 기록 | 결과 |
|---|---|
| [`production-check-result.json`](artifacts/service_db_grounded_20260919_v1/production_verified/production-check-result.json) | 운영 V7·헬스·검증 환경 준비 PASS |
| [`production_capture.log`](artifacts/service_db_grounded_20260919_v1/production_verified/production_capture.log) | 이전 공개 조회 결과를 legacy 버전으로 보존 |
| [`production_stage.log`](artifacts/service_db_grounded_20260919_v1/production_verified/production_stage.log) | 새 버전 VALIDATED, 단계 당시 legacy 선택 유지 |
| [`production_activate.log`](artifacts/service_db_grounded_20260919_v1/production_verified/production_activate.log) | 새 버전 PUBLISHED, 활성 run 일치 |
| [`production_api_audit.json`](artifacts/service_db_grounded_20260919_v1/production_verified/production_api_audit.json) | 앱 전환 전 운영 DB·임시 18083 API 153건 PASS |
| [`application-deployment-result.json`](artifacts/service_db_grounded_20260919_v1/production_verified/application-deployment-result.json) | 19:58:12 KST 앱 배포 PASS, PostgreSQL/Redis 불변 |
| [`live_api_audit.json`](artifacts/service_db_grounded_20260919_v1/production_verified/live_api_audit.json) | 실제 운영 18081 API 153건 PASS |

실제 브라우저의 뉴스 감성·원문 인용·미산출 영향도 표시 확인은
[`live_ui_verification.json`](artifacts/service_db_grounded_20260919_v1/live_ui_verification.json)에 기록했다.
현재 18083 임시 검증 컨테이너는 제거됐으며 해당 포트의 보고서는 배포 전 검증 기록이다.
최신 뉴스 날짜의 UTC/KST 대조는 게시에 사용한 검증 투영본을 읽어 확인한
[`latest_news_time_audit.json`](artifacts/service_db_grounded_20260919_v1/production_verified/latest_news_time_audit.json)에 보존했다.
영수증 크기와 SHA256은 [`collection_manifest.json`](artifacts/service_db_grounded_20260919_v1/production_verified/collection_manifest.json)에,
운영 버전 선택·되돌리기·Jenkins 재배포 절차는 [`final_delivery_checklist.md`](artifacts/service_db_grounded_20260919_v1/final_delivery_checklist.md)에 있다.

## 현재 완료된 로컬 확인

- 원본 135개 JSONL 분할의 SHA·정확한 스키마·건수와 HDFS 영수증 연결 검증.
- 서비스 투영 및 payload 단위 테스트 각각 8개 통과.
- 모든 493,263개 인용의 원문 구간·리비전·파일/행 해시·인용문 SHA 일치.
- 공개 뉴스–기업 246,117쌍의 canonical 감성 완전 일치, 누락 외래키와 합성 영향도/신뢰도 0개.
- 패치의 독립 적용 검사와 적용 결과 전체 바이트 비교 통과.
- BE/FE 통합 검증과 운영 전환 완료. 위 영수증은 데이터·API·배포 검증이며 예측 정확도나 자동 뉴스 갱신 완료를 뜻하지 않는다.
