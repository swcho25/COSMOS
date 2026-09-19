# 스토리 3 (영향도) 인계 문서

## 2026-09-19 후속 요청 — 기능별 커밋·푸시, 전체 관계 표시는 다음 작업

운영 전환 뒤 사용자가 **지금까지 작업을 기능별 커밋으로 만들고 GitLab에 푸시**하도록 명시 승인했다.
브랜치는 팀 규칙대로 `S15P21C205-48`을 유지한다. 조회 시 원격에는 dev/master/prototype/study만 있어
기존 이슈 브랜치가 없으므로 강제 push 없이 같은 이름의 작업 브랜치를 새로 게시한다.
이후 사용자가 **MR 생성도 명시 요청**했으며, 현재 운영 기준 소스가 있는 `dev`를 대상으로 한다.
코드·테스트·문서는18기능 묶음, 모델/원문/대용량결과/접속키/실험 artifacts는 Git 제외다.

추가로 관계4종 통계를 요청해 운영 DB에서 확인했고 `RELATIONSHIP_TYPE_STATS_20260919.md`에 기록했다.
과거전체119(공급20·투자54·협력38·경쟁7), 기본30D9, 7D1, 90D57이다.
화면의 **주가 고도 `1D/1M/3M`은 주가 등락률 기간**이며 전체 관계 조회 기간이 아니다.
전체 그래프가 기본30D만 조회해 관계가 적게 보이는 문제에 대해 사용자가 "다 보이게" 하는 방향을 말했지만,
곧 **"일단은 커밋만 해서 푸시하자"**라고 범위를 정했다. 따라서 **전체 관계119개 표시 변경은 아직 하지 않았다.**
후속 구현 때 과거 관계의 존속 여부와 최근 기간 점수를 분리하고, 근거 없는 점수나 현재 유효성을 만들지 않는다.

---

## 2026-09-19 20:01 KST 완료 — 승인된 새 분석 데이터의 운영 서비스 전환

사용자의 **“변경 가자~”** 승인에 따라 HDFS `lake-grounded-20260919-v1`을 서비스 DB의
별도 버전으로 적재하고 BE/FE 운영 전환·API·화면 확인까지 마쳤다. 아래 "진행 중" 기록은
이번 완료 이전의 작업 이력이다. 이 배치를 다시 stage/deploy하지 말고 audit으로 확인한다.

- 운영 release `grounded-20260919-v1`, 이전 `d2e482c04396-53` 보존.
  `application-deployment-result.json`: **2026-09-19T10:58:12Z PASS**.
  PostgreSQL/Redis 컨테이너 재시작·이미지 정리 없음. 사용자 계정·스크랩·관심기업 삭제/덮어쓰기 없음.
- 운영 FlywayV7 및 기존 버전 캡처 PASS. 전환 직전 뉴스 **87103건**, 문서183152,
  기업문서190768을 `legacy-service-before-grounded-20260919-v1`에 보존했다.
  기존 전체 DB 백업도 서버에 보관하며 개인정보 포함 백업은 로컬로 가져오지 않았다.
- 운영 stage **366.01초 PASS**, activate **15.64초 PASS**.
  활성 run `lake-grounded-20260919-v1`, snapshot `a70eb366-22cd-59de-88a3-fc7bceac09ca`.
  뉴스219662·공개뉴스기업246117쌍·공시797·과거관계119·관계근거541·문장근거493263·기간점수67·기업지표606.
  기본30D 관계선은9개이며119개 전체가 현재30일 관계라는 뜻은 아니다.
- 최종 JAR SHA `99f8aa5d4d03df252f19986a8056985827b04d0331acc85484f4e75993c61a24`.
  운영 backend 이미지 `sha256:b2b3e35409632308915389cd56a89177bac16b8f656cdc81bbbd0d8c75d2cc82`.
  BE 격리 통합43개·최종뉴스8개 PASS, FE build/lint/회귀6개 PASS.
  복제/운영전환전/실제운영 API 각153요청 PASS: 전체67관계·기간/원문인용/뉴스감성/NULL/커서/버전격리/지표대조.
  실제 운영 `live_api_audit.json`의 최대단건3.957초이며 동시사용자 부하성능 수치는 아니다.
- 운영전환 전 근거조회가 느렸던 원인은 `active_run`의 통계 부재: 실제1행을1330행으로 추정.
  `ANALYZE cosmos_analysis.runs; ANALYZE cosmos_analysis.active_run;` 뒤 같은 근거 API57.09ms 확인.
  BE 변경/재기동 없이 개선됨. 게시기 capture/stage/activate 후에도 두 메타테이블 ANALYZE하도록 반영하고
  서버 스크립트 동기화·문법 검사 완료. 데이터 테이블도 적재 직후 FK검사 전 ANALYZE 유지.
- 공개 HTTPS 뉴스 화면에서 새 분석 버전 이름, 긍정/부정/중립/보류, 뉴스–기업 원문 인용,
  미제공 신뢰도, 미산출 영향도의 "분석 전" 표시를 실제 확인했다.
  예: DB하이텍 기사와 삼성전자 연결의 실제 인용은 12인치 집중/8인치 생산능력 감소 문장이다.
  제목만으로 연결 오류나 감성 정답을 단정하지 않는다. 경제적 정확도 독립 검수 완료를 뜻하지 않는다.
- 데이터 기준은9/16 00:00UTC. 확인된 최신UTC발행시각은 `2026-09-07T23:21:28Z`,
  한국 화면에서는 `2026-09-08 08:21:28 KST`다. 확정UTC가 없는 뉴스도 별도로 있다.
  자동 최신뉴스 재분석/갱신은 이번 범위에 없다.
- **현재 감성은 기업별 금융 감성이다. 새 배치 GNN 영향값은 미산출(NULL), 생성형 LLM은 미연결.**
  GraphRAG 설명은 관계 검색+고정 소개+정확한 원문 인용이다. 생성형 설명 완료/주가 예측 완료라고 말하지 않는다.
- 임시 검증 컨테이너3개와 임시 환경파일3개 정리 PASS. 운영4컨테이너 ID/image/StartedAt 불변, BE/FE health 정상.
  clone DB/DB백업/HDFS구판/새payload/복구용이미지는 보존. 예약 작업 생성 없음.
- 전달: `README_GROUNDED_SERVICE.md`, `service_delivery/grounded_20260919.patch`(42파일,
  SHA `ca00ea10b8dd28c214d349074090c3439c1ab1162fd108e171cf84f2a4736d23`).
  운영기준 소스의 격리 변경만 패치로 제공했고 루트 BE/FE는 덮어쓰지 않았다.
  **추후 Jenkins 재배포 전에 팀 배포 브랜치에 패치를 반영해야 한다.** commit/push/MR은 하지 않았다.

상세 운영 영수증은 `artifacts/service_db_grounded_20260919_v1/production_verified`의
`production_*`, `application-deployment-result.json`, `live_api_audit.json` 참조.
같은 작업 루트의 `review-cleanup-result.json`, `live_ui_verification.json`도 보존했다.

---

## 2026-09-19 19:49 KST 진행 중 — 승인된 서비스 운영 적재

사용자가 SSH 확인 후 **“변경 가자~”**라고 서비스 반영을 승인했다. 아래 SSH 확인만 했다는
이전 기록 이후의 작업이다. 운영 V7과 기존 분석 버전 보존을 마쳤다. 새 버전 적재 중이며 운영 앱 전환은 아직 하지 않았다.

- 작업 경로 `artifacts/service_db_grounded_20260919_v1`, 서버
  `/home/ubuntu/cosmos-grounded-service-20260919-v1`.
- 서버에 전체 DB 백업 `before.dump`(60843158바이트,
  SHA `dbe1376026d2fb2808b93dbbd2c32906532cd90252d23434a8ed900e86a44d69`) 보관.
  사용자 정보가 들어 있으므로 로컬 다운로드하지 않는다. 복제 DB
  `cosmos_grounded_review_20260919` 복원 성공.
- 운영 기준 커밋 `d2e482c043968865d4770010bd12f32481725f2a`를 별도 `service-source`에
  펼쳐 BE/FE 수정. 실제 운영 Spring Boot4.1.1/JDK21 유지. V7이 `cosmos_analysis` 버전 테이블과
  `current_*` 뷰를 만들며, `active_run` 한 행으로 선택한다. 기존 public 수집/사용자 쓰기 보존.
  알려지지 않은 감성·영향·확신도·UTC시각은 NULL을 유지한다.
- BE 수정 통합테스트43/43 PASS(실제 격리 PostgreSQL17/Redis7); FE 빌드/lint/회귀6 PASS.
  `cosmos-backend:grounded-20260919-v1`, `cosmos-frontend:grounded-20260919-v1` 이미지 준비.
  복제 BE `cosmos-grounded-review`, localhost18082, FlywayV7/JPA/health PASS.
  internal `cosmos_data`만으로는 호스트 포트가 안 열리므로 `cosmos_app`도 연결한다.
- 검증 payload manifest SHA `550adf5ce583e8f5444d7ebd71f1a039e2fd6b5fa029de8dd1c475286f7205d8`.
  뉴스219662/공시797/공개 뉴스기업쌍246117/총문장근거493263/관계119/관계근거541/점수67/지표606.
  원문 인용·offset·소스 SHA 전체와 FK 검증 PASS. 뉴스 최신 발행일은9/7, snapshot은9/16기준이다.
- 뉴스86984개인 운영본과 새본은 UUID/정규화URL 교집합0. 기존 뉴스/스크랩은 보존한다.
  공시639개·관계54개 UUID재사용. 공시 관측800은 SEC종류 중복을 통합한797개 원문이다.
- 새 `publish_grounded_service.py`는 capture-legacy → stage → activate 단계별 트랜잭션을 사용.
  복제 DB의 legacy 캡처 완료. 첫 stage는 통계 미갱신 때문에 FK 쿼리가 O(N²) 계획을 선택하여
  해당 복제 세션만 취소·rollback했다. 테이블별 ANALYZE를 먼저 실행하도록 수정한 재실행은
  `review_stage_optimized.log`에서371.61초 PASS. 새 버전 활성화, legacy로 되돌린 API 확인,
  다시 새 버전 활성화까지 복제 DB에서 PASS. 현재 복제 DB는 새 버전이다.
- 복제 API 감사의67관계/기간·원문인용은 통과했다. 이후 companyId
  `5ce31928-4f2a-5046-9f4d-a8e8723592df`, sentiment=NEGATIVE, size5 뉴스필터가60초를
  초과해 해당 읽기쿼리만 취소했다. BE service_api_contract 에이전트가 NewsQueryRepository의
  반복 LATERAL 조회를 개선 중. 첫 개선의 실제쿼리103ms; 기본목록5.49초도 추가 최적화 중.
  최종 JAR→복제 backend 이미지 교체→전체 API감사 재확인 후 운영 적용한다.
- 위 뉴스 조회는 최종 수정 완료. 최종 JAR SHA `99f8aa5d4d03df252f19986a8056985827b04d0331acc85484f4e75993c61a24`.
  실제 SQL 기업/부정52.510ms·기본목록63.768ms; 추가 격리 뉴스 통합8/8 PASS.
  최적화 clone의 최종 API153건 PASS(`review_api_audit.json`), 최대 개별 요청3.152초.
- 운영 `production-check` PASS: V7 적용·localhost18083 healthUP·운영4컨테이너 불변.
  기존 운영 버전 capture PASS44.93초(뉴스87103·문서183152·기업문서190768).
  **현재 운영 stage 실행 중(session98260)**. 원격 `production_stage.log`, publisher PID의
  DB backend266160이19:49경 `evidence_context` INSERT 실행 중인 것 확인.
  완료 후 activate →18083감사→배포helper deploy→18081/HTTPS/UI감사 순서.
  사용자에게 **20시 전후 완료 예상**을 알렸다. 아직 완료라고 보고하면 안 된다.
- `audit_grounded_service_api.py`로 67개 관계/기간과 인용·감성·미상시각·뉴스커서를 대조한다.
  `manage_grounded_release.py`는 실제 배포 compose2개와 env파일을 그대로 사용하고,
  backend/frontend만 바꾼다. production-check는 PASS, deploy는 아직 미실행.
  운영 PostgreSQL/Redis 재시작·이미지삭제·커밋/push/MR·예약 작업은 하지 않는다.
- GraphRAG 설명은 검증된 관계 검색 + 원문 인용 + 정형 설명이다. 생성형 LLM을 붙인 상태가
  아니며, 이번 새 데이터의 GNN 영향값도 아직 계산하지 않았다. 사용자에게 이 구분을 설명했다.
- 재배포 때 변경이 유실되지 않도록 `service_delivery/grounded_20260919.patch` 및 재생성 도구,
  `README_GROUNDED_SERVICE.md` 초안 작성. 루트 기존 BE/FE는 덮어쓰지 않았다.

---

## 2026-09-19 후속 — 서비스 서버 SSH 접속 확인, DB 반영 미실행

서비스 DB 반영 사전 조사 후 사용자가 잠시 대기를 요청했고, 이후 새 접속 키와 서버 담당자가
확인한 호스트 지문을 제공하며 접속 테스트를 요청했다. 이번 후속에서는 접속과 컨테이너 확인만
완료했다. **서비스 DB 데이터·스키마·배포·기존 HDFS 적재본은 변경하지 않았다.**

- 서비스 서버 `ubuntu@j15c205.p.ssafy.io:22`, 새 개인키는 사용자 Downloads의
  `j15c205_loader.pem`. 개인키를 저장소에 복사하거나 내용을 출력하지 않음.
- 서버 ED25519 지문 `SHA256:zFgKE9ec9dqzuU5Ajq2knAGy30R82CGzhoEfzlYOhvk`를 실제
  호스트 키와 대조해 일치 확인. 작업 전용
  `artifacts/service_db_grounded_20260919_v1/service_host_candidate.known_hosts`와
  `StrictHostKeyChecking=yes`, `HostKeyAlgorithms=ssh-ed25519`를 사용해 접속 성공.
- Windows 내장 ssh-keyscan은 sntrup761x25519 알고리즘 오류가 있어 Git 포함 ssh-keyscan으로
  공개 호스트 키만 조회했다. 미확인 키를 자동 신뢰하는 옵션을 사용하지 않는다.
- 개인키 ACL에 다른 사용자 그룹 읽기 권한이 있어 OpenSSH가 처음 거부했다. 해당 파일만
  현재 사용자·SYSTEM·Administrators 접근으로 제한한 뒤 인증 성공. `sudo -n` 사용 가능.
- 서비스 컨테이너: `cosmos-postgres-1`, `cosmos-backend-1`, `cosmos-frontend-1`,
  `cosmos-redis-1`. DB를 임의 갱신하지 않는다. 접속 확인 영수증은
  `artifacts/service_db_grounded_20260919_v1/ssh_access_verified.json`.
- 이전 실제 DB 읽기 감사: graph snapshot은 구90일 run의 9/16 00:00Z 한 개,
  current/history373행·기업집계606행. 새 run은 같은 기준일이므로 일반 Loader의 최신시각
  검사 우회나 기준일 변경 금지. 별도 버전/명시적 정정 계약과 기존 이력 보존이 필요하다.
- 실제 문서 Loader 계정은 뉴스·기업연결 쓰기가 가능하지만 근거 두 테이블 권한이 없고,
  관계 Loader 계정도 근거/뉴스 접근 권한이 없다. `company_metric_history`의
  positive/negative_count는 실제 DB에서도 NOT NULL인데 새606행 중344행은 NULL이다.
  미분석을0으로 바꾸지 않고 DB/API 계약을 맞춰야 한다. 실제 DB는 SEC와 구성점수 컬럼을
  이미 지원하므로 로컬 V1~V3만 보고 누락됐다고 판단하지 않는다.
- 사용자는 구판과 새 선별본을 구분하고 선택한 버전의 뉴스·감성·관계·근거·점수를
  일관되게 보여주는 방식을 문의했다. 현재 뉴스 분석 결과 전체의 버전 전환은 미구현.
  상세 실제 스키마·운영 Loader 코드는 위 작업 폴더의 preflight/operating_* 자료 참조.

---

## 2026-09-19 17:18 KST 후속 완료 — 새 선별 결과의 HDFS 적재·검증

사용자가 **적재 형식 변환 → 특징·집계·스냅샷 → 새 버전 HDFS 게시·검증**을 명시 요청했다.
이전90일본의 문제가 많아 지우는 방안도 질문했으나, 새 버전 검증 전에는 이전본을 보존한다고
설명했다. 기존 분석 결과는 새 계산에 섞지 않고 뉴스 ID 대응표만 재사용했다.
서비스DB 반영·GNN 재학습/교체·commit/push/MR·예약 작업은 범위 밖이다.

- 작업 루트 `artifacts/lake_grounded_20260919_v1`, run **`lake-grounded-20260919-v1`**.
  기준은 동결 입력과 같은 **2026-09-16T00:00:00Z**. 최신 실시간 시점으로 바꿔 말하지 않는다.
- 새 코드 `grounded_lake_identity.py`, `grounded_lake_features.py`, `prepare_grounded_lake.py`,
  `spark/publish_grounded_lake.py`. 기존 재개 게시기는 opt-in 그래프 sidecar/출처 metadata만 확장.
  이전 호출의 기본 경로 제한은 유지했다. [계약·경로](README_GROUNDED_LAKE.md).
- `prepared_v1` **로컬 준비와 독립 검증 완료**. publication SHA
  `23f3b08f8bd79d7496c0f1c5cdb799234971ba830b8d3ed0e1f8618de25e52d8`.
  기본219790뉴스+관계원문4개=219794리비전, 중복132통합→**정제219662**.
  원래기업연결246264행 보존, 집계는중복147통합한**246117쌍**, 충돌71쌍 감성NULL.
  기존UUID3720재사용, 충돌0. ID맵1045234키(이전393501키와metadata불변).
- 16데이터셋/135JSONL분할/6098861173바이트. 기업특징·집계606씩, 관계특징·점수67씩,
  노드202, 기간관계67, 과거관계119, 근거541(NEWS177·DART364),
  공시기업관측800, 원문리비전220048(뉴스219794+공시254), 뉴스감성246264.
  감성분석174680·보류71584. 뉴스감성은GNN주가예측이 아니다.
- 주가단일SHA검증본으로12118관측 사용. 최신KOSPI9/11·NASDAQ9/10의오래됨을명시.
  모든뉴스에검증된UTC시각이있는것은아니다. raw/확정UTC/보수적인집계available_at을구분.
  공시의날짜끝23:59Z는정확한UTC시각으로승격하지않고`published_at=NULL`로보존.
- 독립검사:246264원문·감성입력전체PASS(`edge_source_validation.json`),
  16데이터셋·220048원문·541인용·FKPASS(`prepared_independent_audit.json`),
  606기업지표6666값·606주가특징4848값·67점수/상관PASS
  (`prepared_v1_independent_feature_audit.json`), canonical뉴스관계중복0.
  신규identity15+기존15,feature15,준비10,게시18테스트PASS.
- 전송패키지 `publication.tar.gz` **1660661668바이트**, SHA
  `e79937a34af7ab710c7fbe0c511dc86dd57e62da00c8013ad939260506ee02ec`.
  manifest의분할/metadata와격리publisher3파일만포함. 실패identity진단DB나기존모델은미포함.
- **17:18 KST 최종 확인: HDFS 새 버전 게시·독립 재읽기 감사 PASS**.
  서버 `/home/ubuntu/ner-work/lake_grounded_20260919_v1`, 분리 실행 PID3091925,
  시작16:55 KST, Spark application `local-1789804685669`, 종료 코드0.
  135/135분할·16/16데이터셋·12/12경로그룹, 총1981088행(여러 테이블 합계, 기사 수 아님).
  Parquet135파일 **2221706265바이트**, 빈 데이터 파일0. Spark local[1]/driver4g로 완료.
  `extract_and_launch.py`가packageSHA/경로/파일SHA/독립PASS를검증했다.
  원래서버백필프로세스/구버전코드를덮어쓰지않는다.
- 최종확인완료: 16종Parquet전체양방향대조·건수/키/스키마/체크섬 및
  `/data-lake/metadata/runs/run_id=lake-grounded-20260919-v1/_VERIFIED`.
  별도CLI로 최종135파일의 크기/체크섬, 전역marker/receipt/5종출처metadata를 다시 확인했다.
  로컬 `hdfs_verified/receipt.json` SHA
  `1d098cc75106e90a544b948ede672639ad5e1b2ae11bd35a783a0adc1bdcf5b1`,
  `hdfs_verified/final_hdfs_audit.json`에독립검사PASS·건수·기존본보존증명.
  `audit_hdfs_final.py` SHA `9e8dbf2321b5671f5aa38e02647e6871e202dab6e9b55d84f036125930e54eb3`.
  감사기의HadoopCLI checksum은메타12바이트+MD5 16바이트를검사해 publisher.toString과대조;
  감사변환 정상6/변조거부9검사PASS. 게시기/동결입력코드변경없음.
  기존90일본marker SHA불변확인. **구90일본삭제·DB적재·모델교체·예약작업은수행하지않음**.
  다음소비자는위완료marker를확인한이run만명시해읽어야하며상위경로전체run을합쳐읽지말것.
  같은기업/기간에뉴스·공시점수가둘다있는관계0, 119과거관계와67기간별관계행을구분.
  9/16기준정적자료이며실시간9/19갱신본이아님. 재게시/재학습할필요없음.

---

## 2026-09-19 후속 완료 — 관계 추출 범위 v5 / 통합 그래프 v3

사용자 요청: 관계112개가 적다는 확인 후 **관계 추출 범위를 조금 늘리기**. 이후 114,162후보의 의미·시간·간접 전이를 질문했으며 원래 작업을 취소하지 않았다. 새 GNN 학습이나 HDFS/서비스 게시 요청은 아님.

- 새 코드 `kg_grounded_relations_v5.py`, `scan_grounded_relations_v5.py`, `review_relation_expansion.py`와 테스트. 기존 v3/v4/완료 그래프/모델은 변경하지 않음. v5 SHA `411b30c8b07c13637303e8f3ab79ede89dce87d620b1d18652ccbb8f85173296`.
- 문법 확장: 독립 절, 명시 상대방 목록의 역할별 연결, 제한된 같은 문단 인접 대명사, 후행 공급자와 추가 영문 협력/체결 계약 표현. 대명사 오귀속·허위/부정 주장·역할 없는 공동등장 차단. 일반 endpoint 복원은 비시점 별칭만으로 승인할 수 없어 보류. 기존 특정 KAI 예외는 그대로 유지.
- `artifacts/kg_relations_v5_expanded_20260919_v1`: B 후보 기업이2개 이상 남은 **114,162기사 전부 처리**. 기존183후보 원본 보존+신규12=195근거 후보. 기사 처리 약193초; SHA검사/검수/통합 시간은 별도. manifest SHA `9dcd267f4b31eb4bd759a12994e701a50dcb4103c8293af7059be2ec6d7e0dfb`. 이는 전체 원천 뉴스나 모든 가능한 관계 범위가 아님.
- 신규12의11기사 전문을 두 에이전트가 분담 검토: **유지9·보류2·제외1**. 미래 행사 MOU와 포스코케미칼 과거 사명/법인 시점 근거 부족은 보류; Netflix의 Apple 매출 기여를 공급으로 잘못 읽은 것은 제외. `kg_relations_v5_review_20260919_v1/review_0.jsonl`, `review_1.jsonl`. 기존15보류 유지. 총 검수177keep/17review/1exclude. 정책 `kg_relation_review_policy_20260919_v3.json` SHA `595de66c0394258f29a5ec9597c39e40a962b946dc3a70aea4c6a52fe9f33e7d`.
- 새 로컬 통합본 **`artifacts/kg_integrated_20260919_v3`**: **119관계 / 541근거(뉴스177·DART364) / 430문서**. 공급20·투자54·협력38·경쟁7. 관계 참여기업76개, 유형을 합친 기업쌍110개. manifest SHA `7857801e51e9024ae2c358200b7d6e971a3ff7b80cd20b2c6fc5b6f1e495b539`; temporal SHA `46bff25a55aeaba5f60329076a28593c3fe329f860b750692be3f92287f688aa`.
- 추가7관계: 삼성전자→현대차 공급, 포스코퓨처엠→LGES 공급, LG화학→LG전자 공급, LGU↔현대차/기아 협력, 현대차↔SK이노베이션 협력, LG CNS↔Palantir 협력. 기사 당시 원문 진술이지 현재 유효성 증명 아님. LG화학 공급2기사는 사실상 중복보도, LGU2edge는 공동협약1건이므로 독립사건으로 세지 말 것.
- **추가 근거는 기존 기준시점 최근90일 밖**이라 7D1/30D9/90D57점수 행은 그대로이며 점수파일 SHA도 v2와 동일 `d25ccbb629a1b702b500b9b84ee8060df086763bbcf21fb4af051a42e1d87c0d`. 같은 기간 뉴스·공시 두 구성점수가 모두 있는 관계는 여전히0. 과거 관계 범위 확대를 최근 화면 개선으로 소개하지 않음.
- 회귀검사92개(기존v3/v4 51 + v5 28 + 배치경계/재개10 + 검토완전성3) 및 별도 반례7개 통과. `integrity_verification.json`:541원문·67점수 독립 검산 PASS. `all_relationships_verification.json`:119개 관계 실제GraphRAG 조회·인용 PASS. `new_relationship_examples.json`에 신규7관계의 실제 설명 보관. 권장 추출형 설명기의 기존 기본 경로v2는 유지하므로 새 조회는 directory로v3를 명시.
- `kg_relations_v5_discovery_20260919_v1/cases.json`: 개발누락15+반례6, 원문/offset/SHA보존. `comparison_v5.json`: 실제 원문·실제사전에서는 누락3개 복구, 반례6개 금지쌍0. Paychex–PayPal은 단위시험의 전체법인명과 실제별칭 입력 차이로 미복구. 3/15를 재현율로 제시하지 않는다.
- 재개: 출력 STOP 파일로기사경계 중단, 동일코드/입력 `--resume`; source/code/체크포인트 SHA·카운터검증 후재개. 이번전체배치는완료했으므로 다시돌릴필요없음. [사용법·한계](README_RELATION_COVERAGE_V5.md).
- 최종 별도 에이전트 감사 `kg_relations_v5_review_20260919_v1/independent_final_audit.json` SHA `037da5adb8cddb559aa849ae2fc2e3709edbe1c90e69c9978c052d888825b810` PASS:114,162개 대상·완료ID·영수증·카운터 exact, 원본8GB DB와 코드 해시 재확인, 기존183JSONL 원행 보존, 신규12 원문/24endpoint 및 검토 정책완전성, 통합 기존532+승인9만추가 확인.
- GNN/기존번들/뉴스감성/원B선별/원본/HDFS/서비스DB/배포/commit/push/MR/예약 작업 변경 없음. **새v3그래프를 GNN에 적용한 성능은 검증하지 않음.** 기존37.45%는v2입력의 이전 실험 수치다. 간접 전이는 후보·경로와 추정값을 제공하는 기능이지만 이번 GNN 평가의 직접기업 정답률을 간접예측 성능으로 설명하면 안 됨.

---

## 2026-09-19 08:08 KST 완료 확인 — 아래 실행 중 기록을 대체

요청한 **1~5단계의 로컬 산출물·GNN 재학습·성능 검증 완료**. 6번 백엔드 전달/서비스 연동은 시작하지 않았다. **성능 개선은 확인되지 않았으므로 모델 교체를 권장하는 결과가 아니다.** [최종 결과 문서](RESULTS_GROUNDED_GNN_20260919.md)를 먼저 읽는다.

- `grounded_gnn_20260919_v1/dataset_v3`로 후보/뉴스 제거/신규 그래프 제거 각각 3시드, 9회·72에포크 완료. 03:50:26~05:00:56 KST 약 70분 31초. 학습 프로세스 종료·GPU 해제 확인. 재학습할 필요 없음.
- 테스트 36,518쌍의 후보 방향 정확도: KOSPI 1D 42.42%, 3D 40.02%; NASDAQ 1D 41.21%, 3D 39.25%. 시장×기간 4셀의 거래일 정확도 동일 비중 평균 39.5138%, 시장별 기준선 40.8213%. HAC/Holm에서 후보의 유의한 우위 없음. KOSPI 3D 기준선 대비 -4.1188%p는 유의한 열위.
- 실제 기존 stage3 모델과 동일 legacy 4,813쌍·13개 식별 열 대조 완료. 같은 거래일/4셀 집계 기존 39.6143%, 새 후보 37.4494%. 이미 본 2023년 보조 비교, 유의성 미검정. 새 모델 미응답 70쌍도 분모 포함. `reporting_repair_20260919_v1/old_new_legacy_comparison.json` 및 `compare_frozen_old_legacy.py --verify`에 SHA·재현 기록.
- 독립 검증 72지표 행/120통계 대비/앙상블·분모/9개 CPU 체크포인트 재로딩 PASS. `training/independent_verification.json` SHA `97e4df4f831fe69271fb0f357161b90321a371ed2cd5e7f481dc04dc6d0a9948`.
- 최초 자동 검증은 Windows PowerShell `Get-FileHash` 오류로 실패. 원래 실패 영수증 보존 후 래퍼를 .NET SHA로 수정·실행해 `training/independent_verification_stage_v2.json`에 `verified_with_rank_report_correction` 기록.
- 원래 metrics의 상수 예측 순위 상관 12개 NaN을 독립 검증기가 발견. float32 std의 상수 판정 문제로, 정확도 오류가 아니다. 명시적 `--repair-rank-report`로 별도 `training/independent_verification.corrected_metrics.csv`에서 해당 12개만 0으로 수정. 원래 metrics·HAC·예측·모델·학습 코드는 동결 유지. 검증기 SHA `2a3df557bde2ae1f144bc4bca5cfa9f0c907e67bb5781264036f617bceccf7dc`. 별도 에이전트가 다른 열 불변·원본 SHA·동률 순위 계산을 재확인.
- 학습 완료 영수증 SHA `72194e56e1560f0a2d5e8587341245fc06d3aa3436d120b39b02d5953963dd60`; dataset_v3 prepared SHA `e1523dd7c78fd5d10d7953e2920b6b26333676f679e12acffa769252415cb304`.
- 통합 그래프 112관계/532근거(뉴스168·DART364), 422문서. GraphRAG는 원문과 다른 생성 표현이 NLI를 통과한 실제 사례를 보존하고 권장 경로를 제한된 설명+원문 인용으로 교체. 전체 112관계 검증 완료. 현재 뉴스·공시 두 점수가 모두 있는 동일 기간 관계는 0이며, 간접 영향 예측 성능은 이번 검증에 포함되지 않음.
- 운영 서비스·DB·HDFS 게시, 기존 번들 교체, commit/push/MR, 예약 알림 없음. 원래 B pc1 STOP 유지. 1~5단계 완료가 전체 원천 뉴스 재선별 완료나 모델 품질 목표 달성을 의미하지 않음.

---

아래는 진행 당시 기록이다. 최신 상태는 위 완료 확인을 따른다.

**2026-09-19 최신 진행 — 뉴스·공시 관계/구성 점수/근거 검색·추출형 설명 보완 완료. 03:50 KST부터 GNN 9회 학습·평가가 실제 GPU에서 진행 중. 성능 결과는 아직 없음.**

이번 요청의 1~5번 중 1~4번은 아래 제한을 가진 로컬 산출물로 구현·검증했다.
5번 GNN 학습·성능 검증은 실행 중이며 완료했다고 말하지 않는다. 운영 서비스/DB/HDFS 게시,
원래 모델 번들 교체, commit/push/MR, 예약 알림은 하지 않았다. 원래 B pc1 STOP도 유지한다.

### 현재 확정 그래프·근거

- `artifacts/kg_integrated_20260919_v2`: **관계112개**, 근거532(뉴스168/공시364), 원문422문서.
  종류별 관계 INVEST54/PARTNER34/COMPETE7/SUPPLY17. 관계·근거·독립사건은 다른 수량이다.
  manifest SHA `604cfbdd5e61eb4f882b12383edc7ed71d3bfcb1f8028f94f96f472b8b5e91e9`.
  temporal532행 SHA `56a4932d1b09d02fa4025c2d47188e64fbb528ed5c1100f96726d40c24f33770`.
  `integrity_verification.json`에서 모든인용/시간/방향/참조/67점수독립재계산 PASS.
- `kg_relations_v4_recent_20260919_v1`: 원래v3후보181을변경하지않고최근복수B기사2,052개를
  15초재검사해2근거추가(183후보/74후보관계). 최종검수168유지/15보류. v4추가2건은
  LG유플러스↔카카오협력, 한화시스템→KAI장내지분매입. 공동건설/현재지분/주가효과는추론하지않음.
  KAI원B판정은needs_review유지; 원문명칭·유일별칭·과거DART발행인metadata로이기사의관계상대만복원.
  `kg_recent_endpoint_audit_20260919_v1/audit.json` 및 `kg_relations_v4_independent_audit_20260919_v1/audit.json`.
  정책 `kg_relation_review_policy_20260919_v2.json` SHA
  `dd27f95c241c58230f54a947d45c81b9cd0cf4e41202b5f030a9bf568b54c1f9`.
- 기준2026-09-16T00:00Z 점수7D1/30D9/90D57행. 최근30D·90D 각각뉴스만있는2관계가추가됐다.
  **같은기업쌍·종류·기간에뉴스와공시가모두있는행은현재0**; 현재개인화슬라이더가변하는사례까지
  충분해졌다고말하지않는다. missing은NULL. 과거실제자료의양쪽점수예시
  `historical_component_example.json`: 2023-05-30LG화학→LGES, 뉴스2문서/공시1문서,
  뉴스32.967995/공시18.126925, 50:50=25.54746. 현재스냅샷예시가아님.

### 설명 생성의 실제 실패와 보완

최종v2 실Qwen/NLI CPU검사에서 '카카오와동맹한LGU가AIDC를짓는다'를 카카오도공동건설하는것으로
확대했지만NLI0.9986이통과시켰다. **NLI통과를의미정확도보증으로사용하면안된다.**
원본실패기록 `kg_integrated_20260919_v2/graphrag_cpu_qa_20260919/` 보존.
새권장 `safe_integrated_relation_explanations.py`는4유형모두 **고정된관계안내+정확한원문/URL**만
반환하며자유LLM생성기를호출하지않는다. 이전생성기는연구용으로남김. GraphRAG근거검색은유지.
CPU8개PASS+실제3조회+root의전체112관계조회PASS. 공시보고기업과원문인용을분리보존한다.
`safe_explanation_qa_20260919/resolution_receipt.json` SHA
`55262c84ce0e029fef3d6f865ce0d50e5e90fea38aa6c1da3a994278046c4cb2`;
`all_relationships_verification.json`에112조회확인. 원문자체오류/전체관계정확도를보증하는것은아님.
안내: `README_INTEGRATED_RELATIONS.md`, `README_SAFE_RELATION_EXPLANATIONS.md`.

### 현재 실제 GNN 실행과 재개

- 루트 `artifacts/grounded_gnn_20260919_v1`, 최종입력 **dataset_v3**, 학습출력 **training**.
- 신규190,094문맥인코딩03:48:46완료. 전체토큰누락0, 특징223,371행, PCA재계산최대오차0,
  기존은행byte동일. 실패한overflow시도/원래dataset/v2는그대로보존.
- 학습전전수패턴검사+독립원문검수에서추천/광고혼입12기사,시점미확인시세표4기사를임시격리.
  미래정보누설입증은아님/영구무관뉴스판정아님. 정책
  `grounded_input_quality_20260919_v1/quarantine_policy.json` SHA
  `3b875d8206a8399d11d6ca8dfe98f149e19213c2ae2b9018c1c8c826c27d1ee9`.
- `derive_grounded_training.py`는frozen부모와검수정책을별도derived-v1계보로연결한다.
  학습6기사8감독쌍을loss/prior에서제외 → **99,407기사/111,376감독쌍/112,671입력쌍**.
  검증36,074기사/40,627쌍. 테스트33,210기사/**36,518쌍분모유지**,10기사11쌍미응답,
  예측가능36,507. legacy_val4,813쌍중70미응답그대로. 입력/특징/라벨/비대상배열전수검증PASS.
  prepared SHA `e1523dd7c78fd5d10d7953e2920b6b26333676f679e12acffa769252415cb304`;
  derivation SHA `9604df6acd735495fa50905c0313b1e27355e00fcde4348a8c47ceaa22485a31`.
- GPU 실행Python **PID36636**, 래퍼 **PID8624**, 03:50:26 KST시작.
  `training_launch.json`, `training.stdout.log`, `training.stderr.log`.
  candidate/no_news/no_new_graph×3seed,30epoch/patience6/고정설정. 학습9개모두완료후에만평가.
  03:55확인시candidate seed0 epoch0~3완료, epoch56~66초. 정확도는아직계산하지않았다.
  단순예상8~15epoch면9회약1~2.5시간+평가,전부30epoch면약4~5시간+평가. 확정완료시각아님.
- 각seed `epoch_state.json`이optimizer/RNG/최선모델과완료epoch checkpoint를가리킨다.
  중단후아래명령으로완료모델/epoch를재사용한다. STOP를만들었다면재개승인후그파일만제거한다.

```powershell
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/train_grounded_news.py --dataset AI/graph/artifacts/grounded_gnn_20260919_v1/dataset_v3 --out AI/graph/artifacts/grounded_gnn_20260919_v1/training --device cuda --phase all
```

**실행중준비/인코딩/파생/학습모듈과입력해시를바꾸지말것.** 그래프v2는연간cutoff규칙을따라
기존v1과GNN입력graph배열이동일하다(새2026-08관계를2026-01스냅샷에소급하지않음).
직접기업감독만있고간접기업성능검증은미포함. 이미노출된2023/2024~26탐색적평가이며65%보장없음.

일회성후속검증 **PID4148**도실행했다(`verification_launch.json`,stdout/stderr).
`verify_grounded_when_complete.ps1`가현재학습PID의evaluation완료marker를기다렸다가
`verify_grounded_evaluation.py`로9모델CPU재로딩/72지표·120HAC검정·Holm/분모/원본예측을교차검증한다.
학습중단·실패면완료라고표시하지않고종료한다. 예약작업·알림이아니다.
결과: `training/independent_verification.json`, `training/independent_verification_stage.json`.
학습을다른PID로재개하면후속검증도별도로실행한다. 명령은 `README_GROUNDED_GNN.md` 참고.
남은필수작업: 실제9학습/평가/독립검증의완료또는실패를확인하고,실측성능과한계를사용자에게보고.

---

**2026-09-19 03:33 KST 진행 — 뉴스·공시 통합 v1과 GraphRAG 검증 완료, GNN 입력 준비 완료. GPU 인코딩 호환 수정 중이며 학습은 아직 시작하지 않음.**

사용자 승인 범위는 아래 03:01 기록의 1~5번이다. 현재 서비스/원격 게시/모델 교체/예약은 하지 않는다.

- 뉴스 보완본 `artifacts/kg_relations_v3_expanded_20260919_v2`: 114,162 복수 B 후보 기사 검사,
  근거181/관계72. 기존72는70유지·2법인귀속보류, 추가109는96유지·13문맥보류.
  정책 `artifacts/kg_relation_review_policy_20260919_v1.json` SHA
  `ddc7e23ae46eb0f1d4f36a57f3fbcffda6ee869efd0b3452421452616d07b55c`.
  최종뉴스166유지/15보류. 원문검증·AI검수이며 인간 정답 정밀도·재현율은 아니다.
- 공시 `artifacts/kg_disclosures_20260919_v1/result`: 선택 DART414보고서/254원문파일,
  관계근거364(INVEST345/SUPPLY19),60유형·방향별기업쌍. SEC386관측/관계0.
  `integrity_audit_exact_bytes.json`364인용/728endpoint/254SHA PASS.
  CRLF 원문은 `read_bytes().decode()` 또는 `newline=''`로 읽는다. 보통 `read_text()`로 개행을
  바꾸면 인용offset이 어긋난다. 이전 실패 감사도 원인 추적용으로 보존했다.
- 통합 `artifacts/kg_integrated_20260919_v1`: 관계110/근거530(NEWS166+DISCLOSURE364),
  원문문서420. manifest SHA `f9f57f5ae920a62d71c4b15efb7368b8395ea9117076406c81088afca3abe230`.
  `integrity_verification.json` PASS. 기준2026-09-16T00:00Z의 7D1/30D7/90D55점수행,
  **모두공시점수만존재/뉴스NULL**. 최신뉴스 점수까지 완성됐다고 말하지 않는다.
- 신규 `integrated_relation_graph.py`/`build_integrated_relation_graph.py`/
  `verify_integrated_relation_graph.py`, CPU5PASS. 원문·해시·정확한 기업쌍/관계/방향·과거cutoff검증.
  구성점수는 문서근거량 변환이지 확률·주가영향이 아니다. 둘다존재50:50/한쪽NULL이면존재값.
- 실제 통합 GraphRAG smoke는 `artifacts/kg_integrated_smoke_20260919_v1/graphrag_actual_smoke.json`:
  Qwen2.5-1.5B CUDA+NLI CPU, 뉴스SUPPLY/COMPETE생성검증/PARTNER추출fallback,
  DART INVEST/SUPPLY는 보고기업메타데이터를별도확인한 원문추출형. smoke는 뉴스72인 초기통합436근거로
  실행했고 최종530근거의 source/quote는별도전수검증했다. HTTP/서비스연결은안했다.
- 안내 `README_INTEGRATED_RELATIONS.md` 추가. 이전뉴스전용README와구분한다.

최근90일 진단: 부모기사7,661/B포함쌍11,293/복수B기사2,052/ready쌍4,250/ready기사3,719/
복수ready기사436. frozen v3의최근복수B전체 raw관계0 → 날짜필터보다앞에서누락됨.
`reasoned_teacher`는 별도v4에 상대절 동맹/완료된지분매입/명시컨소시엄역할만 제한추가중.
`selector_label_diagnosis`는 08e396be…기사의KAI상대기업누락추적: merged B에047810이있지만review
(0.4666/include0.4012)라부모include-only에서제외. 원문'한국항공우주산업(KAI)'/유일별칭으로
기사한정관계상대복원근거작성중. 원래B결정/뉴스감성/기존v3/통합v1은수정하지않는다.
새로운관계는별도문맥검수후새manifest로만통합한다.

GNN `artifacts/grounded_gnn_20260919_v1/dataset_v2`: CPU준비174초완료.
train99,413기사/112,679입력쌍/111,384감독쌍; val36,074/40,750/40,627;
test33,210/36,665/36,518. 새특징190,094문맥/구특징33,277. 검증legacy_val4,813쌍은보조평가전용.
최초 `dataset`은 미래라벨가능여부가입력기업까지결정하는문제를발견해폐기대상으로보존했다.
v2는모든ready입력보존/감독mask만가격라벨가능여부사용, 라벨없는1,565입력보존.
v1/v2감독pairs/mask SHA동일. CPU준비12개/학습4개PASS, 실제GAT재개가중치bitwise동일검사포함.

`prepare_grounded_training.py`고정후 `encode_grounded_features.py`어댑터에서로컬Transformers5
TokenizersBackend API호환수정중. 구prepare_for_model없음/overflow첫256토큰만반환문제는전체원토큰
동일성검사가잡았으며절단을허용하지않았다. 실패실행protocol/chunk는별도attempt로보존한다.
**실제GNN학습은아직미시작**, 512문맥파일럿통과후190,094문맥인코딩, 그래프버전확정후
candidate/no_news/no_new_graph×3seed=9학습과검증을실행한다.
신규 `train_grounded_news.py`는epoch별optimizer/RNG/최선모델체크포인트재개,
STOP는현재미완epoch만버리고마지막완료epoch부터재시작한다. 완료모델은건너뛴다.
주평가직접기업감독만있음; 간접영향예측이검증됐다고말하지않는다.
2023/2024~26이미노출기간; 기준선/동일표본/HAC5·20/Holm으로탐색적비교,65%보장없음.
소스실행중변경금지. 그래프v1이 dataset_v2에이미고정되어있으므로추가v2그래프는
기존파일덮어쓰기없이명시적revision을선택하거나동일성영수증으로만연결해야한다.

---

**2026-09-19 03:01 KST 진행 범위 갱신 — 사용자가 1~5번(GNN 재학습·성능 검증까지) 실행 승인, 병렬 구현 중.**

아래 과거 기록의 "GNN 재학습하지 않음"은 당시 범위다. 지금은 (1) 뉴스 관계 추출 보완·검수,
(2) 공시 원문/관계 검증, (3) 두 출처 통합·7D/30D/90D 점수, (4) 통합 근거 GraphRAG,
(5) 새 뉴스/과거 시점 그래프로 GNN 재학습·비교 평가까지 승인됐다.
서비스/운영 DB·HDFS 게시, 기존 번들 교체, commit/push/MR, 예약 알림은 이번 요청에 포함하지 않는다.
기존 pc1 B 미처리분 STOP와 모든 완료 산출물은 보존한다. 새 파일/새 출력 디렉터리로 진행한다.

현재 담당: `reasoned_teacher` 관계 v3/실제 review 파일럿, `selector_label_diagnosis` 원격 DART
원문 확인·최소 반입/검증, `selector_model_diagnosis` 새 GNN 데이터·시점 그래프 어댑터,
root `integrated_relation_graph.py` 출처 통합/구성 점수/GraphRAG 검색.
아직 새 관계 전체 배치나 GNN 학습 시작으로 보고하지 않는다. 지금은 실제 코드/입력 준비 중이다.

초기 전체 계획 추정 24~72시간은 미측정 관계 개선 시간을 포함한 추정이다. 기존 규모실험 특징 생성
약49분,18학습 약76분이므로 GNN 자체가 며칠 걸리는 것은 아니다. 새 후보의 가격검증 전 규모:
train2020~22 105,271기사/118,812쌍, val2023 37,276기사/42,027쌍,
test2024~26 33,929기사/37,384쌍. 실제 적격 건수·파일럿/첫epoch 실측으로 재추정한다.
2023/2024~26은 이전 실험에서 노출된 기간이며 새 blind test라고 부르지 않는다.
날짜만/시간대 미상 기사는 기업 시장으로 시간대를 추정하지 않고 해당 달력날짜가 전세계에서 종료되는
다음날12:00UTC(UTC−12 EOD) 이후 첫 종가를 사용하는 보수적 입력/라벨 정책을 기록한다.
관계 available_at은 feature cutoff보다 엄격히 과거여야 하며 당일 자기 기사 관계/미래 엣지를 넣지 않는다.
관계 수와 성능을 보장하지 않는다. 연구용 학습 가능성과 인간 정답/운영 승인 여부는 별도다.

---

**2026-09-19 02:38 KST 최신 상태 — 뉴스 관계4종 재추출 및 새 KG의 관계별 GraphRAG 로컬 연결·실모델 검증 완료. 공시는 이번 결과에 미병합.**
사용자가 관계는 공급/투자·지분/협력/경쟁4종, 기업별 뉴스 결과는 긍정/부정/중립3종이며 관계마다 근거뉴스·GraphRAG 설명까지 필요하다고 재확인했다. 이어 공시 활용 여부를 물었다.
요구사항 문서 `RELATION_NEWS_CONTRACT.md`. 분류 종류 수와 실제 생성 관계 수를 혼동하지 않는다. 분석 보류는 별도status/NULL이며4번째 감성이나 중립이 아니다.

기존 완료 KG와 뉴스·감성은 보존하고 새 파일만 추가했다:
- `kg_grounded_relations_v2.py`: 정확히4종 관계. 최대주주/지분보유·명시 경쟁 문법 보완, 회사전체인수는INVEST의company_acquisition 세부유형으로 보존. 자산 인수·공동 투자자 간 잘못된 연결·부정/계획/제3자는 제외. CPU37개 PASS.
- `rebuild_grounded_relations.py`: immutable 부모의candidate_ready가2개 이상인 기사22,184개만 재처리, 기사별원문SHA/offset/endpoint 검증, STOP·원자저장·재개·결과해시. CPU10개 PASS, 실제기사4개 사전확인.
- `grounded_relation_graphrag.py`: 기업쌍/관계종류의 직접근거 검색, 기사/URL/원문위치·날짜·출처 반환, 기존Qwen/NLI 재사용, 인용검증 실패시추출형. CPU15개 PASS(root도재실행). 설명검색은GNN을호출하지않고점수를바꾸지않는다.
- `README_RELATION_GRAPHRAG.md`: Python/CLI 사용법. 새수정본 DB를명시해야하며기본은원래KG DB다.

수정관계 배치 **`artifacts/kg_grounded_relations_20260919_v2`**,02:34:35 완료. 기존483,738기사 중복수ready기사22,184개전체처리, 461,554개는2개이상endpoint가없어서관계처리대상아님. 원래기사/감성다시학습·추론하지않았다.
최종 **관계25개: SUPPLY4 / INVEST14 / PARTNER6 / COMPETE1**. 근거72건:SUPPLY6/INVEST57/PARTNER8/COMPETE1. 여기서관계수는src,dst,type,directed 조합이며근거수는원문문장/문서수다.
INVEST57개근거는전부기사에언급된**기존최대주주지분보유**이며새로운투자거래57건이아니다. 같은발표/반복기사가있어독립사건수와도다르다.
새manifest SHA `2a2c2527d5bfc52109afb3bab3007957701057921113539ae53580528108674c`; protocol `d09d92d5996edf545826aef89c7148064e9d4ae4f3db0f13da21eb1d011de861`; DB `485353b846a5e083871814348a6abda5dddacf255d35ce7f550d14411682839e`.
`final_verification.json`: 파일3종 SHA/bytes/행수,DB/코드·endpoint정책/기사별처리영수증/SQLite무결성 PASS. 이전부모DB SHA변경없음을배치전후및GraphRAG reader에서검증.
`final_relation_revision_audit.json`:72근거/72기사/144endpoint의원문offset·정제잔존·원본입력SHA·방향·집계검증통과, 구조·역할오류0. 관계집계25행은고유무방향기업쌍24개(삼성전자–삼성전기는지분/공급2유형). 전기사에서같은관계/정규화문장을합치면32종이며독립사건수로해석하지않는다.
법인귀속재검토2건은 `artifacts/kg_relation_contract_20260919/endpoint_review.json`으로해당기사기업조합만관계보류했다(하나금융그룹/포스코케미칼). 기초뉴스감성라벨은바꾸지않았다. 삼성엔지니어링→삼성E&A 과거이름2근거는추가검토필요. 명칭이거짓이라는뜻이아니며시점별법인동일성근거미확인이다.

실제GraphRAG검증 **`artifacts/kg_relation_contract_20260919/real_graphrag_smoke.json`**,실행스크립트와`GRAPHRAG_EXAMPLES.md`보존.
실제Qwen2.5-1.5B CUDA+기존CPU mDeBERTa실행. SUPPLY/COMPETE는GENERATED·verified_paraphrase, INVEST/PARTNER는검증실패후EXTRACTIVE_FALLBACK·정확한원문반환. 네유형모두원문인용·URL·날짜·과거cutoff·대칭관계역순조회통과. raw생성도감사용으로보존했으며미검증생성을수용한설명으로표시하지않는다.
설명에는`source_kind=NEWS`, `disclosures_searched=false`가명시된다. 기존 GraphRAG는구GNN번들용이며새어댑터가이번KG를읽는다. 운영HTTP/BE/FE연결은하지않았다.

**공시 답변과남은범위**: 기존`lake_disclosures.py`는DART정기보고서최대주주표에서지분관계를추출하고SEC는공시–기업관측까지만구현. `relationship_score_components.py`에출처별7D/30D/90D 뉴스·공시점수계산코드는있다. 하지만이번새뉴스KG에공시자료를병합하거나점수를재집계하지않았다. GraphRAG가공시까지검색했다고말하면안된다.
관계25개는아직적고보수적인문법/기초귀속게이트의재현율제한이남는다. `graph_quality_validated/ready_for_gnn_training/production_approved=false`유지. 관계품질보완·공시병합·점수재집계·GNN재학습·HDFS/서비스반영은완료범위가아니다.
기존pc1 STOP유지. 예약/커밋/push/MR 없음. 실제GPU smoke종료후GPU0%/0MiB 확인.

---

**2026-09-19 02:24 KST 최신 상태 — 로컬 배치 및 파일·감성 이력 검증 완료. 사용자 요구는 관계4종+기업별감성3종+GraphRAG 근거 설명까지이며 기능 완성은 아님.**
사용자 "다 됐나?"에 현재 상태를 확인했다. 전체 로컬 배치는01:35:41 KST `stage=complete,artifact_complete=true,graph_materialized=true`로 끝났다.
런처PID38472 종료, GPU0%/10MiB, stderr에는 모델 로딩 로그만 있다. 별도 재개나 GPU 작업을 시작하지 않았다.
입력은 부분 병합 채택 후보666,282쌍/483,738기사. grounding candidate_ready246,264/review419,901/exclude117.
최종 graph 뉴스 **219,790건**, 기업192개, 뉴스–기업 연결246,264쌍이다. 입력 기사483,738건이 모두 그래프에 들어간 것이 아니다.
감성246,264쌍 처리 완료: **analyzed174,680**, 귀속혼합56,259/상반감성15,267/길이초과10/언어미지원48은 label=NULL(합계71,584).
관계 근거17행(PARTNER9/SUPPLY8), source-target-type-directed 집계12행, 관계 검토12,556기사. 추출량이 매우 적으므로 기업 관계망이 충분하거나 GNN 학습 준비가 끝났다고 말하지 않는다.
최종 산출물: `artifacts/kg_grounded_full_20260918_v1/manifest.json`의7개 파일만 소비한다. 경로 `exports/attempt_408b487e4d784fca871d744b8367868a/`.
manifest.finished_at=2026-09-18T16:35:34.945082+00:00, 출력 DB SHA `99d526002168b6cf4936e625832b6653ccf757324ed6ff0b616fe7b293b17ec6`, protocol SHA `272ba071a318ba36efeb5ff0597955f70130797169e5509a3e35cd47bbd14be3`.
읽기 전용 최종검증 `final_verify_20260919.py`로 고정 코드·사전, manifest 계약, receipt2개, DB 해시, 출력7종의 SHA·bytes·행 수, SQLite/참조/감성NULL 검증 PASS를 확인했다. 보고서 `final_verification_20260919.json`.
`final_audit_provenance.json` 전수 감사 PASS: v1=1,536/v1.1=244,728, 허용 provenance·2단계 이관 연결 전부 일치. 감성 **긍정95,545/부정23,644/중립55,491**, 보류71,584는label/score/probabilities=NULL. 근거 문맥492,722개 입력SHA·확률·라벨 정합성 불일치0. 감성 정확도 검증은 아니다.
원래 pc1 STOP=true 유지, 미처리142,361쌍은 이 작업에 포함되지 않았다. 기존 B 선별/원문/모델을 바꾸지 않았다.
**로컬 산출물 생성 완료**와 **품질 승인·GNN 재학습·HDFS/서비스 DB 반영**을 구별한다. graph_quality_validated/ready_for_gnn_training/production_approved는 모두false 유지. 후자는 실행하지 않았다.

사용자 재확인 요구사항:
- 서비스 관계 종류는 정확히 **SUPPLY(공급), INVEST(투자·지분), PARTNER(협력), COMPETE(경쟁)** 4개. frontend/src/lib/meta.ts도 같은 계약이다. 현재 내부추출기는 ACQUIRE를 별도5번째로 다뤄 정규화 정책이 필요하지만, 완성한 현 산출물에는 ACQUIRE행은0이다. 없는 관계를4개 종류에 억지로 채우지 않는다.
- 뉴스–기업별 결과는 **POSITIVE/NEGATIVE/NEUTRAL** 3종이다. 같은 뉴스라도 기업별로 다를 수 있다. 보류는4번째 감성도 중립도 아니며 별도status와NULL로 구분한다. 현재 FinBERT 출력은 기업 문맥 감성이고 실제 주가영향/GNN예측과 동일하지 않다.
- 관계마다 **기여 뉴스·원문 인용·출처URL·날짜 + GraphRAG 검색/설명**까지 제공해야 한다. 근거JSON 저장만으로 이 기능이 완료됐다고 말하지 않는다.
- 기존 `news_graphrag.py`의GraphExplainer/`POST /v1/impact/explain`은 기존 모델 graph/archive를 이용한다. 이번 새 KG산출물의 relation evidence를 기존 GraphRAG에 넣는 연결은 아직 없다. 새 관계4종 재현율·법인귀속 보완과 새데이터 검색연결/설명 검증이 남았다.

---

**2026-09-19 00:56 KST 최신 상태 — 관계 전체 완료, 감성 원문 검증 오류 수정 후 GPU 정상 진행 확인.**
정제·근거 검사666,282쌍과 관계 검사483,738기사 완료. candidate_ready246,264/review419,901/exclude117.
관계 근거17행(PARTNER9/SUPPLY8), 관계 검토12,556기사. 17은 고유 기업 쌍 수가 아니며 엄격한 규칙의 낮은 재현율이 남아 있다.
00:42 재개 실행은00:45:01 감성1,536쌍 저장 후 종료했다(analyzed1,096/귀속혼합350/상반감성89/언어미지원1).
원인: FAST 기사 제목 `Fastenal’s Q4 Earnings Call: Our Top 5 Analyst Questions`를 근거 인용 검증에서 본문처럼 따로 정제해 정상 제목을 거절했다.
`kg_target_sentiment.py` v1.1은 좌표가 있는 근거를 **전체 기사 정제 재현·정제문 정확한 위치·원문 위치 매핑**으로 검증한다. 좌표 없는 구형 입력의 기존 검사는 유지한다.
모델 가중치·토크나이저·실제 입력 생성·확률 추론·분류 규칙은 그대로다. 모델 확률은 정확도가 아니고 감성은 주가 영향 예측이 아니다.
CPU 감성17개 PASS(3개 추가), 기존1,536쌍의 입력·문맥·언어/귀속 동일, 다음8,000쌍 pure_prepare 예외0. 원래 실패했던 다음256쌍도 전부 통과.
source SHA `3466a25a1d8a1a3ee1086a3cd246aaff2762f54766ecda95a29d7d522f201412`→`5c7a3bddbe4d956a8bd8a81e620a192ac93e0c5de33c54f6dc099cd307f4b9d2`.
유지보수 **`artifacts/kg_grounded_full_20260918_v1/maintenance/sentiment_span_20260919/`**에 원본 코드·전후 protocol·체크포인트·patch·receipt·seal을 보존했다.
이관은 감성 검증 코드/검증 provenance와 유지보수 receipt만 허용했고, 기존 예측1,536행·모든 커서·원본·모델은 변경하지 않았다. 이전 행의 v1 provenance를 보존한다.
현재 protocol SHA **`272ba071a318ba36efeb5ff0597955f70130797169e5509a3e35cd47bbd14be3`**, 유지보수 receipt2개. 실행 중 고정 코드/receipt를 수정하지 않는다.
00:53:33 숨김 백그라운드 재개 **런처PID38472**, 로그 `artifacts/kg_grounded_full_20260918_v1.20260919_005333.{stdout,stderr}.log`.
초기 원본 SHA/체크포인트 검사는 약2분 걸린다. 00:55:42 실행 재개 로그를 확인했고 **00:56:21 감성5,376/246,264쌍** 커밋을 확인했다. 이전 실패 지점(rowid4,499)을 지나 sentiment_cursor14,702까지 진행했다.
감성 상태 analyzed3,855/귀속혼합1,191/상반감성326/길이초과1/언어미지원3. GPU94%·1,672MiB, 런처PID38472 유지, stderr는 모델 로딩 로그뿐이다. 아직 최종 manifest는 없다.
읽기 전용 후속 감사: 현재 protocol 및 maintenance2개 SHA/연쇄 일치, 기존1,536행 v1 provenance는 archived protocol과 일치한다. exporter가 행별 감성 payload를 그대로 보존하고 버전 단일성을 요구하지 않아 v1/v1.1 혼합은 허용된다. 완료 후 각 provenance 건수·허용 목록 일치도 별도로 확인한다. export의 WAL checkpoint 동안 오래 읽기 트랜잭션을 열어두지 않는다.
상태는 `read_grounded_kg_progress.py`의 읽기 전용 SQLite 조회와 PID/로그로 확인한다. 실행 중 status.json 직접 읽기 금지.
다음: 전체 감성·내보내기·manifest와 행별 provenance 검증. 원래 B pc1 STOP 유지; GNN/HDFS/서비스DB/예약/commit/push/MR 없음.

---

**2026-09-19 00:42 KST 최신 상태 — grounding 완료, SQL 조회 병목 및 Windows 파일 잠금 재시도 보완 후 재개.**
사용자 "다 됐나?"에 상태를 확인했다. 정제/근거 검사는09-18 23:04:55에 **666,282쌍 전부 완료**했다.
결과 candidate_ready246,264 / review419,901 / exclude117. 이후 관계 단계가 기사별 `pairs_decision(decision=?)` 인덱스를 선택해 매번246,264행을 반복 검색하면서 지연됐다.
00:25:27 관계검사7,936기사, GPU0, 감성0이었다. 아직 전체 완료가 아니었고 사용자에게 이를 알렸다.
00:27:43 **새 KG 출력의 STOP만** 생성했고00:28:15에8,192기사 저장 후 paused_stop_file/프로세스 종료를 확인했다.

새 유지보수 도구 `optimize_grounded_kg_queries.py`로 **새 KG 출력 SQLite에만 `ANALYZE pairs; ANALYZE articles;`** 실행했다.
고정된 builder/모델/protocol/원본 merged DB는 변경하지 않았다. 기존 인덱스 통계만 생성했으며 새 인덱스 추가도 없다.
새 연결의 실행 계획은 `sqlite_autoindex_pairs_2(article_id=?)`로 바뀌었다. (_1은 source_rowid UNIQUE다.)
6개 실제 기사 조회 합계3.945703초→0.000385초, payload multiset 동일. meta/테이블별 건수 동일, quick_check 및 cursor/참조/카운터 검증 통과.
영수증 **`artifacts/kg_grounded_full_20260918_v1/query_optimization_20260919.json`**에 변경 전후 DB SHA·계획·조회 결과 hash·시간과 통계를 보존했다.
진행 중 출력 DB는 아직 최종 SHA가 고정되지 않았으므로 통계 보완이 기존 재개 계약과 충돌하지 않는다. 완료 manifest가 생긴 DB나 원본 merged DB에는 이 작업을 적용하지 않는다.

00:31:06 `start_grounded_kg.ps1 -Resume`로 **같은 출력 폴더**를 숨김 백그라운드 재개했다. 런처 PID27924.
로그 `artifacts/kg_grounded_full_20260918_v1.20260919_003106.{stdout,stderr}.log`.
재개 후 실제256기사 처리2분35초→약0.04초,22,016기사까지 진행/SUPPLY근거1개 확인.
00:33:17 상태파일 Get-Content 조회와 원자적 os.replace가 겹쳐 WinError5로 종료했다. 마지막22,016기사 커밋은 유지됐다.
00:34:09 동일명령으로 다시 재개(런처PID31184), 로그 `artifacts/kg_grounded_full_20260918_v1.20260919_003409.{stdout,stderr}.log`.
00:36:25 재개 실제 처리 확인: relation_articles85,504/483,738(로그는86,272까지), SUPPLY근거2개, 오류없음/PID유지. 256기사약0.04초로개선확인. 감성은아직0.
**실행 중 status.json을 Get-Content/open으로 직접 읽지 말 것.** 새 `read_grounded_kg_progress.py`는 SQLite read-only WAL snapshot만 사용한다. stdout/stderr tail과 런처PID로 생존도 확인한다.
00:37:11 직접 status 읽기를 제거했는데도 같은 WinError5가 재발해445,696기사 커밋 후 종료했다. 따라서 Get-Content만이 원인이라고 단정하지 않는다.
이 시점 관계근거17개(SUPPLY8/PARTNER9), relation_reviews12,478기사, 감성0. 데이터·커서는 그대로 보존했다.

**예외적으로 비의미적 실행 코드 변경을 명시적으로 감사·기록하고 프로토콜을 이관했다.**
`atomic()`에 일시적인 PermissionError(errno13/Windows5,32,33) 최대5초 재시도(20ms→200ms backoff)를 추가했다. 기타 오류는 즉시 실패, 영구잠금은 기존 완성 파일을 보존하고 실패한다.
실행 이력 receipt 목록·SHA를 protocol/최종 manifest에 포함하도록 보완했다. 모델 입력·출력, ground/prepare/relations/sentiments 등 처리 함수 AST는 이전과 동일함을 확인했다.
유지보수 경로 **`artifacts/kg_grounded_full_20260918_v1/maintenance/atomic_retry_20260919/`**:
원본builder/protocol, 전후protocol, checkpoint.before.json, patch, apply_migration.py, receipt.json, seal.json 보관.
원래protocol SHA `e6a4b7cb96ffb844f1917eb433589301ff81374f2537c2648b433fbe749ea9e2`, 새protocol SHA `f36db36bd4b6fe84cc6700ee251ab3eb638a66c3956591d507dd307230c1ebba`.
builder SHA `60df5471166267ca73b542a579a11147cc3906bd20249eb0d6ac83d806937df9`→`045ba047e1c453cd1aa341a4b418380d261c0a9fb7956467543e7c84267a0c71`.
모델/사전/분석 정책/원본/체크포인트 변경 없음; 실행기 바인딩1개와 maintenance receipt 추가만 허용했다. 최초 이관 검사에서 자기 유지보수 스크립트가 __main__ dependency에 포함되어 중단됐고, 실제 builder가 import하지 않는 해당 스크립트만 receipt에 별도로 기록한 뒤 이관했다. 검사 실패 시 데이터/프로토콜은 변경되지 않았다.
실행기CPU28개 PASS(기존25+파일잠금3). 전체 관련 검사 총91개 PASS. 이전 결과의 생성 코드 이력은 지우지 않고 최종manifest.execution_maintenance로 전달한다.
00:42:32 재개: 런처PID19976, 로그 `artifacts/kg_grounded_full_20260918_v1.20260919_004232.{stdout,stderr}.log`.
관계 커서445,696부터 이어가며 전체48만3,738기사 관계 검사가 끝나면24만6,264쌍 감성을 처리한다. 현재 실행 중인 새 builder도 다시 수정하지 않는다.
정제/근거 666,282쌍은 재실행하지 않고 관계 커서8,192부터 이어간다. 모델·관계 판단 정책은 그대로다.
다음: 재개 후 감성 진행 확인, 완료 manifest/산출물 검증. 새로 처음 실행하는 후속 builder 버전에는 prepare 종료 후 ANALYZE를 명시적으로 넣어야 한다. **현재 실행 중인 고정 builder는 수정하지 않는다.**
GNN 재학습/HDFS·서비스 DB/예약·push·MR 없음. 기존 pc1 선별 STOP는 유지.

---

**2026-09-18 22:49 KST 최신 상태 — 로컬 근거 지식그래프 전체 배치 실행 중.**
사용자 "오케이 ㄱㄱ"로 정제/귀속 보완 → 뉴스–기업 근거·기업 문맥 감성 → 기업–기업 관계·근거 생성을 승인했다.
범위는 아래 부분 병합의 **666,282쌍/483,738기사**다. 원래 pc1 선별 재개, GNN 재학습, HDFS/서비스 DB 게시, 예약 작업, commit/push/MR는 실행하지 않는다.

신규 코드: `build_grounded_kg.py`, `kg_grounded_news.py`, `kg_grounded_relations.py`, `kg_target_sentiment.py`.
설명/실행기: `README_GROUNDED_KG.md`, `start_grounded_kg.ps1`.
첫 실제 1,024쌍 파일럿 `artifacts/kg_grounded_pilot_20260918_v1`은 완료했다(초기 코드 버전이므로 최종 코드 실측과 구분).
초기 grounding ready361/review663, 감성 analyzed262/기업 문맥 혼합75/상반된 감성24. 관계0건, 표본의 고유기사는1,023건이었다.
파일럿 완료 이후 수정된 코드로 원래 파일럿을 `--resume`하면 코드 고정 검사가 거부하는 것이 정상이다. 최종 코드는 새 파일럿 경로로 검증한다.
Windows sandbox의 multiprocessing Pipe가 WinError5로 막혀, 동일 로컬 명령의 샌드박스 밖 실행 승인을 받아 파일럿을 마쳤다. 네트워크/원격 변경은 없다.

기존 192개 AI 개발 표본으로 grounding 보완: `artifacts/kg_grounding_audit_20260918_v1/v2/v3`.
최종 v2_1(소스 SHA `add2b02be1af25561368c6cea8a2d602df3a421f0e5d678726ab1a49ad30327d`)은63ready/129review.
오리온 공장 지명, CEO 소속, 그룹/별도법인, 과거 경력 등 오귀속 5건을 보류로 수정하고 감원·투자·운항 사고·협약·주문량 사건 누락을 보완했다.
이는 노출된 개발 자료에 대한 동작 확인이지 인간 정답·정확도·모집단 정밀도 검증이 아니다. 기존 B 판정과 품질 게이트를 바꾸지 않는다.

복수기업 실제 기사200개/528쌍 probe는 `artifacts/kg_relation_probe_20260918_v1`.
기존 관계 문법은 B include 전체에서도0건, ready교집합에서도0건이었다. 5개 원문 검토에서 명시 협력 문장의 미지원과 올바른 보류(제3자 공동투자/계열사/대상 외 회사)를 구별했다.
현재 관계 문법의 한정된 확장을 검사 중이다. 모듈 인식과 실제 ready양끝 게이트의 결과를 분리해야 한다. 없는 관계를 숫자 채우려고 만들지 않는다.

실행기는 원문/모델/코드/사전/런타임을 고정하고 SQLite 묶음별 원자적 커밋, STOP, 재개, 완료 직전 장애 복구, 출력 해시 검사를 구현했다.
중단·재개/변조 거부 CPU25개, grounding18개, 감성14개, 관계31개로 **총88개 PASS**.
관계 최종 정책 `explicit-two-party-clause-2`, 모듈 SHA `cdb880cebf8ede008f54d460ae06684cd26a88b920058a0ffb5f15c670f1c3b1`.
`artifacts/kg_relation_probe_20260918_v2`에서 B 전체 endpoint 기준 명시 관계2개를 찾았다. 한화솔루션/고려아연은 grounding review이므로 ready양끝 게이트를 적용한 실제 그래프 관계는0개다. 억지로 승격하지 않았다.
최종 실제 파일럿 `artifacts/kg_grounded_pilot_20260918_v2`는22:47:56 완료(exit0): 1,024쌍 중ready382/review642, 감성analyzed275/귀속혼합80/상반감성27.
원문 근거819개 substring 검증, 파일7종 해시·건수, SQLite 커서·참조 검증 PASS. 고유기사1,023개 중 그래프 뉴스382개, 뉴스–기업 연결382개. 관계0개로 관계 재현율 보장은 하지 않는다.

전체 배치: **`artifacts/kg_grounded_full_20260918_v1`**,22:48:31 KST 숨김 백그라운드 시작.
런처 PID35096(venv launcher), 실행정보 `artifacts/kg_grounded_full_20260918_v1.launcher.json`.
로그 `artifacts/kg_grounded_full_20260918_v1.20260918_224831.{stdout,stderr}.log`.
22:49:08 `state=running,stage=prepare,prepared_pairs=1792/666282`,ready660/review1132 정상 커밋 확인. 시작만 된 상태이며 전체 완료가 아니다.
22:50:17 추가 확인: **48,128/666,282쌍 정제·근거 검사 저장**,ready17,628/review30,493/exclude7. stderr 오류 없음, 백그라운드 프로세스 유지. 전체 관계·감성 단계는 아직 시작 전이다.
최신 상태는 출력 `status.json`과 SQLite `meta.stage`를 확인한다. 완료는 `manifest.json`의artifact_complete와 출력 hash/건수까지 검증해야 한다.
원래 pc1 `paused_stop_file`/734,970쌍/STOP=true 불변 확인. 새 KG 출력에만 STOP를 만든다.
중단: 새출력/STOP 생성 → 현재 묶음 커밋 후 paused_stop_file 확인. 재개: `powershell -NoProfile -ExecutionPolicy Bypass -File AI/graph/start_grounded_kg.ps1 -Resume`.
코드/사전/모델/설정 변경 시 이어서 실행하지 못하도록 검사한다. 작업 중 새 모듈을 수정하지 말 것. 기존 예약 작업은 재생성하지 않았다.
다음: 전체 배치 완료·근거/NULL/참조/무결성 확인 → 실제 생성 건수와 보류 범위 보고. GNN 학습/HDFS/서비스 배포 완료라고 말하지 않는다.

---

**2026-09-18 17:41 KST 최신 상태 — 요청한 1·2번(저장분 부분 병합·무결성 검사, 분포·표본 품질 감사) 완료. pc1 중단 유지.**
사용자 "ㄱㄱ 빠르게" 요청으로 base+중단 pc1+완료 pc2의 저장분만 별도 부분 산출물로 정리했다.
`merge_kg_provisional_partial.py`가 원문 입력 해시·offset·행 길이·기존 예측 해석·중복·연속 범위·원본/코드 불변을 검사했고,
17:32:32 정상 종료(exit0), `status.stage=partial_artifact_complete`와 `partial_receipt.json` 일치를 확인했다.
병합 **2,514,445쌍(전체2,656,806의94.64%)**, 고유 기사 **1,446,017건**.
**채택 후보666,282쌍 = 고유 기사483,738건**, 제외796,791쌍, 보류1,051,372쌍.
미처리 **142,361쌍**은 `coverage.json`의146개 suffix 구간으로 보존했다. 별도로 원문 바이트·행을 읽은 누락 감사와 일치한다.
미처리 집합 내부 고유134,677기사는 처리 집합과 겹칠 수 있으므로 전부 신규/미처리 기사라고 부르지 않는다.

주 산출물: `artifacts/kg_b_partial_merged_20260918_v1/`.
`state.sqlite3`는 원문+판정 조회용이며 `exports/include_with_source/`는 제목·본문·발행일·URL·기업·원본 SHA·B 판정을 함께 담는다.
부분 완료만 뜻하므로 `complete=false`, `full_population_complete=false`, `graph_ready=false`, `human_gold=false`, `production_approved=false` 유지.
기존 전체 병합기를 pc1 미완료 상태로 실행하거나 status를 조작하지 않는다. 새 부분 병합 시도는 새 출력 경로를 써야 한다.
수신/후속 작업은 완료 영수증 존재만 보지 말고 **status와 영수증을 함께 확인**한다.

감사: `artifacts/kg_b_partial_audit_20260918_v1/`, 종합 문서 **`README_KG_B_PARTIAL_AUDIT.md`**.
`distribution.json`, `sample_review_final.jsonl`, `sample_review_summary.json`, `audit_completion_receipt.json` 저장 완료(17:39).
고정 해시 표본192쌍을 64쌍씩3묶음으로 AI 보조 검수했다. 초기 검토는 에이전트2개가128/64쌍을 담당했고 root가 경계 사례를 조정했다.
최종 **채택 가능144/제외 권고33/판단보류15**. 인용 원문 일치·입력 SHA·최종 병합 DB 연결192건 전부 확인.
144/192=75%는 AI 표본 검토 비중이며 **전체 정확도·정밀도·F1·인간 정답·GNN 개선 수치가 아니다**. B 기존 품질 게이트 미통과 상태는 유지한다.
일반명사(cadence/메타), 지명(오리온), 화폐·인증 표현(한화로/GS), 등락 목록·소속·약력 오탐을 확인했다.
그룹/지주회사·과거 사명 귀속은 추가 확인 대상. 원래 B 라벨/검토 파일은 보존하고 표본 조정5건만 별도 기록했다.
채택 후보 중 차트 UI 문구42,061쌍·본문 없음81쌍·1970년 기록124쌍을 확인했다. UI 문구만으로 미래 주가 누설을 단정하지 않는다.
기업 사전 시장별 채택은 KOSPI571,409/NASDAQ94,873쌍. 상위10기업이49.57%를 차지한다.
**기업 시장과 뉴스 출처 국내외는 다른 분류**다. 로컬 원문 입력에는 출처 지역이 없으므로 대체 비교하지 않는다.
누락 감사 에이전트가 한때 서로 다른 분모/분류를 비교한 메시지는 철회했고 `missing_distribution_interpretation.json`에 정정했다.
동일한 기업시장 기준 처리 집합 NASDAQ35.83%/미처리35.45%이며, 이 비교만으로 무작위성·모델 편향을 입증하지 않는다.

검증: 부분 병합 테스트5개 통과, 분포 계산 합성 자료·날짜/길이/문자 규칙·실패 status 거부 확인, 실제 전체 저장분 분포 분모 일치.
신규 코드 `merge_kg_provisional_partial.py`, `audit_kg_partial_distribution.py`; 재현/후속 검사 명령은 종합 문서와 감사 폴더 스크립트 참조.
다음에는 확인된 오탐/날짜/본문 정제와 법인 귀속을 반영한 뒤 뉴스–기업 근거, 감성, 기업–기업 관계를 만든다.
같은 기사에 함께 채택된 기업이라는 이유만으로 기업–기업 관계를 생성하지 않는다. GNN 주가 라벨·시간 분할·과거 그래프는 별도 단계다.
pc1 `paused_stop_file`/734,970쌍/STOP 불변, nvidia-smi 계산 프로세스 없음 확인. 명시 재개 없이 잔여 선별을 다시 시작하지 않는다.
원본·B 모델 보존. 예약·KG/감성/GNN 실행·HDFS/서비스DB 게시·commit/push/MR 없음.

---

**2026-09-18 16:40 KST 최신 상태 — 추가 노트북 pc2 완료 결과 수신·내부 무결성 검증 PASS. pc1은 중단 유지.**
사용자가 `C:/Users/SSAFY/Downloads/output.zip`을 전달했다. 455,422,689바이트 ZIP을 안전한 신규 임시 폴더로 풀고
검증 통과 후 `artifacts/kg_b_parallel_20260918_v1/pc2/output`에 보관했다. 기존 입력·모델·pc1 결과는 덮어쓰지 않았다.
ZIP SHA256 `0d80fd0f862a5ca993f125b5406aa34b65d5ff3e385aa831ee9afd6a3215c21c`.
891 ZIP 항목(파일886개), 해제크기5,309,813,246바이트, 모든 파일 ZIP CRC 및 추출 SHA 기록.
pc2 **877,175/877,175쌍 완료**, include232,938/exclude278,461/review365,776, 관측 기사674,601건.
pc2 분모·split manifest·기존 B 모델/코드/입력 protocol 일치, status/receipt 일치,
SQLite 전체 row/chunk/cursor 체크섬 및 export 전체 SHA·877,175행↔DB 일치·export pair 중복0 확인.
모델 추론이나 재학습 없이 약105초 CPU 검사. 이 PASS는 수신 파일 내부 무결성이며 선별 정확도·운영 승인·원문 재검증 완료가 아니다.
감사기록 `artifacts/kg_b_parallel_20260918_v1/pc2_received_audit_20260918.json`,
파일목록 `pc2_received_zip_inventory_20260918.json`, 실행스크립트 `receive_pc2_20260918.py`.
pc1 status/STOP 해시 불변, **734,970쌍 저장·142,361쌍 미처리** 그대로다. 명시 재개 요청 없이 시작하지 않는다.
base902,300 + pc1저장734,970 + pc2완료877,175 = 보관 결과2,514,445쌍(전체2,656,806쌍 중94.6%).
이 합계는 구성별 기록 합계이며 전체 병합·원문 offset 대조·최종 전체 완료 영수증은 아직 없다.
기존 전체 병합기는 두 worker 완료를 요구한다. pc1 미완료 상태에서 실행하거나 완료 상태를 조작하지 않는다.
부분 자료 활용 요청이 오면 별도 부분 산출물·누락 범위를 명시하는 경로로 처리한다.
pc1 재개·예약 알림·KG/감성/GNN·HDFS/DB 게시·commit/push/MR 없음.

---

**2026-09-18 16:29 KST 최신 상태 — 사용자 요청으로 pc1 정상 중단·저장, pc2 결과 수신 대기.**
사용자가 "지금 여기까지 하고, 옆 노트북은 다 끝났다. 무엇을 가져오나"라고 요청했다.
16:29:04 `pc1/output/STOP` 생성 후 현재 묶음 저장·정상 종료를 확인했다. 새 재개 요청 없이 자동 실행하지 않는다.
16:29:06 `paused_stop_file`, **734,970/877,331쌍 저장**, include194,383/exclude232,614/review307,973, 오류 없음.
pc1 미처리는 **142,361쌍**. 16:29:21 기존 PID4144/6524/17100/30152 모두 종료, GPU0%/0MiB 확인.
읽기 전용 SQLite 검사에서 pairs 행수=chunks 합계=files 소비행수=status=734,970, 완료파일742/전체888 일치.
정상 종료로 WAL/SHM은 사라졌고 DB 본체가 저장됐다. 기존 base902,300쌍·모델·원본·pc2 패키지는 보존했다.
중단 영수증: `artifacts/kg_b_parallel_20260918_v1/pc1_pause_receipt_20260918_1629.json`.
pc2 완료는 현재 사용자 보고만 있으며 파일 미수신·완료 검증 전이다. 추가 PC의 실행 폴더 안 `output` 전체를 받아
`artifacts/kg_b_parallel_20260918_v1/pc2/output`에 보관한다. state.sqlite3/protocol/status/provisional_receipt/exports 및 남은 부속 파일 전부 필요.
상대 PC의 프로세스 종료 및 status `provisional_complete`, complete=true, 877,175/877,175를 확인한 뒤 복사한다.
pc1이 부분 완료이므로 기존 `merge_kg_provisional_partitions.py`의 전체 완료 병합을 실행하지 않는다.
pc2 수신 검증과 부분 자료 활용은 전체 완료와 구분한다. 완전 병합에는 pc1 잔여142,361쌍 처리가 필요하다.
명시 재개 요청이 있으면 `start_kg_b_parallel_pc1.ps1 -ResumeAfterStop`으로 이어간다. 예약·KG·감성·GNN·HDFS 게시 없음.

---

**2026-09-18 11:04 KST 최신 상태 — pc1 중단 확인 후 저장 지점부터 재개.**
사용자가 "원래 하던 작업 멈춘 것 같다"고 알려 확인했다. 11:03 관측 시 기존 PID는 모두 없고 GPU 0MiB였다.
마지막 저장은 10:55:42 KST, 167,816/877,331쌍. status의 running 값은 남아 있었지만 실제 프로세스는 종료돼 있었다.
기존 stderr는 비었고 정상 종료·Python 오류 기록도 없어 종료 원인은 확인되지 않았다. STOP 파일은 없었다.
11:03:46 `start_kg_b_parallel_pc1.ps1`로 같은 pc1 배정분만 재개했다. 기존 SQLite/WAL·입력·모델은 보존했다.
새 launcher PID4144 / bootstrap6524, worker wrapper30152 / 실제17100; 실행 세션61678을 임의 종료하지 않는다.
11:04:48 실제 **168,880/877,331쌍**, include44,773/exclude53,479/review70,628, 오류 없음·GPU69%/718MiB 확인.
재개 전 167,816쌍에서 실제 저장 건수가 증가했다. 이 수치는 시점 관측이며 다음 확인에도 PID·상태 갱신·GPU를 함께 볼 것.
영수증·로그: `artifacts/kg_b_parallel_20260918_v1/launches/pc1_20260918_110346_509.*`.
worker 로그: 같은 루트 `pc1/logs/worker_20260918_110400.log`. 기존 base902,300쌍과 pc2 패키지는 그대로다.
추가 노트북의 실행·진행 상황은 확인하지 않았다. 예약 실행·알림 없이 B 잠정 선별만 계속한다.

---

**2026-09-18 09:28 KST 최신 상태 — 사용자 요청으로 현재 노트북 pc1 배정분 백그라운드 재개.**
사용자가 "이 노트북에서도 하던 거 다시"라고 명시 요청했다. `start_kg_b_parallel_pc1.ps1`로 pc1만 시작했다.
기존 저장 128쌍 뒤부터 재개. 무제한 배정분은 877,331쌍이며 원본 전체 입력 작업과 pc2는 실행하지 않았다.
09:28:07 숨김 launcher PID38424, bootstrap 실제 Python17580; 09:28:21 worker wrapper34444/실제25808 관측.
09:28:46 실제 `running` **584/877,331쌍**, include151/exclude178/review255, 오류 없음, GPU71%/718MiB 확인.
진행은 특정 시점 관측이므로 다음 세션은 status·새 로그·실제 프로세스로 생존 여부를 다시 확인할 것.
실행 세션5491은 배경 프로세스 때문에 유지될 수 있다. 임의 종료하지 않는다.
실행 영수증·stdout/stderr: `artifacts/kg_b_parallel_20260918_v1/launches/pc1_20260918_092807_515.*`.
worker 로그: 같은 루트 `pc1/logs/worker_20260918_092821.log`; 상태·SQLite: `pc1/output`.
원본 `kg_b_provisional_full_20260918_v1`은 STOP/902,300쌍 그대로다. pc2 전달 패키지도 변경하지 않았다.
재개에는 새 `start_kg_b_parallel_pc1.ps1`을 사용하며, 살아 있는 launcher가 있으면 중복 실행을 거부한다.
정상 중단은 `pc1/output/STOP` 생성. 이후 `-ResumeAfterStop`으로 명시 재개한다.
예약 작업·알림·자동 재부팅 재개는 등록하지 않았다. 잠정 선별만 실행하며 KG/감성/GNN/게시/push/MR 없음.

---

**2026-09-18 08:51 KST 최신 상태 — 두 노트북 병렬 선별 패키지 준비·실제 GPU 재개 확인 완료.**
사용자가 같은 GPU의 추가 노트북으로 병렬 처리하고 싶다며 한 번에 실행할 `sh` 파일을 요청했다.
`artifacts/kg_b_parallel_20260918_v1`에 기존 902,300쌍의 일관된 SQLite 백업과 남은 두 입력을 만들었다.
원본 입력·B 모델·고정 실행 코드·기존 결과·STOP을 보존했다. 새 학습이나 LLM 재판정은 없다.
배정은 **pc1 877,331쌍 / pc2 877,175쌍**, 기존 완료분과 합하면 정확히 2,656,806쌍이다.
완료 파일을 제외하고 부분 파일은 저장된 바이트 이후만 복사했다. 분할 계획에 원본 위치 매핑을 저장했다.
계획 SHA `f8c29e5e14db3fb578e0831e2e435490dee7fd68a0063ab949251dacbc91d199`.

**아래 과거 기록의 `start_kg_b_provisional.ps1`을 두 분할 작업과 함께 실행하지 말 것. 남은 입력이 중복된다.**
이제 각 PC는 자신의 `pc1` 또는 `pc2` 패키지만 실행한다. 상세 명령은 `README_KG_B_PARALLEL.md`.
추가 노트북 복사용 `artifacts/kg_b_parallel_20260918_v1/pc2_worker.zip`은 1,979,949,850B(약 1.98GB).
ZIP SHA `3d6a55f35a03e2a2cb7c455d97f4e38f22788d87b53c38c993df51b2c3d46860`.
압축 안의 928개 파일 전부 원본 manifest SHA 일치 확인(`pc2_archive_verified.json`).
추가 PC 최종 경로에 압축을 풀고 `bash run.sh` 또는 Windows `run.ps1`로 실행한다.
Python 3.12와 NVIDIA 드라이버는 사전 필요하며 라이브러리는 첫 실행 때 독립 `.venv`에 설치한다.
추가 노트북에 전송하거나 그곳에서 설치·실행한 것은 아니다. 해당 노트북 OS 답변도 아직 없다.

현재 PC의 새 pc1 패키지에서 Git Bash `run.sh`로 GPU 64쌍 처리·종료 후 PowerShell `run.ps1`로 다음 64쌍 재개했다.
**총 128쌍 / 4개 transaction**, 처음 64개 결과 SHA 불변·기존 완료분과 ID 중복 0·128개 모두 실제 B 확률 출력 확인.
pc1은 `paused_limit`(128/877,331), include33/exclude39/review56. pc2 시작 전. 무제한 본 배치는 시작하지 않았다.
08:50:54 GPU 0%/0MiB, 두 실행 exit0. 원래 전체 작업은 계속 STOP/902,300 유지.
Git Bash는 도구 sandbox 신호 파이프 제한으로 최초 실행 불가였고 승인된 일반 권한 실행에서 확인했다.
새 PC의 신규 pip 설치는 미검증이며 현재 PC의 기존 가상환경을 `--skip-install`로 재사용했다.
근거: `portable_resume_verified.json`, 각 `logs`, `pc1_package_receipt.json`, `pc2_package_receipt.json`.

현재 PC 배정분 시작(저장소 루트):

```powershell
& .\AI\ner\.venv\Scripts\python.exe -X utf8 -u .\AI\graph\artifacts\kg_b_parallel_20260918_v1\pc1\bootstrap.py --skip-install
```

재실행은 저장된 부분부터 이어간다. STOP을 만들었으면 `--resume-after-stop`이 추가로 필요하다.
두 작업 완료·프로세스 종료 후 pc2의 `output` 전체를 회수하고 `merge_kg_provisional_partitions.py`로 새 폴더에 병합한다.
분할 CPU 7검사·병합 CPU 6검사·실제 분할 계획 검사 통과. 실제 전체 병합은 아직 실행하지 않았다.
여전히 **provisional_candidate_only**이며 검증 통과 라벨·KG·감성·서비스 데이터로 격상하지 않았다.
예약 실행/알림/자동 재개/GNN 학습/서비스 게시/commit·push·MR 없음.

---

**2026-09-18 07:09 KST 최신 상태 — 사용자 요청으로 전체 B 선별 배치 정상 중단·저장 완료.**
사용자가 "지금 하던 작업 멈추고 컴퓨터 좀 껐다 킬게"라고 요청했다. 새 재개 요청 전 자동 실행하지 않는다.
출력폴더에 STOP을만들어현재묶음을커밋한뒤정상종료했다. STOP파일은유지한다.
07:09:45 `paused_stop_file`, 오류없음. 07:09:57 launcher37064/worker18496 없음·GPU0%/10MiB 확인, exec31514 exit0.
저장완료 **902,300 / 2,656,806쌍(약34.0%)**, 채택후보238,961 / 제외후보285,716 / 보류377,623.
입력2,688파일중912파일완료,다음파일부분처리cursor보존. DB실제행수·상태분모·소비행수·커서체크섬·protocolSHA 일치확인.
상세 영수증 `artifacts/kg_b_provisional_full_20260918_v1/pause_receipt_20260918_0709.json`.
이전 전체입력/모델/결과/고정코드를보존하며, 다시학습/다운로드하거나이미처리한902,300쌍을재판정하지않는다.
남은1,754,506쌍부터계속한다. 보류목록의LLM자동재판정이나KG생성은시작하지않았다.

사용자가 다시 이어가라고 하면 저장소 루트에서 다음 명령으로 STOP을 해제하고 재개한다.

```powershell
& .\AI\graph\start_kg_b_provisional.ps1 -ResumeAfterStop
```

실행전현재프로세스없음·paused_stop_file·STOP유지·동일protocol을확인한다. 재개시worker가입력/모델/코드/DB해시를재검사한다.
실행중인것처럼보이는아래00:32기록과기타옛상태를최신으로오해하지말것. 예약알림/자동재개등록없음.

**2026-09-18 00:32 KST 최신 상태 — 사용자 승인으로 기존 B의 전체 후보 GPU 잠정 선별 실행 중.**
사용자가 "gpu로 B로 이제 다음단계 드가자. 멈추더라도 다시 기억해서 할 수 있게"라고 명시적으로 요청했다.
이전 1번까지만 진행하던 범위를 이번 전체 후보의 잠정 분류(2번)로 확대했다. 아래의 과거 전체실행금지 기록은 이번 요청으로 대체한다.
기존 B가 자동 선별 검증을 통과한 것은 아니며, 실패 기록을 통과로 바꾸지 않았다. 새 `kg_provisional_bulk.py`만 사용한다.
결과는 세 확률 argmax의 잠정 채택/제외/보류이고, 동률·비정상점수·입력 미처리는 보류로 저장한다.
모든 원시 예측·원문SHA·파일/바이트참조를 유지한다. 제외 원문 삭제, LLM 재판정, 새학습, 서비스 게시 없음.

00:31:48 전체 처리 재개, launcher PID37064 / 실제 GPU worker PID18496.
00:32:25 실측 상태 running, 704/2,656,806쌍 저장(채택182/제외226/보류296), GPU40%/632MiB.
이는 특정시점 관측이다. 다음 세션은 현재 status, 실제 프로세스, 새 로그로 생존 여부를 다시 확인한다.
대상 1,498,312기사/2,656,806쌍, 입력2,688파일/10,068,121,511B. 전체후보는 이미 로컬에 있고 입력파일 전체SHA 검증통과.
모델은 `kg_cascade_student_v2b`, `artifacts/kg_cascade_v2_pairfix_20260917_v1/student_B/attempt_001` 그대로다.
모델manifest SHA `522208fa63344d897cc3038e1135c8595eb758a86abfb20e9124c7c3429c1c3d`.
소스manifest `artifacts/kg_cascade_v1_20260917_v1/transfer/source/manifest.json`, SHA `c8c82954b87e1dfdd592616747fc5a8ee2374694ae9b71dcd375719b3685200e`.
고정 실행protocol SHA `071d545cd4f1e281b6a7c1bdf12c4286e58670678a87dc5e93499118e61bc7fa`.

진행·결과: `artifacts/kg_b_provisional_full_20260918_v1/status.json`, `state.sqlite3`, `protocol.json`.
실행로그: `artifacts/kg_b_provisional_launch_20260918_v1/launch_20260918_003148_376.{json,stdout.log,stderr.log}`.
고정코드17파일을 출력폴더 `frozen_code/`에 복사해 해시 확인했다. 실행중/재개전 모델·소스·고정 Python 파일 수정금지.
SQLite FULL/WAL transaction은32쌍 결과와입력커서를함께저장. 전원종료/강제종료시미커밋묶음만다시처리한다.
CPU22검사통과(중간transaction실패rollback/재개/중복/변조감지/최종export/STOP 포함).
실GPU64쌍처리후프로세스종료→재개해다음64쌍처리: 총128·처음64결과SHA동일·누락/중복없음 확인.
실제전원차단시험은하지않았다. 증거는 `artifacts/kg_b_provisional_preflight_20260918_v1/resume_verified.json`.

재개 명령(저장소 루트, 기존 결과 재사용, 숨김 백그라운드):

```powershell
& .\AI\graph\start_kg_b_provisional.ps1
```

새모델학습/뉴스재다운로드부터시작하지않는다. 기존프로세스가살아있으면추가실행하지않는다(OS잠금도중복실행차단).
정상중단은 출력폴더에 `STOP` 파일 생성. 이요청으로중단했으면 `start_kg_b_provisional.ps1 -ResumeAfterStop`으로명시재개.
PC단순종료/절전은 STOP파일을만들지않으므로일반명령사용. SQLite/-wal/-shm 파일삭제금지.
최초64쌍속도33.13쌍/초로단순전체환산22.28시간, 704쌍시점38.43쌍/초. ETA는확정시간이아니며입출력/검증/내보내기별도.
사용자노트북전원설정확인: AC유휴절전없음/DC30분/덮개닫으면둘다절전. 충전기연결·덮개열고화면보호기/잠금은계속동작.
전원설정변경과예약알림은하지않았다. 완료뒤추가KG/감성/관계/점수/HDFS·DB게시/GNN/commit·push·MR 자동실행없음.
전체무라벨추론에는기존CAL/TEST소속기사도포함되지만정답/LLM판정은열지않고튜닝에사용하지않는다.
자세한운영설명은 `README_KG_B_PROVISIONAL_BATCH.md` 참고. 최종완료는 provisional_receipt와전체분모대조로판단한다.

**2026-09-18 00:04 KST 최신 완료 상태 — 41건 수정 확인과 기존 A/B 재사용 비교 종료, 둘 다 기준 미달.**
`artifacts/kg_cascade_v3_diagnosis_20260917_v1/reasoned_development/run_003`은 00:02:25에41/41완료.
v3 teacher 결과 include18/exclude20/review2/실행불가1, 추론537.0567662초, exec40960 exit0 확인.
00:04:27 GPU0%/10MiB 확인, 이 검사의 추론 프로세스는 종료됐다.
고정개발평가: 참고판정35/41, 합성16/16, 부적절채택1, 미종료출력1 → accepted=false.
37/41이상·합성전부·부적절채택0·실행실패2이하 기준을 낮추지 않았다. v2는36/41이었으므로 전체 개선 미입증.
스타벅스 한국운영사/미국법인 귀속혼동은 남았고, 현대차 협력생산·한전요금/적자·컴캐스트계약·PayPal CFO이동을 과도하게 제외했다.
원문/참고판정/프로토콜/출력/완료영수증의 해시와 v3 판정 재현 확인 후 development_report/challenge_receipt 저장.
이41건은 이미 노출된 AI·합성 개발 참고자료이며 모집단 정확도나 사람 정답 성능이 아니다.

`old_ab_equal_ensemble_dev` CPU비교도완료: 원B163/256·macroF1 .62637·logloss .833185재현;
고정0.5평균156/256·macroF1 .60957·logloss .823440. 모든 기존 routing임계값 기준미달, 비교종료.
동일원문/라벨/그룹/분모·원A/B routingJSON재현·원본SHA불변 확인, artifact receipt/report/script 보존.
원래 A/B 학습 결과는 모두 유지한다. 새학습모델완료/검증통과/전체뉴스적용가능이라고 말하지 않는다.
**이번 제한범위의 두 검사는 끝났다. 새 본 실험 freeze/1,792쌍 전량재판정/신규학생학습은 시작하지 않았다.**
CAL768/TEST512 판정도 시작하지 않았고 실제내용을 열람하지 않았다. 예약/전체선별/KG/감성/GNN/서비스/push/MR 없음.
사용자에게 자동선별 기준은 LLM 대체동의 검증이며 경제적 정답률98%가 아님을 명확히 설명한다.
기준설계: DEV방향별20쌍이상/교사불일치2%이하; CAL방향별183독립그룹이상+보정된일치하한98%이상;
TEST불일치율95%상한2%이하·자동처리30%이상·실측속도이득·fallback동일성. 41건 개발기준과 구분한다.

**2026-09-17 23:59 KST 최신 범위 — 수정 확인41건과 기존 A/B 결과 재사용 비교부터 마친다.**
사용자가 기존1번을 이미 했는데 수정 후41건만 다시 확인하면 되는지 질문했다.
41건은 이미 노출된 문제 사례의 수정 확인이며, 빠른 분류기의 독립 검증을 대체하지는 않는다고 설명했다.
우선 현재41건 실행과 저장된 A/B DEV확률의 고정50:50평균 비교를 끝내기로 안내했다.
**새 본 실험 freeze/1,792쌍 전량 재판정/새 분류기 학습은 진행하지 않는다.**
이전7~9시간은 전량 새 실험을 가정한 예측이며 현재남은41건의 시간이 아니다.
23:59:36 run_003은25/41 처리, include10/exclude13/review1/실행불가1, 실제 실행 중.
기존 A/B 비교는 weights0.5 고정·원래DEV256라벨/임계값 유지, CAL/TEST·GPU사용 없이 별도 산출물로 보존한다.

**2026-09-17 23:56 KST 1번 선별기 개선 작업 진행 — 이전 결과는 보존, 새 본 학습은 아직 시작 전.**
사용자가 "그럼 1번해"로 빠른 선별기 학습·검증 개선을 요청했다. 범위는 1번만 유지한다.
이전 LLM TRAIN1,536/DEV256은 재사용하여 A/B 모두 학습을 끝냈고 23:11 DEV 품질 기준 미달로 종료했다.
기존 데이터나 결과가 사라져 처음부터 하는 것이 아니다. 아래 17:53 기록은 당시 중단 상태이며 이후 A/B는 완료됐다.

진단 `artifacts/kg_cascade_v3_diagnosis_20260917_v1`: 보류675 중334는 정규명 문자열 보호 규칙에서 발생했지만,
B의 DEV 오류93 중87은 LLM 의미 판정 구간이므로 이름 규칙만 바꾸면 해결된다고 주장하지 않는다.
실제 TRAIN/DEV25개와 합성16개를 새 판정 방식의 개발 검사로 사용한다. AI 참고 판정이며 독립 사람 정답이 아니다.
새 `kg_target_aliases.py`, `kg_reasoned_teacher.py`/`_v2.py`/`_v3.py`, `kg_reasoned_student.py`,
`run_kg_reasoned_cascade.py`, `evaluate_reasoned_teacher_development.py` 및 CPU 검사 작성. 원본 고정 코드는 수정하지 않았다.
v1은17건 출력 형식 실패, v2는41건 모두 유효하지만 참고 일치36/41·합성15/16·부적절 채택3건으로 실패했다.
v3는 기업 법인/사건 종류를 먼저 구분하고 주가 중계·과거 직장·다른 운영사의 사건을 구별하도록 보완했다.
동일41건·동일 개발 합격 기준으로 `reasoned_development/run_003` GPU 확인 중(exec session40960).
교사v3 CPU6·학생7·runner21검사 통과, 실제 CAL/TEST는 열람하지 않았다.

새 본 실험 계획 freeze, TRAIN/DEV1,792쌍 전량 재판정, 새 학생 학습은 아직 시작하지 않았다.
사용자가 "그때 거의 다 한 것 아니었나 / 기존 작업으로는 어려운가" 질문했다.
기존 LLM 결과·학습 A/B는 보존되고 재사용 가능하나 기존 선별기가 품질 기준을 통과한 것은 아니라고 설명했다.
전량 새 판정의 7~9시간 예측을 기존 중단 작업의 남은 시간처럼 설명한 혼동을 바로잡았다.
전체 재판정을 유일한 개선 경로로 전제하지 않고, 41건 결과와 기존 라벨 재사용 범위를 검토한다.
동일 라벨 의미가 검증되지 않은 교사 결과를 몰래 섞거나 검증 기준을 낮추지 않는다.
현재 41건 결과가 끝났는지는 실제 프로세스와 status/completion을 다시 확인할 것.
예약 알림은 재생성하지 않았다. 전체 선별/KG/감성/GNN/HDFS·DB·서비스/commit·push·MR은 범위 밖이다.

**2026-09-17 23:16 KST 최종 상태 확인 — 현재 학습 실행 중 아님.**
`artifacts/kg_cascade_v2_pairfix_20260917_v1`은 23:11:49에 `QUALITY_GATE_FAILED`/dev로 정상 종료했다.
23:16:33 확인 launcher39856/coordinator30888/B12252·37576 모두 없음, GPU0%/10MiB.
이번 종료는 실행 오류가 아니라 사전 고정한 DEV 선별 기준 미달이다. 이전 23:00 RUNNING 기록을 최신으로 오해하지 않는다.

A/B 모두 4epochs·DEV256 예측 완료. B는 epoch2 선택, DEV logloss0.8331851664(A0.9172916263).
B의 개발 손실은 더 낮지만 자동 채택·제외 검증은 통과하지 못했다. 모델 개선/운영 가능/사람 정답 정확도를 입증한 결과가 아니다.
사전 기준은 방향별 최소20쌍·LLM 교사와 불일치2%이하. B에서 임계값0.8일 때 채택10쌍 중2불일치, 제외17쌍 중3불일치.
임계값0.9의 채택은4쌍 중0불일치지만 표본 부족이다. 모든 사전 임계값에서 최소20쌍을 충족하지 못한다.
A/B 자동 방향 모두 false → candidate_selection.selected=null. CAL768/TEST512/fallback은 시작하지 않았다.
23:17 무결성 확인: 고정 계획, 완료9단계의 산출물 해시, release→evaluation_report 해시 일치, CAL/TEST 미생성 PASS.
교사 판정과 모델/예측/로그는 보존했다. 같은 계획을 다시 실행해도 동일한 완료단계를 읽고 같은 품질 미달로 종료한다.
새 실험이나 기준 변경, 전체 선별/KG/GNN/서비스 작업은 시작하지 않았다. 예약 알림·commit/push/MR도 없음.
사용자에게 "학습은 끝났고 개발 검증 기준 미달로 종료, 현재 실행 중 아님"을 안내했다.

**2026-09-17 23:00 KST B 실행 오류 수정 후 재개 — 현재 실행은 이 기록을 기준으로 확인한다.**
사용자 "지금 하고 있는거야?" 확인 시 원본 v2는 22:42:07 FAILED였으며 실제 학습 프로세스가 없었다.
A는 22:41:37에 4epochs 완료, epoch3 DEV logloss0.9172916263 선택. A의 DEV 자동 채택·제외 기준은 모두 미달이다.
B는 첫 epoch 전에 tokenizer가 기업/본문 sequence 범위를 겹쳐 저장하는 오류로 중단됐다. 메모리 부족이나 라벨 손상이 아니다.
원본 v2·계획·교사 TRAIN/DEV·A 모델/예측/임계값 결과는 그대로 보존한다. 원본 runner를 그대로 재실행하지 않는다.

수정은 새 `kg_cascade_pair_encoding_fix.py`와 `kg_cascade_student_v2b.py`에서 토큰 위치 마스크·버전/출처 기록에만 적용했다.
입력 토큰·구조·데이터·4epochs·학습률·선택/검증 기준은 유지한다. A 결과를 이미 관측했다는 사실도 새 계획에 명시했다.
CPU helper7/adapter3/runner22검사 PASS. 실제 TRAIN/DEV1,792쌍 전체의 원래 토큰/유형/attention/창 가중치/커버리지 보존 확인.
GPU 합성3창 forward/backward에서 209개 유한 gradient와 attention gradient, head 저장·재로딩 일치 확인.
GPU 영수증은 `artifacts/kg_pairfix_development_20260917_v1/gpu_execution_receipt.json`; 실행 검사이며 정확도 검증이 아니다.

새 실행 `artifacts/kg_cascade_v2_pairfix_20260917_v1`, 계획SHA `1d1e924735764d73b47bd497d88fee8cdf08fc18689294ce843e18e99212377a`.
22:59:50 숨김 백그라운드 launcher39856/coordinator30888, B launcher12252/실제worker37576 명령줄·부모PID 확인.
23:00:35 RUNNING/student_train_B, GPU63%/2908MiB, B plan23:00:16 생성되어 이전 실패 지점을 통과했다.
LLM TRAIN1,536(사용가능1,533)/DEV256 및 A 완료단계5개는 해시 검증 후 재사용했다. B는 새 경로 `student_B/attempt_001`.
launcher 기록은 `launcher_20260917_225950.json` 및 같은 이름 stdout/stderr. 이전 실패 로그와 부분 A001은 보존했다.
후속은 B DEV → 고정된 후보 선택 → 조건부 CAL768/TEST512/fallback/보고서. 기준 미달은 QUALITY_GATE_FAILED로 멈춘다.
새 성능 개선이나 검증 통과는 아직 확인되지 않았다. CAL/TEST는 현재 시작 전이다.

현재 고정 코드 수정 금지. 중단 후 재개할 때 아래 명령을 사용하며 새 계획을 다시 freeze하거나 완료 결과를 덮어쓰지 않는다.
중단된 B는 optimizer 재개가 아니므로 새 attempt로 4epochs를 시작하고, 완료단계는 재사용한다.

```powershell
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/run_kg_cascade_pair_repair.py --mode run --out AI/graph/artifacts/kg_cascade_v2_pairfix_20260917_v1
```

사용자 범위는 **선별기 재학습·검증까지만**. 성공해도 full_fast_pass_allowed=false로 남긴다.
예약 알림을 재생성하지 않았고 전량선별/KG/기업감성/GNN/HDFS·서비스/commit·push·MR은 시작하지 않았다.

**2026-09-17 22:30 KST 사용자 "하던거 다시 하자" 요청으로 재개:** 중단 요청은 이번 재개 요청으로 해제됐다.
동일 v2 계획SHA `af7144450d98348f9ead2961aaf4562f5fdaf335579e35ed5500806ef358e11a`·고정코드·LLM완료6파일·부분A0017파일 해시 재확인 PASS.
LLM train1536(사용가능1533)/dev256 결과를 재사용하며 새 판정/다운로드를 돌리지 않는다.
22:29:59 숨김 백그라운드 시작 launcher16796/coordinator37236. 22:30:42 student_train_A RUNNING, 실제PID생존·GPU93%/2885MiB.
새 학생 경로는 `artifacts/kg_cascade_v2_20260917_v1/student_A/attempt_002`; 기존001·pause_receipt·종료로그 보존.
새 launcher 기록은 `launcher_resume_20260917_222959.json` 및 동일 이름 stdout/stderr 로그다.
현재 코드 변경 없이 A4epochs부터 다시 실행, B/DEV선택/조건부CAL·TEST검증으로 이어진다. 실패한 품질 기준을 낮추지 않는다.
독립 검토에서도 완료단계 재사용·CAL/TEST 순서·재개 휴식시간의 속도평가 제외 확인. 실제 새 성능은 아직 미확인.
범위는 **선별기 재학습·검증까지만** 유지. 예약 알림 재생성 없음, 전량선별/KG/GNN/HDFS·서비스/commit·push·MR 시작 금지.

**2026-09-17 17:53 KST 사용자 요청으로 작업 중단·인계 저장 완료 — 다음 세션은 여기부터 읽을 것.**
사용자가 "한데까지 기억하고 작업 마무리하자"고 요청했다. 새 지시 전 자동 재개하지 않는다.
실제 command line과 부모 PID를 확인한 학생 A worker15656을 종료했고, 부모24056/coordinator7896/launcher38212도 종료됐다.
17:53:10 확인 해당 PID 전부 없음, GPU0%/0MiB. 완료 데이터와 부분 체크포인트는 삭제하지 않았다.
원래 coordinator는 자식 종료를 CalledProcessError/FAILED로 기록한다. 이는 이번 사용자 중단에 따른 종료다.
원본 상태는 `artifacts/kg_cascade_v2_20260917_v1/status_after_requested_stop.json`에 보존했고,
현재 `status.json`은 `PAUSED_BY_USER`, 상세는 같은 폴더 `pause_receipt.json`에 기록했다.

저장된 진행 상황:
- 학습용 LLM: 1,536쌍 처리 완료. include517/exclude341/review675, 실행불가3. 실제 사용가능1,533쌍(review 포함).
- 개발용 LLM: 256쌍 처리 완료. include77/exclude72/review107, 실행불가0.
- 두 완료단계의 6개 산출물 해시, 원본/프로토콜/분모/판정 재현 확인 PASS. 계획·고정 코드 해시도 유지됐다.
- 학생 A: `student_A/attempt_001`에서 4epochs 중 2epochs 완료 후 3번째 도중 중단.
  epoch1 dev logloss1.0120606641, epoch2 0.9373048447. 확정 모델 성능이나 정확도가 아니다.
  부분 체크포인트 포함 7개 파일 해시를 pause_receipt에 보존했다. training_manifest/단계완료 영수증 없음.
  optimizer 상태를 저장하지 않으므로 3epoch부터 이어가는 기능은 없다. 다음 실행은 attempt_002에서 A의 4epochs를 새로 학습한다.
- 학생 B, CAL768, TEST512, fallback 재현 검증, 최종 성능 판정은 아직 시작 전이다.
- 완료 LLM 판정은 재사용한다. 전체 원본 다운로드/판정부터 다시 하지 않는다.

재개 전 `PAUSED_BY_USER`와 프로세스 없음, 계획·완료 영수증 무결성을 확인한 뒤 같은 명령을 실행한다.
`--mode freeze`를 다시 실행하거나 기존 attempt/교사 결과를 삭제·덮어쓰지 않는다. 백그라운드 실행 시 숨김 창과 새 launcher 로그 사용.

```powershell
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/run_kg_cascade_repair.py --mode run --out AI/graph/artifacts/kg_cascade_v2_20260917_v1
```

현재 작업 범위는 여전히 **뉴스 선별기 재학습·검증까지만**이다. 전체 후보 선별/KG/감성 생성/GNN재학습/서비스 게시로 넘어가지 않는다.
예약 알림 heartbeat `cosmos`는 사용자 요청으로 삭제된 상태다. 다시 만들지 않는다. commit/push/MR도 하지 않았다.
기업별 감성·관계 근거 뉴스는 후속 요구사항으로 기록만 되어 있다. 새 선별기 검증 통과나 전체 뉴스 처리 완료를 주장하지 않는다.

**2026-09-17 17:38 KST 예약 취소:** 사용자가 예약된 작업을 하지 말라고 요청해 heartbeat `cosmos`를 삭제했다.
도구가 deleteStatus=deleted를 반환했다. 사용자의 새 요청 없이 자동 확인·완료알림 예약을 다시 만들지 않는다.
현재 실행 중인 v2 선별기 학습·검증 배치는 계속 실행 중이다. 예약 취소를 현재 배치 중단으로 해석하지 않았다.
17:38 확인 train LLM1536/1536완료, dev LLM112/256, 실제 coordinator7896/launcher38212생존·GPU100%.
학생 A/B 학습과 CAL/TEST는 아직 시작 전. 사용자 범위는 여전히 선별기 재학습·검증까지만이다.

**2026-09-17 16:43 KST 사용자 범위 제한:** 현재 뉴스 선별기 재학습·검증까지만 진행하고 완료 시 알린다.
전체 후보뉴스 선별·KG 생성·GNN 재학습·HDFS/서비스 게시 등 후속 단계는 새 지시 전 시작하지 않는다.
현재 v2 runner는 검증/보고서까지만 수행하고 전체 배치를 자동 시작하지 않으므로 코드 변경 없이 그대로 실행한다.
16:43 train208/1536, 추론457.292초(약2.20초/쌍). 이전 학생3epoch603.25초를 A/B각4epoch로 외삽했다.
남은 train/dev약58분+학생A/B약27분+CAL/TEST약47분+fallback/준비시간으로, 18시 최종검증 완료는 어렵다고 알렸다.
전체 단계 진행 시 현실적 예상19:00~19:30이며 확정 시각은 아니다. DEV/CAL 미달 조기종료는 성공완료와 구분한다.
사용자가 완료 알림을 다시 요청해 heartbeat `cosmos`를 새로 생성했다(생성 성공 응답 확인).
10분 간격, 평소 조용히 유지, 완료/품질미달/실행실패 알림. 18시에도 실행 중이면 미완료·남은 단계 안내를 한 번 한다.
완료 후 모니터 중지. 프로세스 변경/다음 작업 자동 시작 금지. 아래 16:38의 알림 부재 기록은 이 재생성으로 대체됐다.

**2026-09-17 16:34 KST 뉴스 선별 수정 실험 시작:** `README_KG_CASCADE_REPAIR.md` 참고.
원본 v1은 train1,536/dev256과 학생3epoch까지 완료했으나 DEV 자동 채택/제외 모두 비활성이다.
15:54~15:58 실제 coordinator/자식 Python은 종료되어 있었고 GPU0%; 상태의 RUNNING은 오래된 값이었다.
원본 CAL 완료 판정0. 종료 원인은 확인되지 않았으며 OOM으로 단정하지 않는다. 원본 실행/결과는 보존한다.
train 보류539에 인용ID누락345·직접/간접불일치99가 섞여 있어 의미상 보류와 실행 실패를 분리했다.
새 `kg_semantic_teacher.py`는 같은 Qwen으로 전체 기사×기업을 두 번 독립 프롬프트·1토큰 분류한다.
이는 새 의사라벨 기준이며 종전 인용검증 교사와 동등한 성능/사람 정답/실제 주가 영향이 아니다.
가격기사 두 전체 형식만 별도 규칙 제외; raw LLM 판정은 보존한다. LG화학 시세 사례는 raw 오채택이 남아 있다.
대상 사명의 정확한 문자상 등장 불명확 시 raw 제외를 보류로 돌린다. 과거사명/별칭/모자회사 복원은 아니다.
개발16(가상8+train/dev 진단8) 실제 GPU 최종 run004: exclude7/include3/review6, 추론23.318초·전체46.059초.
가상8 기대 판정과 일치, 관측 시세2 제외 및 기업귀속2 보류 확인. 사후 개발검사이며 정확도16/16 주장이 아니다.
challenge_receipt는 이 제한된 개발 확인만 승인하고 human_gold/factual_accuracy_established=false를 명시한다.
후보 A/B는 각각4epochs, encoder LR2e-5/head3e-4, seed1709. 동일1536/256/768/512 그룹분할을 재사용한다.
기존 train/dev 진단 이력을 명시했으며 CAL/TEST 새 라벨은 사전 열람하지 않았다. 개발/확인 기준을 낮추지 않는다.
CPU90검사 PASS. 모델/정책/원본/검증해시·분모·재개·오류·실측시간 분리·가격템플릿 경계를 검증했다.
새 실험 `artifacts/kg_cascade_v2_20260917_v1`, 계획SHA `af7144450d98348f9ead2961aaf4562f5fdaf335579e35ed5500806ef358e11a`.
16:34:34 로컬 숨김 백그라운드 실행 launcher38212/coordinator7896, teacher_train 단계 시작 확인.
16:38 확인 train80/1536(채택22/제외19/보류39), 추론누적172.522초, 실제 두 PID 생존·GPU80%/4083MiB.
종전 heartbeat `cosmos` 업데이트는 'Automation does not exist'로 실패했다. 현재 자동 알림 활성 상태를 보장하지 않는다.
자동화를 재생성하지 않았으며, 백그라운드 학습 실행과 알림 설정은 별개다.
후속은 새 train/dev 의사라벨→A/B학생학습→DEV고정→CAL→TEST/fallback실측. 실패 시 전량 확대하지 않는다.
`kg_cascade_bulk.py`는 통과 release가 있어야 별도 실행 가능하며 애매한 쌍은 큐에 남긴다. 전량 시작 전이다.
모델준비시간은 한 번만, 추론시간만 전체 건수로 환산한다. 빠른 전체1회 판정≠모든 보류 해소≠지식그래프 완성.
현재 frozen 코드 수정 금지. 같은 `run_kg_cascade_repair.py --mode run --out ...`으로 완료단계 검증/교사 재개 가능.
중단된 학생학습은 새 attempt에서 시작하며 optimizer 재개는 아니다. 로컬PC를 끄면 이 GPU 작업은 멈춘다.
서비스·DB·기존HDFS·GNN번들·commit/push/MR 변경 없음.

**기업 화면 감성값 후속 범위:** 뉴스→기업별 긍정/부정/중립·원문근거·분석상태를 KG 생성 시 별도 산출해야 한다.
현재 선별모델은 경제관련성만 출력한다. 기존 `lake_sentiment.py`는 제목+본문첫200자의 기사 전체 감성이다.
이를 모든 언급 기업에 복사한 결과만으로 기업별 감성이 완성됐다고 말하지 않는다. 미분석/실패는 NULL, 중립과 구분.
`company_document`의 기업별 nullable sentiment 구조는 있지만 새 기업별 모델/Loader/배포연결은 아직 아니다.
화면 '분석 전'과 로컬 FE의 누락값→중립 처리에는 버전차이가 있다. 실제 배포API/DB 원인을 단정하지 않는다.

**2026-09-17 02:53 UTC(11:53 KST) cascade 진행 갱신:** 서버 후보 export/포장 완료,
2,656,806쌍의 압축파일4,667,146,599bytes 다운로드 완료, SHA256
`8e854db4df745622f0f94d389fdd1f77f96b7c26c022ca6ef70fa756ae14fabe`를 로컬에서 재검증했다.
02:47경 직전 다운로드 파일의 0바이트 관측을 정체로 오해했으나, 실제로는 02:47:10에 다운로드가 끝나
02:47:14부터 VERIFYING_AND_EXTRACTING이었다. 재확인 없이02:49:22에 전송Python을 중단한 것은 오진이다.
`transfer_diagnosis_correction.json`에 정정했다. 추가SCP helper는 완성archive 존재 검사에서 종료되어 전송하지 않았다.
02:52:42 같은 고정실험/같은 검증archive로 재개. launcher34516/coordinator2256/transfer2104.
02:53 현재 RUNNING/download_only, VERIFYING_AND_EXTRACTING, 해당worker CPU사용 확인/새오류없음.
서버재추출·코드/학습계획변경 없음. 실제 새3072 teacher판정·학생학습은 아직 시작 전.
다운로드 진행률은 열린 파일의 한 번의 크기 관측으로 단정하지 말고 최신상태/최종파일/해시를 함께 확인할 것.

**2026-09-17 LLM→빠른 분류기→애매한 기사 LLM 방식의 새 실험 예약 실행:** `README_KG_CASCADE_PILOT.md` 참고.
사용자는 전량 LLM 선별 시간을 줄이면서 판정 품질을 유지하는 방식을 요청했다. 동일 성능은 아직 입증하지 않았다.
`run_kg_cascade_pilot.py`가 다운로드만 → 새 3,072쌍 준비 → Qwen train/dev 판정 → MiniLM 전체 미세조정
→ DEV 임계값 고정 → 독립 CAL 확인 → 봉인 TEST 비교·fallback 재실행을 순서대로 실행한다.
분할은 1,536/256/768/512. 과거 검토 750기사·가격 val/test 11,969기사의 ID/본문/URL을 복원해 제외 목록 동결.
전체 후보 중복 그룹에서 기업 하나씩 뽑는다. 기존 189,101건은 제외 ID 복원에만 쓰며 모집단으로 쓰지 않는다.
교사 판정은 사람 정답이 아니다. 학생은 채택/제외/보류만 출력하며 근거·관계 유형을 만들어 넣지 않는다.
학생 encoder LR 2e-5/head LR 1e-3, 3 epochs, full-token windows, seed1709. 원본 GNN 번들은 유지한다.
CAL 고정 방향별 최소183그룹·교사 일치율97.5%단측하한98%, TEST 추가불일치95%상한2%·자동30%·실측가속 요구.
전건 fallback은 가속 성공이 아니며 실패한 CAL에서 임계값을 다시 고르지 않는다. 독립 사실 검수는 별도 후속이다.
관련 CPU73검사 PASS. 실제 RTX4050 가상6train/3dev·1epoch·10창 학습/재로딩 PASS, 최대할당2.20GiB.
이 GPU 검사는 실행 확인이며 실제 뉴스 성능 검증이 아니다. 원본 인코더/교사 가중치 변경 없음.
실험은 `artifacts/kg_cascade_v1_20260917_v1`, 계획SHA `8e68aafc15b2c0b5ad57d153849d4c191f0af535a81760bcde1fd4636f3d8b65`.
02:14:26 UTC(11:14 KST) 로컬 숨김 백그라운드 실행. launcher PID38740, 실제 coordinator11908/transfer28284.
02:14:28 UTC 현재 download_only/WAITING_FOR_SERVER; 서버 Spark는 EXPORTING, 실제 새 교사 판정·학습은 아직 시작 전.
서버 counts 검증: 원천13,259,404행 → 기업언급1,498,312기사/2,656,806쌍, 누락·모호ID·빈정제본문0.
counts 생성은 전체 export 완료가 아니다. manifest/_VERIFIED/전송검증이 끝나야 로컬 표본 작업을 시작한다.
PC를 끄면 로컬 작업이 멈춘다. 같은 run 명령으로 완료단계 검증·교사청크 재개, 중단된 학습은 새attempt에서 시작.
실험 중 고정 코드 수정 금지(해시 불일치 시 정지). 서비스/DB/HDFS기존출력/전량LLM/commit/push/MR 변경 없음.

**2026-09-17 전체 후보 재선별 준비·실제 속도 비교:** `README_KG_FULL_RESELECTION.md` 참고.
HDFS 완료 dict-v1.3 manifest: 기업 언급 1,498,312기사·2,656,806기사×기업 행.
20260908-prepared의 기존 후보 전체 범위이며 이후 실시간 뉴스/NER 미검출/산업만 언급 기사는 제외된다.
`spark/export_kg_candidates.py`는 새 sandbox에 전체 후보를 분할 추출한다. 실제 서버 Spark smoke 통과,
`/home/ubuntu/ner-work/kg_reselection_20260917_v1`에서 01:32 UTC 시작, 완료 기록은 아직 아니다.
`knowledge_graph_reselection_worker.py`는 입력 해시/고정 정책/모델 검증과 SQLite 청크 재개를 구현했다.
동일 기존32쌍: 종전 단건323.105초, 새단건282.128초, 2개배치505.492초(모델로딩 제외).
세 실행의 선택/관계유형 변경0. 단일 실행의 속도 비교이며 정확도 향상·모집단 처리시간 확정이 아니다.
작은 표본 속도 단순 외삽만으로도 전량 LLM은 수개월이므로 전량 추론은 아직 시작하지 않았다.
사용자는 두 번째 노트북도 RTX4050/6141MiB라고 확인했다. 실제 연결·설치·동일표본 속도 확인은 남았다.
두 PC는 서로 다른 기사×기업 작업을 나누며 VRAM을 합치지 않는다. 서비스/기존HDFS 출력 교체 없음.
과거 전체 정제는 142파일·428,898행 뒤 OOM 정체를 발견, 원본체크포인트 보존 후 정상종료했다.
`recover_lake_normalization.py`의 새 메모리 제한 복구는 정제/전역ID만 수행하며 자동분석·게시는 없다.
실제114행은기존Spark압축payloadSHA일치,문제42238행전체probe통과,새1파일SQLite143/471136검증통과.
01:45:45UTC에새서버run `lake_backfill_recovery_20260917_v1` PID1790883/CPU2/nice10으로정제만재개했다.
01:47:51UTC확인147파일507122행커밋+현재파일9000행처리중. PC종료와독립되며완료기록은아니다.
두PC분할·모델경로이동·정책/모델고정검증을포함한CPU79검사PASS. 휴대용코드/기존32비교표본ZIP생성,
`second_pc_code_and_benchmark.zip`의README_KO참고. 2번째PC에모델전송/설치/추론은아직실행하지않았다.
이번 산출물은 `artifacts/kg_full_reselection_20260917_v1/`. commit/push/MR·GNN재학습 없음.

**2026-09-17 지식그래프용 경제 관련성 선별 파일럿 구현·검증:** `KNOWLEDGE_GRAPH_NEWS_SELECTION_RESULTS.md`, `README_KNOWLEDGE_GRAPH_NEWS_SELECTION.md` 확인.
기존 Qwen3-4B 가중치는 고정하고 기사×기업의 경제 관련성만 판정. 과거 경제정보 유지, 신규성/가격라벨은 별도.
`knowledge_graph_relevance_policy.py`, `run_knowledge_graph_relevance.py`: 전체원문/근거ID, 같은모델두차례관계값일치조건, 불일치/오류보류.
가상10쌍개발7→9→10일치 후정책동결. 이후known718/가격holdout11969제외 새32, 국내16/해외16, 2020~22/본문200~6000.
실제모델19채택/4제외/9보류. 참고A23/5/4,B23/6/3. 참고선택30/32일치(정확도아님):합의채택22/제외5/보류3/이견2.
모델채택19중참고양쪽채택15/양쪽제외1/양쪽보류2/참고이견1. 합의채택누락7=잘못제외3+보류4. 근거ID누락5보류.
HMM광고오채택, 지주/계열귀속불명채택, NAVER과거사업/Microsoft협상/삼성증권주관업무누락. 전량운영품질확보아님.
`knowledge_graph_news_preview.py`는원문SHA/정확인용/전체쌍검사후뉴스→기업만출력. 회사간관계/사건시각/주가효과생성없음.
`knowledge_graph_lake_gate.apply_selection`은analyze_article직후/_write_analysis전로컬연동점;채택기업과양끝채택관계만집계전유지. 현재운영연결없음.
artifacts/kg_news_selection_v1_20260917_v1/:model_run,comparison,graph_preview(원모델19),reviewed_graph_preview(선택3자일치15연결/13기업).
reviewed는평가참고를이용한사후AI자료정리로모델성능아님. 직접/간접유형은원모델값이며합의유형아님. human_gold/ready/production=false.
CPU56검사PASS,실제32원시출력/인용/분모검증. 이전57파일보존. 추론60회323.105초/전체378.4761초. 새평가known750으로확장필요.
기존분류기/GNN/LLM가중치재학습,기존HDFS/서비스/DB수정,commit/push/MR없음. 전체뉴스선별완료라고표현금지.

**2026-09-17 선별기준/평가자료 교정 완료:** `NEWS_SELECTION_RUBRIC_RESULTS.md`, `news_selection_rubric_v1.md` 먼저 확인.
`news_selection_rubric.py`는귀속/신규성/경제구체성/증권범위/주장상태/중요성6축과정확인용을검증.
같은대상사건으로6축판정, candidate는의미상후보뿐. 학습시각·종목·중복·가격구간확인전ready=false.
이전32사후재판정: 후보14/재검토8/제외10. 기존AI채택19만보면후보14/재검토4/제외1(NFLX배경재인용).
원래참고·사용자100선택덮어쓰지않음. 14도확정학습정답아님. 삼성생명신상품은중요성미확정후보유지.
새32는규약동결후known686/holdout11969배제. 동일32를두AI가각각전체원문검토. 사람gold아님.
A후보11/제외8/검토13, B12/5/15. 파생상태일치25/32, 전6축일치9/32(후보6/제외1/검토2), 이견23.
신규성축일치16/32로불안정. 새발표/후속정보차이4건+기타12건. 일치수를정확도로표현금지.
이견은자동합의/제외로바꾸지않음. 전건human_gold=false/usable_as_training_gold=false/ready=false.
B는말미에별도개발작업집계메시지수신(새32사례/상대판정미노출). 완전정보차단주장금지;notes참조.
독립사람검토용원문만의human_blind_inputs.jsonl별도보존. AI답포함queue를blind검수에사용금지.
시각메타16clock_without_verified_zone/16date_only. 사용불가확정아님,별도시각정책검증필요.
23tests·실제96판정형식/인용/해시검증PASS. 모든자료는`artifacts/relevance_rubric_v1_20260917_v1/`.
다음known제외718=686+32. 새정답기준으로기존100을검수했다고표시하지말것. GNN/LLM재학습없음.
기존프롬프트/번들/서비스/HDFS/commit/push/MR변경없음. 후속은신규정보시점확인/독립검토보강.

**2026-09-17 참고19건 사후 감사·해석 정정:** `RELEVANCE_CLASSIFIER_REFERENCE_AUDIT.md` 확인.
19는AI경제관련성참고값이지검증된GNN학습기사/사람정답19가아님. NFLX과거소식의신규확정근거부족,
AMZN비교문장·삼성생명상품소개·두산우선주계약등은관련성/신규성/종목/학습적합성분리가필요.
새분류기32전건보류는그대로지만‘실제정답19누락’으로해석금지. 원래참고·통계수정없음.
새기준을모델결과에맞춰사후조정해정확도재계산하지말것. 기준확정후새표본평가,전량확대보류유지.

**2026-09-17 후속: 사용자100건으로 관련성 분류기 학습·새32건 비교 완료, 자동 선별 후보 미채택.**
`RELEVANCE_CLASSIFIER_RESULTS.md`, `README_RELEVANCE_CLASSIFIER.md`를 먼저 읽는다.
`prepare_relevance_classifier_data.py`: 사용자69채택/31제외, train80=55/25·dev20=14/6.
ID·URL·정규화본문 그룹으로 분할, AI추천을본사용자선택이며 독립사람gold가 아니다.
`relevance_classifier.py`: 기존MiniLM encoder동결, 기업명/제목/전체본문 pair창 최대256토큰,
전토큰overflow보존검사, weightedmean+max768차원, 선형classifier만학습(C=.1/1/10).
CrossEncoder미세조정아님. transformers5 backend.post_process사용(삭제된 build_inputs API금지).
개발균형logloss .691087/.709875/.790305, C=.1선택. dev의양방향무오류/지지3규칙불충족.
adopt_enabled=false/exclude_enabled=false를새기사열람전동결. 전건보류를후보성능으로승격금지.
새32는이전검토654/가격val-test11969와ID·본문·URL중복없음. AI참고채택19/제외9/보류4.
기존Qwen v1:9채택/23보류, 참고채택중9채택·10누락·오채택0, 선택일치13/32.
새분류기:0채택/32보류, 참고채택19전부누락·오채택0, 일치4/32(전건보류기준선과동일).
개별추론합165.2001→0.8909초지만로딩제외이며유효선별향상아님. 양쪽검증오류0.
38tests+실GPU가상2기사PASS. 독립CPU에서C3개재학습·수렴, 선택계수/절편/dev점수차이0.
평가기점수/정책/원문인용/LLMraw검증PASS. 실험은`artifacts/relevance_classifier_v1_20260917_v1/`.
원사용자선택SHA불변. 새표본은이번32도제외:654+32=686. 이번AI참고를사람학습정답으로자동편입금지.
**1000건/전량선별확대안함. 다음은경제적연결경로·어려운제외예시의정답품질/양확보와별도관계모델후보검증.**
GNN재학습/기존번들교체/서비스/HDFS/commit/push/MR없음. 현재언어모델encoder는학습하지않음.

**2026-09-17 후속: 과거 기업 사전·선별 v3 구현 및 새32+문맥10 비교 완료, 선별 후보 미채택.**
`RELEVANCE_IDENTITY_V3_RESULTS.md`, `README_RELEVANCE_IDENTITY_V3.md`를 먼저 읽는다.
`historical_company_context.py` + `data/historical_company_context_v1.json`: 8발행인/9종목/44관측.
GOOG/GOOGL은동일발행인. 공개익일UTC·관측400일·최신자료충돌/만료시예전자료fallback금지.
구MRVL CIK3자료와Hana색인2자료제외, DART과거대응미확인. 사전은202종목전체완성아님.
사명/분할/제품/자회사를구분하며관측후소유불변을보장하지않음. 제목/본문/날짜/종목은보존.
v3는원문무손실catalog압축+현재사건지시+출력부가키투영+greedy의복합변경이다. v1/v2보존.
새일반32는과거검토612/val-test11969와ID·본문·URL중복없음. AI참고채택18/제외11/보류3.
v1→v3: 참고채택중채택11→9, 참고제외중채택1→1, 누락7→9, 선택일치14→12/32.
오류1→6(근거ID누락5+출력잘림JSON실패1), 입력길이초과0/0, 추론합172.73→190.70초.
문맥표본은고정hash-greedy와전역종목당2조건에서지역별8을못채워새기사열람전수량변경기록.
후보14148기사중고정규칙으로지역별5=10건선정(출처지역기준,6종목/5발행인). 사전/정책불변.
동일v3문맥전후: 채택2→0/10, 보류8→10. 참고채택5/제외4/보류1, 채택누락3→5, 일치3→1/10.
문맥후오류3=근거ID누락1+요청밖catalog복사중출력768한도잘림2. 입력최대3832로길이초과아님.
**품질향상미확인, 전량선별/학습적용안함. AI참고판정이며사람정확도/실제주가영향/GNN향상아님.**
코드42테스트PASS, 실제자료가상경계13+별도176검사PASS, 원천989MB·입력42재구성일치.
`artifacts/relevance_identity_v3_20260917_v1/`에계획/동결/표본수변경/출력/참고/감사보존.
사용자100선택원본해시불변(결과보고서참조). 다음새표본에서는이번42도제외(기존612+42=654).
다음은생성단계출력형식강제부터검증후새표본의의미판정확인. 과거사전은더넓은종목/시점보강필요.
GNN/LLM재학습·기존번들교체·서비스/HDFS·commit/push/MR없음. 완료GPU프로세스없음.

**2026-09-17 02:54 KST 후속: 사용자 검수 이후 관련성 보완·새32 비교 완료, 보완판 미채택.**
`RELEVANCE_POLICY_V2_RESULTS.md`, `README_RELEVANCE_POLICY_V2.md`를 먼저 읽는다.
사용자100선택과AI추천의13불일치를원문대조, 귀속/현재사건/산업/심리기준을별도v2에명시했다.
기존검토580/val-test11969와ID·본문·URL중복없는새32(국내16/해외16)에서두정책을동일조건비교.
별도AI참고는채택14/제외13/보류5. 기존채택9=참고채택7/제외0/보류2,
수정채택9=참고채택7/제외1/보류1. 참고채택누락7→7, 전체선택일치10→10/32.
수정판긴지시로입력길이초과0→2, 형식오류0/0. 추론합157.92→200.57초.
HD건설기계기사제외는귀속불명보류와다르고, AMAT업종추천의새채택은개별사건근거가부족했다.
**개선미확인으로전량선별적용하지않음. 사람정확도나GNN향상을뜻하지않음.**
공식과거자료7개로개발5기사문맥보강, v2선택채택3/보류2 그대로(변경0). 전체기업이력사전아님.
회귀25검사PASS,원천989MB SHA+32입력재구성·동결·통계·원사용자선택불변확인.
비교기무결성보강만별도addendum기록,정책/추론/지표계산식은동결후미변경.
`artifacts/relevance_policy_v2_{development,data,run}_20260917_v1/`에보존.
양정책+AI참고가함께채택한7은AI검토후보일뿐확정GNN학습아님. 기존사용자69채택기록유지.
다음은시점별법인식별/사업관계자료와단순언급·회고/산업사건선별품질보강후새표본평가,
그후규모선별→가격라벨→GNN재학습/동일조건비교. GNN재학습·서비스/HDFS·commit/push/MR없음.

**2026-09-17 후속 요청: 사용자 간단 검수 100건 완료 결과 수령·보존.**
`Downloads/cosmos-assisted-selection.jsonl`을 발견해 원문/추천SHA·인용·형식 검증을 통과했다.
사용자 선택100/100, 채택69·제외31·보류0·미선택0. 선택시각02:03:00~02:27:13 KST.
원본SHA `9e4254681b4970264a6536c6d7d480d913ba4552b2d1c30e57a998cf03c55030`.
`artifacts/relevance_assisted_results_20260917_v1/`에 원본바이트·선택목록·채택69원문·제외31원문 보존.
AI추천과다른13건: 보류→채택5,보류→제외7,제외→채택1. 이유는수집하지않아추정으로채우지않음.
`RESULTS.md`, `summary.json`, `differences_from_ai.jsonl`에 검증·후속검토사항 기록.
이는 AI추천을본사용자선택100건이며 독립blind정답이아님. 일치율을정확도로해석금지.
다음은13건차이(과거사명/그룹귀속/산업수요범위)를검토하고 별도표본에서선별품질확인.
GNN방향/강도라벨생성·재학습·서비스/HDFS수정·commit/push/MR없음. 다운로드원본보존.

**2026-09-17 02:01 KST 후속 요청: 영문 번역 및 AI 추천을 참고하는 간단 선택 화면 완료.**
`README_RELEVANCE_ASSISTED_REVIEW.md`를 먼저 읽는다.
사용자가 이유·원문 근거 작성 대신 채택/제외/잘 모르겠음만 선택하기를 요청했다.
`artifacts/relevance_two_stage_data_20260917_v1/assisted_review_ko.html`이 새 화면이다.
기존100건 중 영문50건 제목·본문을 Codex AI가 번역했고 원문·입력SHA·기존HTML은 보존했다.
로컬Qwen 번역 파일럿에서 국가명/개장전/예상손실 오역을 확인해2청크 후 중단했다.
파일럿은 `relevance_review_translation_20260917_v1`, 실제 번역은 같은접두사`v2`에 있다.
기존 원문기반AI검토32건 재사용+나머지68건 원문검토로 추천100건(채택63/제외25/보류12)을 채웠다.
추천·이유·정확한원문인용은 `artifacts/relevance_assisted_review_20260917_v1/assessments.jsonl`.
경제적관련성의 AI추천이지 실제주가변동 보장/학습 정답/사람 기준 정확도가 아니다.
사용자선택은 전부null로 시작하며 선택지우기·되돌리기·내보내기·이전파일불러오기를 지원한다.
출력은 `cosmos-assisted-selection-1`, AI추천과 human_selection을 분리한다.
`review_mode=ai_assisted_selection`, `independent_human_gold=false`; 과거blind검수와 혼합금지.
기존완료검수 선택은 이관하되 원본필드는 prior_annotation에 보존한다. 실제사람선택은0건이다.
기존 strict 검수기/형식은 유지하며 한국어보조 버전`human_review_ko.html`도 별도로 보존한다.
번역검수14+간단선택12테스트 PASS,100추천의원문SHA/인용·50번역연결·초기선택0검증PASS.
브라우저URL보안정책으로 로컬HTML 자동열기가 차단돼 시각확인은 못했으며 우회하지 않았다.
사용자에게 로컬파일 링크를 제공한다. 기존사용자탭은 변경/새로고침하지 않았다.
GNN/LLM재학습·서비스/HDFS변경 및 commit/push/MR 없음. 모델·원문·번역·추천 Git제외유지.

**2026-09-17 01:18 KST 후속 요청: 간단한 관련성→채택 후보 검토 2단계 구현·비교 완료.**
`RELEVANCE_TWO_STAGE_RESULTS.md`, `README_RELEVANCE_TWO_STAGE.md`를 먼저 읽는다.
각 단계는 decision+evidence_ids 두 키만 출력, 유효한1차 채택만2차에 보내며1차 답을 숨긴다.
같은 Qwen3-4B 모델의 재판정으로 독립 검증자는 아니다. 근거2/3개 상한 제거, 원문 인용 복원.
기존 검토480/val-test11969 중복 제외, 국내50/해외50의 사람 검수100건과 새평가32건 준비.
`artifacts/relevance_two_stage_data_20260917_v1/human_review.html`은 오프라인 검수 화면이다.
실제 사람이 작성한 정답0건. JSONL 내보내기/불러오기·입력해시·인용 검증을 구현했다.
새32 동일비교: baseline채택10(참고채택8/제외1/보류1), screen채택12(10/1/1),
최종채택11(10/0/1). 참고채택17 중 누락9→7, 형식오류7→0, 같은 기사 길이초과1건은 양쪽보류.
2차12건중1건(DB손해보험 귀속불명)을보류, 참고채택 추가누락0. 전부보류기준4/32도기록.
개선3건은 baseline도원출력채택이나근거ID상한으로실패했던사례, 의미이해향상으로과장금지.
LITE직접합병기사는새1차가놓쳤고 SBUX국내법인→미국종목귀속충돌1건이남는다.
검수후보10/충돌1은 `artifacts/relevance_two_stage_run_20260917_v1/comparison/`에저장.
154테스트·원천/산출물SHA·동결·통계재계산 PASS. 평균추론10.17→5.48초/쌍(로딩제외1회실측).
정책은새본문/참고/출력보기전고정했고가상8smoke후재튜닝없음.100건사람검수는추가필요.
GNN/LLM재학습·서비스·HDFS변경 및 commit/push/MR없음. 모델/본문/결과 Git제외유지.

**2026-09-17 00:28 KST 후속 요청: 로컬 LLM 관련성 정책 보강·고정 비교 완료.**
`RELEVANCE_FOLLOWUP_RESULTS.md`, `README_RELEVANCE_FOLLOWUP.md`를 먼저 읽는다.
같은 Qwen3-4B/NF4로 기업 역할·시점·경제 범주별 근거를 받는 별도 정책을 구현했다.
가상 smoke 이후 정책/런타임5파일을 동결했고 실제 새 결과를 보고 수정하지 않았다.
개발 실제40+가상8, 기존 검토/val/test 중복을 제외한 새32(국내16/해외16)를 비교했다.
새32의 별도 AI 참고판정은 채택18/제외7/보류7이며 사람 정답이 아니다.
기존 선택은 채택13/보류19, 수정은 채택3/보류29. 참고 AI도 채택한 것은12→2건,
참고 채택 누락6→16건, 검증 실패6→22건, 참고 보류인데 채택한 충돌1건은 그대로다.
**수정판은 악화하여 전량 선별기로 채택하지 않는다. GNN 향상을 입증한 작업도 아니다.**
개발40의 채택6건은 모두 참고 AI와 일치하지만, Tesla 신규성/대한전선 근거 오류가 남는다.
과거 사업/자회사 충돌의 보류도 일부는 모델 이해 개선이 아닌 출력 검증 실패의 결과다.
새 현대차 사례에서도 현대로템 MOU를 대상 기업에 귀속하는 문제가 남았다.
`artifacts/local_relevance_policy_20260916_v2/comparison_development_v2`에 검수 후보6,
`comparison_fresh`에 검수 후보2와 각각 사람 검수 입력40/32를 저장했다. 사람 검수0건.
112테스트·원천/산출물SHA·전 쌍 통계·동결 검증 PASS. 기사 잘림/생성중단0.
모델·결과 Git 제외 유지. GNN 학습 데이터/모델·서비스·HDFS 배치 변경 및 commit/push/MR 없음.
다음 후보는 출력 구조 단순화, 발행 당시 기업 관계, 사람 검수 기준표본이며 효과는 미검증이다.

**2026-09-16 22:09 KST 후속 요청: 로컬 LLM 다운로드·뉴스 관련성 파일럿 완료.**
사용자 요청은 로컬 모델 다운로드/Git 제외/제공 프롬프트로 실제 기사 판정과 후보 선별이다.
`LOCAL_RELEVANCE_PILOT_RESULTS.md`, `README_LOCAL_NEWS_RELEVANCE.md`를 먼저 읽는다.
Qwen/Qwen3-4B-Instruct-2507 revision `cdbee75f17c01a7cc42f958dc650907174af0554`,
공식12파일8,060,915,998바이트/가중치3파일 게시자SHA 검증. `local_models/`는 Git 제외.
기존 venv 버전 보존, bitsandbytes0.50.2는 `local_models/runtime_packages`에만 설치했다.
사용자 프롬프트 원문 보존, 근거ID→원문 복원, 입력·모델·코드·옵션 해시로 재개 검증.
RTX4050 Laptop6GB / NF4 4비트. Windows torch2.6 GQA math fallback을 explicit KV +
efficient SDPA로 변경하고 마스크·CPU 동등성·GPU 실행을 확인했다. 기존 패키지 수정 없음.

최종 `artifacts/local_relevance_pilot_20260916_v1/inference_v4` 48/48 완료:
실제40쌍(2020–22 train)은 채택10/보류30/제외0, 가상8은 채택3/보류5/제외0.
실제 보류 중7은 근거ID 개수 계약 위반,23은 모델 보류. 길이초과/생성중단0.
`review_candidates_v4/`에 독립 AI도 채택한7쌍과 충돌3쌍을 분리했다.
**7쌍은 사람 검수 완료나 확정 학습 뉴스가 아니다.** 참고 판정도 AI이며 사람 검토0.
SK이노베이션 옛 사업부 넥스플렉스의2018년 매각을 현재 실적에 연결하는 오판 발견.
KT·신한지주도 충돌 격리, META 등 채택 일치 사례에도 영향 경로 과장이 남는다.
40건 선택 일치15건은 이 개발 표본의 AI 간 일치이며 주가/모집단 정확도가 아니다.
입력·결과·참고48키/출처해시 검증, 관련65테스트 PASS. 추론합449.16초,
실제평균10.17초/쌍, 최대 torch CUDA 할당3.46GiB. 초기v1/v2/v3중단 결과 보존.
**전량 무검수 선별 적용은 부적절. GNN 데이터·모델·서비스·HDFS 배치는 변경하지 않았다.**
다음은 당시 기업 관계 보강/별도 사람 검수/고정 정책의 후속 평가이며 이 파일럿만으로
1,300만 건 처리나 GNN 향상을 주장하지 않는다. commit·push/MR 없음.

**2026-09-16 후속 조사: 실제 서비스 삼성전자 뉴스 오연결 재현.**
`NEWS_LINK_SERVICE_AUDIT_20260916.md` 참고. GS샵 기사(이투데이2626218)가
카카오페이/SK하이닉스/포스코인터내셔널/삼성전자로 연결되며 공개 API에도 동일하다.
원문 본문에는 이 네 기업이 없고, 페이지 마켓 뉴스 목록의 기업 집합과 일치한다.
sidebar 혼입이 유력하나 실제 저장 raw body·운영 추출기 버전은 미확인.
현재 matcher/백필에서도 줄머리·공백 변형 sidebar 반례로 네 기업 오탐을 재현했다.
FE 뉴스 화면은 기사 관련기업 사이의 전체 최신 그래프 선을 가져와 표시하여 문제를 증폭한다.
이번 조사에서 코드·서비스·DB·백필 실행은 변경하지 않았다. 정제와 관련기업 재처리가 필요하다.
이 기사 발행02:47Z는 백필기준00:00Z 이후이므로 현재 백필 산출물 사례가 아니다.

**2026-09-16 17:14 KST 후속 요청: 과거 뉴스 전체 이력 서버 백그라운드 실행 시작.**
사용자가 PC 종료 후에도 서버에서 과거 전체 처리만 계속하도록 명시 요청했다.
`README_LAKE_BACKFILL.md`를 먼저 읽는다. **RUNNING이며 전체 적재 완료가 아니다.**
run `lake-news-history-20260916-v1`, 원격 `/home/ubuntu/ner-work/lake_backfill_20260916_v1`.
supervisor 최초 PID1531691; SSH 종료 후 재접속하여 PPID1/SID=PID/CPU0,1/nice10 확인.
별도 일회성 디스크 guard PID1536548, 코드 `lake_backfill_20260916_ops_v1/`.
30초마다 검사해12GiB미만이면 해당 supervisor에SIGTERM. guard7테스트 Linux PASS.
정제기 자체20GiB검사와 함께 사용하며 재개 시 새 PID로 guard도 다시 실행한다.
시작일 제한 없이 기준 `2026-09-16T00:00:00Z` 이전 prepared/live/완료 realtime 뉴스,
총4,600 Parquet파일15,297,209,566바이트를 입력 목록으로 고정했다.
17:16 KST 정제 완료파일72/누적39,866행+다음파일처리 관측. 최신 상태는 서버에서 재확인한다.
Spark local[1]/driver4GiB로 읽기·정제, SQLite 전역 ID/중복, 기업·규칙 관계 재추출,
서버 CPU FinBERT 감성, Parquet 검증 후 새 run_id에 게시하는5단계 순차 배치다.
기존90일 ID를 유지하며 충돌 시 중단. 현재 계열사를 모든 과거 기사에 소급하지 않는다.
전체 이력은 뉴스 정제·언급·관계·감성·ID 범위다. 7/30/90일 특징·집계·스냅샷은
위 기준 시각 기준이고 공시·가격은 최근 배치의 고정 입력 재사용. 과거 모든 날짜별
스냅샷 생성, 공시 전체 이력 분석, GNN 학습/추론, DB·서비스 연결, 정기갱신은 범위 밖이다.
Linux31테스트와 게시기9단위테스트 통과, 실제4기사CPU추론 및 HDFS sandbox
13종/17shard/5,196행 게시·양방향 내용 검증 PASS. 전체 완료 인증은 아니다.
입력·코드 해시와 launch audit: `artifacts/lake_backfill_20260916/`.
완료 판정은 `/data-lake/metadata/runs/run_id=lake-news-history-20260916-v1/_VERIFIED`.
기존 결과 보존. 자동 재시작 서비스가 아니므로 서버 재부팅/실패 시 문서대로 수동 재개한다.
commit·push/MR 없음. 아래 최근90일 완료 기록과 혼동하지 않는다.

**2026-09-16 16:11 KST 후속 요청: 최근 90일의 7개 정식 HDFS 경로 적재·검증 완료.**
사용자가 빈 경로 정제/분석/특징/집계/그래프를 채우도록 요청하고 최근 90일 우선 및
코드·사전 업로드/실행, 제목·본문 첫200자 PC 감성 분석 전송/결과 반환을 명시 승인했다.
기준 `2026-09-16T00:00:00Z`, run `lake-recent90-20260916-v1`.
상세 경로·실행법·BE 계약 차이는 `README_LAKE_RECENT90.md`를 먼저 읽는다.

정제 뉴스126,310, 기사×기업97,898, 뉴스 관계 근거611, 공시×기업3,565/관계 근거48.
기업 특징/집계 각각606행(202×3기간), 관계 특징/점수 각각373행(7D65/30D127/90D181),
그래프 노드202/기간별엣지373. 제목·lead FinBERT12,679기사 분석, 언어 미제공34,297은 NULL.
감성은 뉴스 GNN의 기업별 주가 방향이 아니다. 관계는 한국어 규칙/DART 최대주주 표 범위다.
이번 동일 관계·기간에는 뉴스/공시 동시 근거0행이므로, 합의된 한쪽 NULL 정책에 따라
현재 배치의 가중치 슬라이더는 점수를 바꾸지 않는다. 없는 근거를 만들어 채우지 않았다.
각 가격 창에서100기업은 수정종가 미제공으로 수익률/변동성 NULL,100기업은 계산 가능,
WDAY/XEL은 최신 선택 이력에 관측 없음. 오래된 수정주가 기준으로 틈을 채우지 않았다.

완료 표식 `/data-lake/metadata/runs/run_id=lake-recent90-20260916-v1/_VERIFIED`.
같은 폴더 `receipt.json` SHA256 `990fa53a7a2a8fbe65a034b9eaf826a9d7e9870d3404598ce2bded69744f687f`.
13종의 건수/키/스키마/Parquet 전체내용 양방향 대조 PASS. 최종29파일 확인,
Parquet16개373,796,008바이트 모두 비어 있지 않음. `_SUCCESS`13개0바이트는 정상 표식.
로컬 회귀73개, 공시48건 원문 인용, 점수/기사 수 독립검증 통과.
기록 `artifacts/lake_recent90_20260916/`; 원격 prefix `/home/ubuntu/ner-work/lake_recent90_20260916_`.
DB는 자연키→UUID/NULL/스냅샷 ID/전체 current 교체 계약을 별도로 맞춰야 한다.
**HDFS_VERIFIED_ONLY: DB·Spring·프론트 미연결, 모델 변경/재학습·commit·push/MR 없음.**

**2026-09-16 14:21 KST 후속 요청: 기존 모델 번들 HDFS 전달 완료.**
사용자가 `AI/graph/artifacts/news_impact_v2_bundle`의 업로드를 명시적으로 요청했다.
버전 `66c54ef0207b4783`의 전체 번들47파일(원 manifest46파일+manifest 자체),
5,139,756,119바이트를 아래 새 경로에 게시하고 HDFS 전체 재다운로드 SHA 검증을 통과했다.
`/data-lake/sandbox/news/junwoo/models/news_impact_v2/66c54ef0207b4783/news_impact_v2_bundle`
버전 폴더에 `BACKEND_MODEL_HANDOFF.md`와 `upload_receipt.json`도 함께 있다.
로컬 전송기록은 `artifacts/model_handoff_20260916_66c54ef0207b4783/upload_receipt.json`.
HDFS receipt SHA `03528c1392eecd2c5d0e9e084ddc10f1fe0b713aaf36c1cc31696b4eab1c2c12`.
기존 원격 경로는 덮어쓰지 않았고 신규 규모실험 모델은 업로드하지 않았다.
**파일 전달만 완료했으며 서버 환경 설치·API 기동·Spring/프론트 연결은 미실행이다.**

**2026-09-16 품질·규모·시각·후속 기간 점검과18회 학습·검증 완료.**
사용자 승인 “ㄱㄱ 다해봐”. 상세는 `README_NEWS_SCALE.md` 및
`artifacts/news_scale_learning_20260916`에 있다. 기존 모델은 교체하지 않았다.

동일 모집단의 중첩20k/50k/100k × 뉴스 포함/제거 × 3시드 결과:
뉴스 포함 **39.0085/37.7348/38.6672%**, 제거 **38.9747/38.2541/38.8267%**.
원래 GNN39.6143%, 종목 빈도39.2045%. 목표65% 미달이며 증량의 일관된 개선 없음.
원래2023검증4,813쌍·미응답70오답·거래일/시장/기간 동등 평균 기준이다.
HAC lag5/20 각각60Holm 비교에서 규모·뉴스효과·원래GNN·종목빈도 대비 유의셀0.
세 규모 NASDAQ1일 및20k NASDAQ3일의 시장다수 대비4셀만 유의한 양수였다.
4셀 평균 차이 자체는 기술통계이며 유의한 전체 악화나 GNN 자체의 부적합을 입증하지 않는다.
반복 사용 검증이며 새 미래 기간의 성능이 아니다.

원격 후보189,101건(국내154,628/해외34,473),989,238,678바이트를 사용자 다운로드·학습
승인 후 로컬 `artifacts/news_scale_source_20260916_50k_final`로 전송·SHA검증했다.
서버 원본 `/home/ubuntu/ner-work/news_scale_export_20260916_50k_final`,
SHA256 `b06ed0b03eff49e3e224eeb7e101186379134a695272d4ae6bde4f18c600a44c`.
12개 구간 모두 현 후보정책의 전량이라 cap만 올려도 증가하지 않는다.
후보189,101→적격108,538→최대학습100,000기사/207,724정답쌍/208,060기사·기업입력문맥.
20k/50k/100k 직접정답기업178/180/180, 그래프202노드. 국내기사비중 약87.59%.
415,448정답 재계산 max3.32e-15, 무정답336문맥 포함 과거가격·자료격리·4,976그래프시점 검증.
특징생성2,920.953초, 학습fit합4,588.047초.18시드 모두patience6종료, 총189epoch.
부모특징일치·전체PCA/감성연결·6문맥인코더CPU재생(max5.36e-7) 확인.
최종CPU18체크포인트×6기사 max1.19e-7, 선택손실재계산 max5.86e-8,
6앙상블·36지표·120HAC/Holm·출력78해시 검증 PASS. 미선택epoch전체예측 재생은 미실행.
`verification.json` SHA256 `025dc60dd5916655d61c137db8b46da8287e285f5a9eb729814ad64f93f21d38`.

블라인드AI240기사 검토(`SCALE_SEMANTIC_REVIEW.md`)는 종전54,683후보의 진단이다.
채택120 지지77/비지지27/보류16, 제외120 지지45/비지지63/보류12; 사람검토0.
새19만 후보나10만학습셋의 정밀도 인증이 아니며 모집단precision/recall로 해석하지 않는다.
시각/후속평가 감사(`SCALE_TIME_EVALUATION_AUDIT.md`)는 완료했으나 원천시각 인증·
양시장 새1/3일 정답에 필요한 미래종가가 없어 시각교정재학습/미사용후속평가는 미실행.
다음은 사람의 사건주체·시점 검수, 발행/수정시각 계약, 모델동결 후 신규기사·종가 확보다.

원격 추출 두 실패는 해외2022의 Javaheap/Parquet OOM이었다. 완료11구간을 검증재사용하고
마지막을 local[1]/driver6g/비벡터읽기로 완료했다. 원천HDFS·공용설정 수정 없음,
기존Spark설정의 실행이력로그 기록은 있다. 실패로그/명령은
`artifacts/news_scale_export_audit_20260916`에 보존했다.
이전 `news_scale_source_20260916_50k`는 실패실행 메타데이터뿐이며 완료본이 아니다.
`news_scale_smoke_20260916`은 이전코드 검증용이다. 기존smoke의 가격이력필터 후 금융근거
소실196건 문제는 본실행 전 같은조건 재검사로 수정했다. 기사240검토결과로 튜닝하지 않았다.
서비스 연결·원본 모델 교체·commit·push/MR 없음. 기존번들의 별도HDFS전달은 후속사용자
요청으로 완료했으며 `BACKEND_MODEL_HANDOFF.md`와 전송검증기록을 확인한다.
아래는 이전 완료 작업 기록이다.

**2026-09-15 사용자 지정 추가 비교 3종 완료:** `README_THREE_COMPARISONS.md` 참고.
현재 자료의 기업 간 연결/모델 선택/방향 손실 가중치를 한 요소씩 비교했다.
5조건×3시드=15회 학습(1,685.9초), 같은 경로에서 loss/accuracy 선택을 각각 저장해 30체크포인트.
자료20,458기사/30,873학습쌍·평가4,813쌍(미응답70오답)·정답·PCA·원천 모두 동결.
연결 제거 **38.9242→38.3178%**, 정확도 선택 **38.9242→38.8788%**,
시장·거래일 방향 CE 가중치 **38.8788→39.6142%**. 원래 GNN39.6143%와 사실상 같다.
같은 가중치·정확도 선택의 뉴스 제거39.1083%, 뉴스 추가 +0.5060%p도 보정 후 우위 미확인.
17대조×4셀=lag별68개 Holm/HAC; 세 핵심 대조·원래 모델·뉴스 제거 대비 유의한 개선 없음.
세 후보의 NASDAQ1일 다수 클래스 대비만 유의. **65% 미달, 원래 모델 유지.**
12개 회귀검사, CPU30체크포인트×6기사 max1.19e-7, 선택정확도재계산오차0,
지표52/HAC136/Holm68 및 산출물99해시 검증 통과. 미선택epoch전체예측은 별도재생하지 않았다.
산출물 `artifacts/news_three_comparisons_20260915`, 원본·코드 변경 없이 별도 스크립트로 수행.
이번 세 비교를 끝으로 현재 자료에서의 추가 설정 탐색을 중단한다. 원천 발행/수정시각 계약,
과거 기업·별칭 이력, 사건 주체·시점 검수 자료와 새 평가 기간 확보가 다음 우선순위다.
GNN의 문제가 없다고 입증한 것도, 이 자료 결함이 정체 원인이라고 확정한 것도 아니다.
새 test 예측·서비스 연결·운영 모델 교체·commit·push/MR 없음. 아래 후보/진행 상태는 이전 기록이다.

**2026-09-15 기존 학습 범위 복원 + 새 기사 추가 GNN 9회 완료:** `README_NEWS_AUGMENTATION.md` 참고.
직전 사건 필터가 기존 학습을 409기사로 줄인 영향을 분리하려고, 위생 검사 통과 기존 학습을 복원했다.
가격 시점 조건은 정답 없는 텍스트 입력에도 적용: 28,421개 입력 중 1,048개 제거, 정답 제외는 3쌍.
기존 16,965기사/27,252쌍 + 동결된 새 3,493기사/3,621쌍 = 20,458기사/30,873쌍.
직전 사건 v2 그래프·특징·PCA·가격 정답·원래 val 모집단은 그대로 유지했다.
old_full/augmented/no_content 각3시드, 총9개 GAT 학습 완료(893.5초).
원래 직접 val4,813쌍(미응답70도 오답) 정확도 **38.8144/38.8541/38.8790%**, 원래 GNN39.6143%.
공통 예측4,743쌍은 39.3869/39.3852/39.4194%, 원래 GNN39.5271%.
추가 학습은 원래 모델보다 −0.7602%p이고 뉴스 제거 대비 우위도 미확인. **65% 미달, 원래 모델 유지.**
NASDAQ 다수 클래스 대비만 유의한 우위이며, 원래 GNN·종목 빈도·뉴스 제거·old_full 대비 HAC/Holm 우위 없음.
최종 `artifacts/news_augmentation_20260915_v2`; 첫 준비본은 정답 노드만 시점 검사한 미학습 기록으로 보존.
CPU9체크포인트×6기사 max8.94e-8, 시드평균/선택9개, 지표24/HAC40/Holm 및 입력·원천해시 검증 통과.
후속 후보는 복합손실 최소 선택과 거래일 방향정확도 목표의 불일치 분리 대조, 원천 시각/기업 검수,
현 데이터에서 기업 간 연결 제거 비교다. 후보만 정리했고 아직 실행하지 않았다.
새 test 예측·서비스 연결·운영 모델 교체·commit·push/MR 없음. 아래는 이전 단계 기록이다.

**2026-09-15 사건 주체·시점 정제 + 새 기사 GNN 9회 완료:** `README_CURRENT_EVENT_EXPERIMENT.md` 참고.
`current_event_gate.py` v2는 인용 기관·고객 매출·괄호 안 인수 대상·과거 시점 표현을 보수적으로 제외한다.
독립 AI 검토 60기사는 이후 규칙 수정의 개발 자료로 사용했고, 새 블라인드 24기사에서는 지지18/보류4/미지지2였다.
사람 검수 정답이 아니고 과거 기사·실적 프리뷰가 남는다. 시각 원천 계약/법인 이력 인증도 미완료다.
새 기사 3,493건/3,621쌍 + 정제된 기존 409건/420쌍. 주가 관측 시작 전의 기업 연결을 학습에서 제외했다.
기존 가격 정답·분류 경계·PCA·val 모집단을 보존하고, 과거 정제 공동언급/전년도 가격상관 그래프를 재구축했다.
같은 그래프에서 clean_old/expanded/no_content 각3시드, 총9개 GAT를 학습했다(190.8초).
원래 직접 val4,813쌍(기존 위생 미응답70도 오답)의 점수는 35.67/38.18/37.44%, 기존 GNN39.61%.
뉴스 제거·기존 GNN 대비 HAC/Holm 보정 후 우위 미확인. **65% 미달, 원래 모델 유지.**
최종 `artifacts/news_current_events_20260915_v2`; 첫 v1 준비본은 알려진 주체 오류 수정 전 기록으로 보존하고 미학습.
회귀10개, 신규정답7,242개 독립재계산, 7개그래프 전엣지, CPU9체크포인트/인코더6쌍,
지표24개·HAC40개/Holm 검증 통과. CPU예측 max1.19e-7, 원인코더 max7.75e-7.
서비스 연결·운영 모델 교체·commit·push/MR 없음. 아래 "재학습 아직 없음"은 이 작업 이전 기록이다.

**2026-09-15 금융 사건 후보 확장·시각 원천 복원 완료:** `README_NEWS_QUALITY_DATASET.md` 참고.
사용자에게 서버 업로드·실행 승인을 받은 뒤 HDFS 원천을 읽어 별도 작업 폴더로 추출했다.
준비본은 국내 11,149,136행 / 해외 2,110,268행이며 고유 기사 수가 아니다.
기존 export가 버린 원문 발행시각·시간대·수집시각·URL을 복원했다. 기존 29,827기사 중
명시 시각 후보는 6,172건이다. 시간대 없는 값을 임의 변환하지 않았고 기존 available_at/정답은 유지했다.
학습 기간 2017~2022의 금융 키워드 후보 54,683건 → 정제·기업 재확인 후 28,026건 유지.
ID/본문 해시/URL 기준 기존 학습과 겹치지 않는 추가 후보는 26,820건, 사람 검수 자료 84건이다.
최종 경로 `artifacts/news_quality_dataset_20260915_v2`: 7개 회귀 검사, 원천 해시·기존 필드 보존,
원문 인용 위치 86,124개·학습 후보 중복·val/test 겹침 0건 검증 통과. 원본 모델 보존.
첫 로컬 결과의 신규 26,821건은 기존 학습 URL 중복 1건을 수정해 폐기했다. 기존 결과와 코드 스냅샷 보존.
아직 후보는 unlabeled/pending이며 사람 검수 완료 0건이다. 자료 제공사를 사건 주체로 잡거나
과거 인수를 현재 사건처럼 잡는 사례가 남는다(`NEWS_QUALITY_SPOT_CHECK.md`). 후보 수 증가를 성능 향상으로 말하지 말 것.
다음은 사건 주체/발생시점 검수, 원천 시각 계약·과거 기업 식별 확인, 정제 그래프/가격 라벨 연결 후 대조 재학습이다.
이번 단계에서 새 GNN 학습·정확도 측정·서비스 연결·commit·push/MR 없음. 65% 목표는 미달 상태다.
아래 "데이터 개정 아직 미수행"은 이 작업 이전 기록이다.

**2026-09-15 과거 주가 맥락 GNN 6회 실험 완료:** `README_MARKET_CONTEXT_EXPERIMENT.md` 참고.
기사 이용 가능 시각 전 종가(+15분 수집 여유)만 사용한 수익률·변동성·고점 대비 하락폭을 추가했다.
뉴스+주가 / 주가만 각 3시드 학습(569초), 원래 직접 val 4,813쌍 정확도 38.65% / 39.09%.
입력 오류 70쌍을 미응답/오답으로 포함한 점수다. 유지 4,743쌍에서는 39.16% / 39.58%.
기존 GNN 원래 직접 정확도 39.61%보다 낮고, 뉴스 추가 이득 및 65% 목표 미달. 원본 모델 유지.
가격 시각 39,728개, 원가격 특징 6쌍, CPU 6체크포인트, 통계 64개 재계산·회귀 5개 검증 통과.
아래 입력 감사 실험까지 총 15개 GNN을 학습·재생했다. 서비스 연결·commit·push/MR 없음.
다음은 금융 사건 중심 학습 기사·정확한 발행시각·원문/과거 기업 정체성 정비 후 새 기간 검증이다.
이는 다음 데이터 개정 과제로 아직 수행하지 않았다. 목표를 뉴스 감성 분류로 바꾸어 달성했다고 표현하지 말 것.

**2026-09-15 100쌍 입력 감사 + 기업별 문맥 9회 실험 완료:** `README_TARGET_NEWS_EXPERIMENT.md` 참고.
기존 256토큰 밖 대상 기업 40/100, 삽입 피드·기업명 오인식 사례를 확인했다.
100쌍 가격 정답 200개는 독립 재계산 최대 차이 6.80e-9로 일치했다. 정성 뉴스 의미와 가격 정답은 다르다.
입력 정제·기업별 문맥·직접 기업 손실로 target/pooled/no_content 각 3시드를 학습했다(1,847초).
유지 val 4,743쌍의 target 39.09%, no_content 39.13%, 기존 39.53%: 뉴스 추가 이득 미확인.
사용자 목표 65%는 원래 4,813쌍 전체에서 미응답 70개도 오답으로 포함한다. target 38.56%, 기존 39.61%로 미달.
4개 회귀 검사, CPU 9체크포인트·6쌍 인코더 재생, HAC·Holm 64개 교차 검증 통과. 원본 보존.
후속 과거 주가 맥락 GNN 6회 비교도 완료했다(`README_MARKET_CONTEXT_EXPERIMENT.md`, 위 최신 기록).
아래의 "대상 기업별 문맥 아직 미실험" 등은 당시 기록이다.

**2026-09-15 직접·간접 균형 손실 9회 실험 완료:** `README_NEWS_BALANCED_EXPERIMENT.md` 참고.
기존 입력/기업 문맥 입력/뉴스 제거에 동일 50:50 손실을 적용해 각각 3시드 학습했다(879초).
val 전체 정확도는 기존 37.67→38.10%지만 직접 정확도는 39.61→39.48%이며,
문맥+균형은 직접 39.56%, 균형 뉴스 제거는 40.18%다. 뉴스 추가 이득 및 채택 조건 미충족.
336개 HAC·Holm 비교에서 기존 모델 대비 유의한 개선 없음. 원본 모델 유지.
손실 테스트 4개, CPU 9체크포인트×6기사 재로딩(최대 오차 1.34e-7), 통계 교차 검증 통과.
새 모델은 val 90,157행만 예측했다. 새 test 예측·서비스 연결·커밋·push/MR 없음.
다음 후보는 대상 기업별 문맥 특징이며 아직 미실험이다. 아래 다음 작업 표현은 당시 기록이다.

**2026-09-15 문맥 입력 3시드 실험 완료:** `README_NEWS_CONTEXT_EXPERIMENT.md` 참고.
256토큰 내 기업명 포함률은 val KOSPI 59.17→98.54%, NASDAQ 45.88→99.85%로 개선했다.
그러나 val 직접 기업 정확도(시장×기간 평균)는 기존 39.61%→39.45%, 뉴스 제거는 39.59%.
새 입력만으로 예측 개선 미확인, 원본 모델 유지. CPU 6기사 재로딩 최대 오차 8.94e-8.
새 test 성능은 평가하지 않았다. 다음 후보는 기존 입력에서 직접·간접 손실 비중만 변경하는 대조 실험이다.

**2026-09-15 실패 분석:** `README_NEWS_FAILURE_ANALYSIS.md` 참고. 모델 변경 없이 진단했다.
직접 기업의 이름이 256토큰 밖에 있는 비율은 test KOSPI 43.22% / NASDAQ 53.32%.
학습의 기사별 평균 직접 손실 계수 비중은 6.12%. 뉴스 제거 대비 세부 구간 우위도 미확인.
다음 후보는 기업 문맥 입력 보장과 직접·간접 손실 비중의 분리 대조 실험이며 아직 재학습하지 않았다.

**2026-09-15 관계 점수 개인화 추가:** `README_RELATIONSHIP_COMPONENTS.md` 참고.
`build_relationship_seed.py --components --news-hits ...`로 출처별 7D/30D/90D 점수를 생성한다.
새 Loader는 구성 점수와 기본 점수를 current/history에 함께 적재한다. 기존 실행은 과거 재현용이다.
공시는 기간 내 접수 근거만 반영하며 GNN 영향도와 별개다. Python 테스트 통과, 실제 DB/Spark 실행은 미실행.
BE 새 마이그레이션과 전체 기간의 근거 export가 필요하다. 학습 데이터·모델 번들은 변경하지 않았다.

**최신: 2026-09-15 v2 모델 학습·검증 완료, 서비스 연결 제외(사용자 결정).**
`README_NEWS_IMPACT_V2.md`를 먼저 참고한다. 모델 버전 `66c54ef0207b4783`.
실제 지수 라벨, 과거 DART 지분, 원문·NLI 검사 후 한국어 설명을 보강했다.
기사 29,827건 / 845,742쌍, 3시드 GAT. 26개 회귀 테스트와 실제 기사 12조건 재로딩을 검증했다.
방향 예측의 다수 기준선 대비 우위는 여전히 확인하지 못했다. 아래 §12는 v1 실측 기록이다.
Spring/프론트/운영 서버에 연결하지 않는다. 사용자 명시 요청 전 push/MR도 하지 않는다.

**추가 예측력 검증(2026-09-15):** `README_NEWS_IMPACT_EVIDENCE.md` 참고.
뉴스 내용 제거/기업 간 연결 제거 GNN을 각각 3시드 재학습해 8개 모델을 비교했다(624초).
기존 test 275,026쌍에서 NASDAQ 3일의 연결 제거·LightGBM 대비 방향 개선은 확인했지만,
전체 다수 클래스 대비 우위와 뉴스 내용 제거 대비 추가 이득은 확인하지 못했다.
30개 회귀 테스트·168개 통계 비교·1,600개 순위 교차 검증 통과. 원본 모델은 그대로 보존했다.
이는 이미 살펴본 test의 과거 대조 실험이며 새 독립 구간에서 입증한 결과가 아니다.

**2026-09-15 원래 Notion 명세 확인 후 범위 정정:** 뉴스 내용 → 기업별 방향·강도 예측과
GraphRAG 설명이 원래 스토리 3이다. 아래 U1/U3는 가격 충격/소스 기업만 입력한 별도 실험이며,
그 결과로 뉴스 조건부 예측을 불가능하다고 단정할 수 없다. 원래 명세의 세 작업은 구현·검증했고,
현재 안내는 `README_NEWS_IMPACT.md`, 실측 요약은 §12, 작업 이력은
`NEWS_IMPACT_WORKLOG.md`, 새 파이프라인은 `news_impact_data.py`, `train_news_impact.py`,
`serve_news_impact.py` 참고. §8-(A)와 원래 명세의 세 작업은 완료했다.
§3의 가격 누설 경고는 유지하되, "입력 스칼라 하나"는 기존 가격 실험의 제약이다.
새 모델에는 뉴스 텍스트·언급 정보를 넣으며, 관측 후 주가를 입력하지 않는다.

작성 2026-09-14 · 브랜치 `S15P21C205-48` · Jira https://ssafy.atlassian.net/browse/S15P21C205-48

마무리 갱신 2026-09-14: §8-(A) 완료. 양 시장 재실행 결과는 §2-3, 검증 기록은 §10.

**후속 사용자 결정: GNN을 주모델로 학습·서비스한다.** U3 모델과 추론 API를 구현했다.
현재 안내는 `README_IMPACT_GNN.md`, 후속 실측은 §11. 아래 계수표 채택 내용은 이전 단계의
결정 기록이며, 계수표는 이제 기준선으로 유지한다.

이 문서를 **먼저 끝까지 읽고** 작업을 시작할 것. 특히 "밟은 함정" 절은 건너뛰지 말 것 —
같은 함정을 다시 밟으면 그럴듯한 숫자가 나오는데 전부 틀린다.

---

## 0. 제품이 뭔가

SSAFY C205 "cosmos" — 뉴스에서 상장사를 뽑아 지식그래프로 잇고, 기사를 읽을 때
**"이 뉴스로 어느 기업이 영향받나"**를 보여주는 서비스.

유니버스는 **202종목** (KOSPI 100 + NASDAQ 102). AI 파트가 스토리 4개를 맡았다.

| 스토리 | 내용 | 상태 |
|---|---|---|
| 1 | 뉴스→기업 추출 (사전매칭 + NER) | 완료 (`AI/ner/`) |
| 2 | 지식그래프 엣지 + 관계 DB 적재 | 완료 (`AI/graph/`) |
| 3 | **영향도** | 이 문서 |
| 4 | 주가 검증 | 미착수 |

### 스토리 3 에 이미 주어진 것 (건드리지 말 것)

- `AI/graph/data/prices_daily.parquet` — 480,723행, 202종목, 2016-01-04~2026-09-11
  컬럼: `ticker, market, trading_at, open/high/low/close_price, adj_close, trading_volume`
- `AI/graph/data/edges_{sector,ownership,co_mention,correlation,relation_scored}.csv`
- `AI/graph/data/db/` — 관계 **317개** (PARTNER 236 / INVEST 44 / COMPETE 23 / SUPPLY 14)
- `AI/ner/data/company_industry.csv` — 202종목의 산업 18분류

---

## 1. 기존 U1/계수표 단계의 결론 (현재 GNN 채택은 §11)

1. **관계를 타고 다음 세션으로 전파되는 것은 0이다.** 이벤트 스터디로 확정.
2. **같은 세션 동조는 크고 확실하다.** 제품이 보여줄 것은 예측이 아니라 **귀속**이다.
3. **이번 비교에서 GNN 의 룩업테이블 대비 우위를 확인하지 못했다.** KOSPI 차이는
   유의하지 않고(t=-0.89), NASDAQ 은 표보다 낮다(t=-5.04). 같은 실행에서 측정 완료.

이 단계의 산출물은 **영향도 계수표**다. 현재 주모델은 §11 의 U3 GNN 이다.

---

## 2. 실측 숫자 (전부 재현 가능)

분할: **train 2017~2022 / val 2023 / test 2024-01-01~2026-09-11**.
모델 선택은 전부 val 로, test 는 마지막에 한 번만.

### 2-1. 전파 검정 (`event_study_propagation.py`)

이벤트 창 프로파일, excess bp (괄호 t). lag 0 = 이벤트 세션 자신.

```
group      lag-3     lag-2     lag-1      lag0      lag+1     lag+2     lag+3
ALL       0.9(0.3)  2.7(0.8)  9.8(2.9)  28.1(8.2)  8.0(2.6)  2.7(0.9)  4.6(1.5)
```

**lag+1 의 +8.0bp (t=2.6) 를 보고 "전파 있다"고 하면 안 된다.** 같은 크기가 이벤트
*이전*인 lag-1 에도 +9.8bp 로 있다. 짝지은 차분(사후 - 사전)을 하면:

```
ALL -1.50bp (t=-0.33)  |  악재만 +0.93 (0.12)  |  호재만 -3.51 (-0.66)
```

**0이다.** 팩터 제거 수준·이벤트 임계·변동성 전파까지 다 흔들어도 0이다.

### 2-2. 규칙 비교 (`build_impact_scores.py`)

평가 세 가지. **목표가 다르면 이기는 규칙이 다르다.**

| 평가 | 타깃 | 정답 | 뜻 |
|---|---|---|---|
| U1 | 그 시장 전 종목 | 잔차 | 학술적 비교용 (GNN 과 같은 잣대) |
| U2 | A 의 관계 이웃 | 잔차 | 화면이 보여주는 것 (부호 있음) |
| **U3** | A 의 관계 이웃 | **\|잔차\|** | **화면이 실제로 하는 것 (순위만)** |

KOSPI test rank-IC:

| 규칙 | U1 | U2 | **U3** |
|---|---|---|---|
| industry (같은 업종 1) | 0.0725 | 0.2034 | 0.1135 |
| industry_pair (18×18) | 0.0572 | 0.1487 | 0.0418 |
| pair_shrunk | 0.0762 | 0.2013 | 0.1017 |
| **pair_corr** | 0.0727 | **0.2154** | **0.2054** |
| relation_score (스토리2 단독) | 0.0397 | 0.1612 | — |

U3 에서 `pair_corr` 가 업종규칙 대비 **+0.1317 (t=+11.34)**. 수축(shrink)은 부호 없는
과제에서 **해롭다** — 업종쌍 평균으로 당기면 같은 업종 안에서 변별력이 죽는다.

NASDAQ 은 관계 이웃이 적어 주요 규칙의 U2/U3 이벤트가 340~515건뿐이다.
`pair_corr` 의 업종규칙 대비 t 는 U2 +0.21 / U3 +0.64 → **제품 순위에서 우위 확인 못함.**
마무리 때 CSV 를 대조해 "t 가 전부 2 미만"을 정정했다: U3 `pair_shrunk` 는 t=-2.72 로
오히려 나쁘다. 전체 횡단면 GNN 비교는 §2-3 과 별개로 읽을 것.

### 2-3. GNN vs 표 (2026-09-14 마무리 재실행)

같은 실행, 공통 test 이벤트 **KOSPI 9,486건 / NASDAQ 10,117건**.
GNN 은 시드 0/1/2 의 **점수 평균**으로 비교한다 (시드별 IC 평균과 다름).

| 모델 | KOSPI val | KOSPI test | NASDAQ val | NASDAQ test |
|---|---|---|---|---|
| 전엣지 동일가중 | 0.0175 | 0.0164 | 0.0481 | 0.0597 |
| relation 부호×점수 | 0.0440 | 0.0415 | 0.0278 | 0.0190 |
| co_mention npmi | 0.0681 | 0.0542 | 0.0442 | 0.0518 |
| 산업 동일가중 | 0.0927 | 0.0822 | 0.0604 | 0.0830 |
| **pair_shrunk (표)** | 0.1072 | **0.0838** | 0.0801 | **0.1089** |
| GNN 3홉 | 0.1081 | 0.0795 | 0.0720 | 0.0952 |

relation 은 공통 집합을 정할 때 제외한 좁은 모델이라 유효 이벤트가
KOSPI 6,663건 / NASDAQ 2,697건뿐이다. 이 행 평균을 다른 모델과 직접 빼지 말 것.

짝지은 차이 (GNN - 기준선, **세션 클러스터 t**):

| 기준선 | KOSPI 차이 (t), 627세션 | NASDAQ 차이 (t), 653세션 |
|---|---|---|
| 산업 동일가중 | -0.0027 (-0.38) | +0.0122 (+2.33) |
| pair_shrunk (표) | -0.0043 (-0.89) | -0.0138 (-5.04) |

KOSPI 는 val 에서 GNN 이 조금 앞서다 test 에서 뒤집혔지만 차이가 유의하지 않다.
이 사실만으로 과적합 원인까지 확정하지 않는다. NASDAQ 은 업종규칙보다 높아도 표보다
낮다. **두 시장 모두 이번 설정에서 표 대비 GNN 우위는 확인하지 못했다.**

인계 당시 KOSPI GNN val/test 는 0.1084/0.0804 였다. 이번 0.1081/0.0795 와 섞지 않는다.
시드만 고정했고 CUDA 결정론 설정은 꺼져 있어 수치의 완전한 일치를 보장하지 않는다.

### 2-4. impact_direction (`estimate_edge_direction.py`)

test 부호 적중률 (n=310):

| 규칙 | val | test |
|---|---|---|
| 유형 기본값 (COMPETE만 NEGATIVE) | 63.8% | 57.4% |
| **전부 POSITIVE (파라미터 0개 기준선)** | 68.8% | **63.5%** |
| 유형별 혼합 (\|r\|≥0.05) ← 채택 | 73.5% | **71.9%** |

유형별: COMPETE 95% (전부 POSITIVE 채택) / PARTNER 68% / SUPPLY 86% / INVEST 77%.
**COMPETE 에 NEGATIVE 를 주면 적중률 5%다.** 국내 동종업계는 같이 움직인다.

---

## 3. 밟은 함정 — 다시 밟지 말 것

### ① 산업 팩터를 뺀 잔차를 타깃으로 쓰면 산술이 새어든다 (test IC 0.47 이 나왔다)

`residualize()` 는 그룹의 leave-one-out 평균을 뺀다 → 그룹 안에서 잔차 합이 0에 묶인다.
산업 그룹 크기별 평균 잔차 상관을 재면 경제가 아니라 1/(n-1) 이 보인다:

```
그룹 크기      2~4개        5개         6개        9개       19~20개
평균 잔차 상관  +0.13~+0.56  -0.09~-0.16 -0.04~-0.14 -0.02~-0.05 -0.034~-0.044
```

MIN_GROUP=5 미만이라 팩터를 **안 뺀** 그룹만 양수다. 부호가 경제가 아니라 "뺐냐"로 갈린다.
→ **타깃 잔차는 `factors="market"` 만 쓸 것.**

### ② 횡단면 전체를 입력에 넣으면 어떤 팩터 정의를 써도 샌다

베타가 대체로 1이라 시장 전체에서도 잔차 합이 거의 0이다. "나머지 전부의 합" ≈ -(자기 자신).
→ **입력은 소스 한 종목의 스칼라 하나로 제한할 것.** 부수 효과로 다홉 전파가 안전해진다
(B 의 값이 입력에 없으므로 B→A→B 경로가 생겨도 샐 게 없다).

### ③ 기준선을 허수아비로 잡으면 이겼다고 착각한다

부호 작업을 처음에 "유형 기본값 55% → 66%" 로 보고했는데, 파라미터 0개짜리
"전부 POSITIVE" 가 59.7%였다. 그걸 안 놓고 비교한 것.
→ **항상 파라미터 0개 기준선을 같이 보고할 것.**

### ④ 추정한 공간과 쓰는 공간이 다르면 안 된다

부호를 `market+industry` 잔차로 추정했는데 모델은 `market` 잔차를 쓴다. ①의 기계적 음의
상관을 관계의 부호로 착각했다. 공간을 맞추니 이득이 사라졌다(63.5% → 63.5%).

### ⑤ 모델마다 채점한 이벤트 수가 다르면 비교가 아니다

커버리지가 좁은 모델이 쉬운 이벤트만 푼 셈이 된다. **공통 이벤트에서 비교할 것.**
단, 교집합을 정할 때 커버리지가 아주 좁은 모델(relation_score: KOSPI 63% / NASDAQ 23%)은
빼야 한다. 안 그러면 비교 자체가 거기로 쪼그라든다.

### ⑥ t 값은 세션 클러스터로 낼 것

같은 날 이벤트 수십 개는 독립이 아니다. 순진하게 세면 t 가 **1.5~1.6배** 부풀려진다.
`train_impact_gnn.paired_t()` 가 순진 t 와 클러스터 t 를 같이 준다.

### ⑦ 엣지에 미래정보가 들어간다

- `edges_correlation.csv` 는 2025-09~2026-09 가격으로 만들었다 = test 구간. **평가에서 제외.**
- `edges_co_mention.csv` 는 2012~2026 누적. 서버에서 `--until 2024-01-01` 로 다시 만든
  `data/edges_co_mention_pre2024.csv` 를 `--co-mention` 으로 넘길 것. (7,383 → 6,126쌍)
- `build_relationship_seed.py` 의 `corroboration()` 이 correlation 을 25% 섞는다.
  317관계 중 21개에서 실제로 max 를 차지한다. 실측 영향은 0.0001 로 무시할 수준이지만 알 것.

### ⑧ 시장 달력 정렬 (틀려도 겉으로 티가 안 난다)

KRX 06:30 UTC 마감, NASDAQ 20:00 UTC 마감. 같은 날짜 D 안에서 시간 순서는
**KOSPI(D) → NASDAQ(D) → KOSPI(D+1)**.
- 같은 세션(동시): 같은 시장이면 그 세션 자신, 교차시장이면 그 직전에 끝난 세션
- `baseline_onehop.source_index()` / `train_impact_gnn.aligned_tensors()` 가 이 규약.
- 쌍별 상관은 `build_correlation_edges.lag_align()` (NASDAQ t-1 → KOSPI t). 같은 날짜로
  맞추면 교차시장 상관이 -0.006 으로 죽는다.

### ⑨ 수익률은 반드시 `adj_close`

NASDAQ `close_price` 는 액면분할 미반영 (NASDAQ 행의 64.6%에서 불일치). 분할일이 -50%
폭락으로 잡힌다.

### ⑩ train 표준편차가 없을 때 `1`로 채우면 정답 크기가 왜곡된다 (U3 후속 감사)

train 표준편차가 없는 종목은 16개다. 기존 `sd=1` 대체는 이 기업의 test 잔차를 다른
기업보다 훨씬 작게 정규화한다. 상관 미추정값 0을 내는 표가 이를 맞히면 인위적인 이득이다.
다른 조건을 고정한 KOSPI 대조: 대체 시 pair_corr IC 0.2051(4,722건), 제외 시
0.0915(4,393건). 기존 §2-2 와 별개 감사 실행이며 숫자를 혼용하지 않는다.
**새 U3 모델은 train 표준편차가 없는 타깃을 평가에서 제외**하고, 서빙 때 이력 부족을
표시한다. 이전 실험 코드는 재현용으로 보존한다.

---

## 4. 파일

### 스토리 3 코드 (`AI/graph/`)

| 파일 | 역할 |
|---|---|
| `impact_dataset.py` | **공용 데이터 계층** — 잔차 패널, 시간분할, 엣지표, 계수행렬 |
| `event_study_propagation.py` | 전파 실재 여부 판정 |
| `baseline_onehop.py` | 파라미터 0개 베이스라인 (초기 탐색용) |
| `estimate_edge_direction.py` | 쌍별 impact_direction 추정 |
| `train_impact_gnn.py` | GNN + 전 모델 공통 이벤트 비교 |
| `build_impact_scores.py` | **최종 산출물 생성** |

각 파일 docstring 에 설계 근거와 실측 결과가 전부 적혀 있다. **코드 읽기 전에 docstring 부터.**

### 산출 데이터

| 파일 | 내용 |
|---|---|
| `data/impact_coefficients_relation.csv` | **568행 — 관계 엣지만. 제품용** |
| `data/impact_coefficients_top.csv` | \|계수\|≥0.15 — 확장 기능용 |
| `data/impact_coefficients.csv` | 35,532행 전체 조밀 행렬 — **진단용, 제품에 쓰지 말 것** |
| `data/impact_scores_eval.csv` | 규칙 비교 전체 결과 |
| `data/db/relationship_impact_direction.csv` | 317행 쌍별 부호 (DB 시드가 읽음) |

계수표 컬럼: `src_ticker, dst_ticker, src_market, dst_market, coefficient,
impact_direction, basis, src_industry, dst_industry, n_train_days, pair_corr, rule, fit_window`

`basis` 는 `relation+same_industry` / `same_industry` / `cross_industry` / `relation+cross_industry`.
분류별 신호 크기 (|계수| 중앙값): 관계+같은업종 0.131 > 같은업종 0.101 >> 관계+다른업종 0.035 >
다른업종 0.024. **신호는 같은 업종에 몰려 있고 나머지는 잡음이다.**

---

## 5. git 상태 (인계 당시 기록)

```
브랜치 S15P21C205-48 (dev 의 ee9d8c8 에서 분기)
커밋 2개, 모두 미푸시:
  ce263d2 test(S15P21C205-48): 관계 엣지의 다음날 전파를 이벤트 스터디로 검정해 ...
  860e23d fix(S15P21C205-48): 관계 영향 방향을 ... (※ 커밋 메시지의 55%→66% 는 틀린 숫자.
          이후 감사에서 63.5%→71.9% 로 정정됨. 코드·docstring 은 정정 완료)
```

**미커밋 (커밋해야 함):**
```
 M AI/graph/.gitignore
 M AI/graph/build_relationship_seed.py
 M AI/graph/data/db/relationship_impact_direction.csv
 M AI/graph/data/db/relationship_score_current.csv
 M AI/graph/estimate_edge_direction.py
 M AI/graph/impact_dataset.py
 M AI/graph/spark/build_comention_edges.py
?? AI/graph/build_impact_scores.py
?? AI/graph/train_impact_gnn.py
?? AI/graph/data/impact_coefficients*.csv       <- .gitignore 에 추가 필요
?? AI/graph/data/impact_scores_eval.csv         <- .gitignore 에 추가 필요
```

---

## 6. 환경

- **파이썬은 `AI/ner/.venv` 만 쓸 것.** 시스템 python 없음.
- 실행 시 `PYTHONUTF8=1` 필수 (콘솔 한글 깨짐)
  ```
  cd AI/graph && PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe <스크립트>.py
  ```
- torch 2.6.0+cu124, CUDA 사용 가능 (RTX 4050). GNN 학습 3시드 60에폭 ≈ 15분.
- **서버** (Spark/HDFS): `ubuntu@j15c205a.p.ssafy.io`, PEM `~/.ssh/J15C205T.pem`
  - `spark-submit` 은 `/opt/spark-4.2.0-bin-hadoop3/bin/spark-submit --master local[2]`
  - `hdfs` 는 비대화식 ssh 에서 PATH 에 없음 → `export PATH=$PATH:/opt/hadoop/bin`
  - **주의**: 공유 `spark-env.sh` 의 `SPARK_LOCAL_DIRS=/data/spark/local` 때문에 권한 오류가
    난 적 있음. 그때는 `SPARK_CONF_DIR=/home/ubuntu/ner-work/spark-conf` 로 우회했다
    (이미 만들어 둠). 공유 설정은 건드리지 말 것.
- **로컬 Postgres**: `BackEnd/docker-compose.yml` (cosmos/cosmos1234, DB cosmos, 5432)
  - Git Bash 에서 docker 경로가 깨지니 `MSYS_NO_PATHCONV=1` 필요
  - 적재: `docker cp AI/graph/data/db/. cosmos-postgres:/tmp/db` 후
    `docker exec -w /tmp/db -i cosmos-postgres psql -U cosmos -d cosmos -f load_relationships.sql`

### 재현 명령

```bash
cd AI/graph
PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe event_study_propagation.py --profile -3 3
PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe estimate_edge_direction.py
PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe build_impact_scores.py
PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe train_impact_gnn.py \
    --seeds 3 --epochs 60 --hops 3 --co-mention data/edges_co_mention_pre2024.csv
```

---

## 7. 작업 규칙 (팀 관례)

- 브랜치는 **Jira 이슈 키만** — `S15P21C205-48`. 임의로 바꾸지 말 것.
- 커밋 메시지는 **한 문장**, 형식 `<type>(<이슈키>): <제목>`. **Co-Authored-By 금지.**
- **push 와 MR 은 사용자가 명시적으로 요청할 때만.** 알아서 푸시하지 말 것.
- 커밋 후 커밋 메시지를 사용자에게 알릴 것.
- **추정하지 말고 측정할 것.** 이 프로젝트에서 "아마 이럴 것이다"로 갔다가 틀린 적이 여러 번.

---

## 8. 다음에 할 일

### (A) 마무리 — 2026-09-14 완료

1. [x] **NASDAQ head-to-head 재실행.** 지정된 3시드·60에폭·3홉 명령이 양 시장 모두
   완료됐고 결과 CSV 32행을 저장했다. 공통 이벤트 결과는 §2-3.
2. [x] **docstring 의 옛 숫자 정리.** GNN 헤더와 부호 추정·계수표·공용 계층의 낡은 설명을
   정리했다. 과거 오류를 설명하는 "55% → 66%" 는 오류 이력으로만 남겼다.
3. [x] **`.gitignore` 에 `data/impact_coefficients*.csv`, `data/impact_scores_eval.csv` 추가**.
   §5 의 미커밋 작업과 이번 정리를 함께 로컬 커밋한다. push/MR 은 하지 않는다.

### (B) 서비스 연결 — AI API 완료, 웹 연결 미실행

현재 GNN API 계약은 `README_IMPACT_GNN.md` 참고. 아래는 이전 계수표 방식의 연결 검토
기록이다. 사용자는 이후 GNN을 선택했고, Spring/프론트 수정과 운영 배포는 아직 하지 않았다.

프론트 연결. `FrontEnd/src/features/news/NewsPage.tsx:400,432` 에 이미
2홉 전파 프로토타입이 있다:

```js
const DECAY = 0.82;                              // 근거 없는 상수
s = relevanceScore × impactScore × 100           // 직접 언급 기업
s = 앞단계점수 × (edge.score / 100) × DECAY       // 1~2홉
```

문제: `edge.score` 는 **관계 세기**(근거 기사 수·lift)지 전달계수가 아니다. 그리고
`impactDirection` 을 안 쓴다. 여기에 `impact_coefficients_relation.csv` 를 넣으면
DECAY 도 필요 없다(홉마다 계수를 곱하면 감쇠가 데이터에서 나온다).

선택지: ① 프론트에 상수로 심기(백엔드·DB 변경 0) ② DB 에 `impact_coefficient` 컬럼 추가
(V4 마이그레이션 + DTO) ③ 지금처럼 부호만 쓰기. **사용자가 아직 안 골랐다.**

> 참고: 제품이 "영향받을 기업 **순위만**" 보여준다면 서빙에 실시간 가격이 **필요 없다**.
> 한 기사에서 소스의 움직임은 모든 후보에 똑같이 곱해지는 스칼라라 순위를 못 바꾼다.
> 가격은 계수를 *추정*할 때만 쓴다. 이러면 "다음날 전파 0" 문제도 비켜간다.

### (C) 다음 후보 — "GNN 을 쓸 방법"을 찾는다면

사용자가 마지막에 물은 것. GNN 이 진 이유는 **입력이 스칼라 하나뿐이라 학습할 게
엣지 가중치 표뿐**이고, 그 표가 곧 `pair_corr` 이기 때문이다. GNN 이 구조적으로
유리한 자리를 찾아야 한다.

**① 이력 없는 쌍 (측정 준비 완료, 제일 빠름)**

관계 쌍 606개의 train 겹침 분포 — 이미 측정한 값:

| train 겹치는 세션 | 쌍 수 | 비중 |
|---|---|---|
| 60 미만 (쌍별 추정 **불가**) | 70 | 11.6% |
| 60~250 | 36 | 5.9% |
| 250~750 | 52 | 8.6% |
| 750 이상 | 448 | 73.9% |

종목 24개가 train 관측 250일 미만. 이력 **0일**인 것도 있다 —
삼성에피스홀딩스, LG씨엔에스, 산일전기, 에이피알. **정확히 뉴스가 많이 나는 종목들이다.**

가설: GNN 은 "이 회사는 반도체 업종이고 SK하이닉스와 PARTNER 이고 공동언급이 많다"는
구조에서 일반화하므로, 과거 가격이 없어도 예측이 나온다. 표는 원리적으로 불가능하다.

검정 방법: `train_impact_gnn.py` 의 공통 이벤트 비교에 **타깃 유니버스를 이력 부족
종목으로 제한한 평가를 추가**하고, 거기서 GNN vs `pair_corr` 를 비교한다.
이기면 산출물이 **하이브리드**가 된다 — 이력 충분하면 표, 부족하면 GNN.

**② 뉴스 내용을 조건으로 (잠재력 최대, 하루 이상)**

지금 계수는 쌍마다 **하나**다. 공급계약 뉴스는 SUPPLY 엣지로, 실적 뉴스는 동종업계로
다르게 전파돼야 하는데 정적인 표는 이걸 표현 못 한다. 스토리 1·2 가 재료를 이미 만들어
뒀다(멘션, 관계 유형, 근거 문장).

**③ 노드 늘리기 (제일 오래 걸림)**

202개는 GNN 에 작다. KOSDAQ 포함 수천 개로 늘리면 같은 파라미터에 데이터가 훨씬 많아진다.
가격은 pykrx 로 받을 수 있지만 멘션·관계도 다시 돌려야 한다.

---

## 9. 알려진 한계 (발표에서 먼저 말할 것)

- **NASDAQ 제품 순위의 우위는 확인 못함.** 관계 이웃이 적어 U2/U3 주요 규칙의 이벤트가
  340~515건뿐이다. `pair_corr` 의 업종 대비 t 는 +0.21/+0.64, U3 `pair_shrunk` 는 -2.72.
  스토리 2 의 관계 추출이 국내 기사 위주라서 생긴 일. 전체 횡단면 평가는 따로 읽을 것.
- **인과가 아니라 동조다.** 다음 세션 전파가 0인 이상 "A 가 오르면 B 가 오른다"가 아니라
  "같이 움직인다"로만 읽어야 한다. 수익 신호로 쓰면 안 된다.
- **INVEST 부호는 여전히 약점.** 유형 기본값(전부 POSITIVE) 84% > 쌍별 추정 77%.
  지분 관계는 prior 가 이미 좋아서 추정이 못 이긴다.
- **윈저라이즈가 전 구간 분위수를 쓴다** (`build_correlation_edges.log_returns`).
  train 구간 클리핑 경계가 test 구간 극단값에 영향받는 미세한 미래정보. 실측 영향은
  소수점 셋째 자리를 못 움직이지만 남아 있는 결함이다.
- **ownership/sector 엣지는 as-of 필터가 없다.** 지배구조·업종은 연 단위로 거의 안 변해서
  전 구간 유효로 뒀다.

---

## 10. 마무리 실행·검증 기록 (2026-09-14)

- 실행: `train_impact_gnn.py --seeds 3 --epochs 60 --hops 3 --co-mention data/edges_co_mention_pre2024.csv`.
  `AI/ner/.venv/Scripts/python.exe`, `PYTHONUTF8=1`, `PYTHONUNBUFFERED=1` 사용.
  약 13분, 정상 종료. 13,608엣지·52특징·market 잔차·직전 연도 베타.
- Python 3.12.14 / torch 2.6.0+cu124 / numpy 2.5.3 / pandas 2.3.3 / scipy 1.18.1,
  RTX 4050 Laptop GPU. 실행 중 모델 로직·설정·입력 데이터는 변경하지 않았다.
- 원본 결과 `data/impact_gnn_results.csv` (32행), 콘솔 로그
  `data/impact_gnn_story3_finish.log`, 입력 9개 파일의 SHA-256·버전·명령
  `data/impact_gnn_story3_inputs.log` 를 로컬에 보관했다. 재생성 가능한 파일들은 git 제외.
- 결과 CSV 에서 GNN/표/업종의 공통 test 이벤트 수가 시장별로 같은 것을 검증했다.
  일반 모델 행의 `test_t` 는 순진 t 이며, `[공통] GNN - ...` 행의 `test_t` 만 세션
  클러스터 t 다. 차이 행의 `n_events` 는 이벤트가 아니라 세션 수다.
- 부호 추정을 별도 출력 파일로 재실행: val 73.5% / test 71.9%, 유형별 혼합 |r|≥0.05.
  기존 부호표와 내용이 같고, DB 점수표의 317개 관계와 부호가 모두 일치했다.
- 휴장일을 포함한 교차시장 세션 정렬, 무효 이벤트 IC 의 원래 행 위치 유지,
  공통 이벤트의 희소 행 인덱스와 세션 마스크 검증 통과. 변경 대상 Python 7개 구문 검사 통과.
- 계수표 35,532행 / 관계 568행 / 확장 3,026행의 중복·자기쌍·부호·임계·규칙·추정창을
  검증했다. 규칙은 `pair_corr`, 내보내기 추정창은 `train+val`.
- 기존 평가 CSV 를 대조해 NASDAQ 의 "모든 t<2", 부호표의 옛 U2 수치,
  `pair_shrunk` 의 표본수 기반 수축 설명을 정정했다. 현재 수축은 고정 alpha=0.5 다.
- 학습 시작 시 train 관측이 없는 열의 `nanstd` 경고가 발생했다. 기존 코드의 표준편차
  대체 처리 후 실행이 완료됐다. §9 의 잔여 한계와 §8-(B)/(C) 후속 과제는 그대로 남는다.

---

## 11. GNN 주모델 채택과 U3 학습·서빙 (2026-09-14)

사용자가 GNN 채택을 명시했다. `train_impact_u3.py`는 단일 기업의 단위 입력과 그래프로
관계 이웃의 영향 **크기 순위**를 학습한다. 기존 `train_impact_gnn.py`는 U1 재현용으로 유지.
현재 서비스 모델은 `artifacts/impact_u3/model.pt`, CPU 추론은 `impact_ranker.py`,
API는 `serve_impact.py`다. 실행·연결 계약·한계는 `README_IMPACT_GNN.md` 참고.

| 시장 | GNN val IC | GNN test IC | pair_corr test IC | 짝지은 차이 | 세션 t | 공통 test 이벤트 |
|---|---|---|---|---|---|---|
| KOSPI | 0.1644 | 0.1349 | 0.1042 | +0.0306 | +2.69 | 4,351 |
| NASDAQ | -0.0238 | 0.1417 | 0.0312 | +0.1105 | +2.55 | 554 |

이번 설정에서 GNN이 표보다 높았다. NASDAQ은 val 59건뿐이고 val IC가 음수여서 안정성을
확정하지 않는다. U3의 후보·정규화 조건이 바뀌었으므로 §2-2/§2-3과 직접 비교하지 않는다.
학습 가중치·그래프·특징/종목 순서를 함께 저장했고 GPU 학습 출력과 CPU 재로딩 순위가
일치했다. API는 기업 코드를 받아 같은 대상 시장의 관계 후보만 반환한다.

추가로 해결한 것: 클리핑 경계를 2022년 말 이전 이력으로만 추정, train sigma 없는 타깃 제외,
가격에서 정한 관계 부호/점수와 집계 신뢰도 특징 제외. 기존 데이터 파일은 변경하지 않았다.

회귀 테스트 8개와 실제 Uvicorn HTTP 요청 검증을 통과했다. 로컬 30요청 응답시간 중앙값
15.9ms, 최대 25.6ms. 모델 버전 `38848187276d3333`, 번들 3,083,684바이트.

남은 범위: 뉴스 문장별 조건화, 최신 관계 스냅샷 반영 및 재평가, Spring/프론트 연결,
운영 배포. 현재는 같은 소스/시장이면 기사 내용이 달라도 같은 순위이며 가격 예측이 아니다.
pre2024 그래프는 val 기간 구조 정보를 포함하고 ownership/sector의 역사적 as-of는 없다.
**실행 가능한 AI 모델·내부 API를 완성한 상태이며, 웹 화면과 운영 서버에는 아직 연결하지 않았다.**

---

## 12. 원래 Notion 명세의 뉴스 입력 모델 (2026-09-15 완료)

사용자가 실제 Notion 명세를 보여 주었다. **뉴스 내용에 따른 방향·강도 예측은 원래 필수 기능**이며,
U3가 이를 대체한다고 설명한 것은 범위 해석 오류였다. 다음 세 작업을 새 파이프라인으로 완료했다.

1. 뉴스×기업 1/3거래일 라벨과 금융 감성 규칙·그래프 없는 LightGBM.
2. 동결 뉴스 임베딩·언급 특징을 직접 노드에 주입하는 PyG 2층 GATv2, 방향·강도 다중 과제,
   3시드 앙상블. 시드별 val 선택 epoch 5/2/1(0 기반).
3. 요청 시 그래프 근거 검색 + 실제 로컬 LLM의 근거 문장 선택·인용, 뉴스 입력 FastAPI.

산출물 `artifacts/news_impact/`, 모델 버전 `abc4be78e5933b99`. 코드와 계약은
`README_NEWS_IMPACT.md`, 서버는 `serve_news_impact.py`(localhost 8092).
기존 U3 8091 API는 별도 실험으로 보존. Spring/프론트 연결·운영 배포는 미실행.

HDFS에서 연도·국내외별 최대 1,500건을 결정적 표집해 32,621건 추출. 최종 **기사 29,835건 /
기사×기업 844,353쌍**, train 2017~2022 / val 2023 / test 2024~2026-09-11.
날짜만 있어 보수적인 가용 시점 **이후 첫 종가**부터 +1/+3세션을 잰 지연 반응 라벨이다.
즉시 반응이나 뉴스 하나의 인과 효과가 아니다. 공식 시장 지수 대신 동일 시장의 다른
유니버스 종목 동일가중 수익률을 제거했다. 라벨 종료가 분할 경계를 넘으면 제외한다.

| 시장·기간 | 다수 클래스 정확도 | 감성 규칙 | LightGBM | GAT |
|---|---|---|---|---|
| KOSPI 1일 | 43.07% | 28.81% | 41.92% | 41.91% |
| KOSPI 3일 | 43.35% | 28.99% | 41.89% | 42.43% |
| NASDAQ 1일 | 36.84% | 31.44% | 35.27% | 37.07% |
| NASDAQ 3일 | 35.74% | 31.32% | 32.19% | 35.81% |

공통 test 표본 KOSPI 152,253쌍 / NASDAQ 122,685쌍. **학습·서빙은 가능하지만, 방향 정확도가
충분하다고 볼 근거는 없다.** NASDAQ의 LightGBM 대비 개선을 다수 클래스 대비 개선으로
바꿔 말하지 말 것. 세션·HAC 비교, IC와 강도 지표는 README/평가 CSV 참고. test로 재선택하지 않았다.

추가 함정과 해결:

- 과거 기사 본문에 수집 시점 주가 위젯이 끼어 있었다. 첫 학습은 중단·격리하고
  `news_content.py`로 주가 위젯·삽입 뉴스·사이드바를 제거해 재학습. 전처리 적용 3,593건.
- 현재 sector/ownership를 과거에 소급하지 않음. 역사 구간에서는 미상 속성을 마스킹하므로
  이 두 유형의 역사적 효과까지 검증한 것은 아니다. 상관은 직전 연도 가격으로만 구성.
- 정확한 달력은 exchange_calendars: NASDAQ DST·조기 마감·과거 KRX 마감시간을 처리.
- 자유 생성 LLM이 2021 MOU와 2024 기사를 섞었다. 현재 GraphRAG는 **추출형**이며 생성 인용이
  원문에 실제 존재할 때만 반환. 인과 전망이나 별도 수치를 생성하지 않는다.
- 상수 예측 모델이 IC 평가에서 어려운 기사를 탈락시키지 않도록, 공통 기사 집합의 순위
  정보 점수(상수=0)를 통상 Spearman IC와 별도 기록. 비교 시 표본 수를 반드시 확인.

검증: 회귀 테스트 20개 통과, 서버/로컬 멘션 표본 201건 일치, CPU 재로딩 12조건 최대 오차 8.64e-7,
수익률·분할·그래프·확률/강도 범위 감사, 기사 내용 민감도와 위젯 변화 불변 확인.
실제 HTTP 일반 예측 warm 중앙값 141ms, 설명 생성 40.8초(단일 사례). 설명 전후 GNN 점수
동일. 테스트 서버는 종료. 재현 가능한 코드·가중치·전처리 설정·근거와 감사 로그를 보관했다.

앞으로 남은 것은 **방향 예측력 개선 연구**, 발행 시각/역사적 기업·관계 자료 보강,
그리고 Spring/프론트 연동·운영 배포다. 현재 모델을 정확도가 입증된 투자 예측 서비스로 소개하지 않는다.
