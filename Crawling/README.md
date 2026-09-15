# COSMOS 크롤러

국내·해외 뉴스와 국내·해외 공시 수집 코드를 이 폴더에서 관리한다. 뉴스는 공통 Kafka/HDFS 파이프라인으로 묶고, 수집 방식과 상태 관리가 다른 DART·SEC는 독립 실행 모듈로 구분했다.

| 폴더 | 포함 기능 | 실행 안내 |
|---|---|---|
| `news/` | 국내 뉴스·Yahoo Finance 수집, URL 중복 색인, Kafka 전달, HDFS Parquet Writer | [뉴스 README](news/README.md) |
| `disclosures/dart/` | OpenDART 수집·재개, 원본 ZIP 처리, HDFS 스냅샷 생성·검증·업로드 | [DART README](disclosures/dart/README.md) |
| `disclosures/sec/` | Nasdaq-100 SEC EDGAR 배치 수집·재개, HDFS 업로드·검증 | [SEC README](disclosures/sec/README.md) |

각 모듈의 README를 기준으로 해당 폴더에서 의존성을 설치하고 실행한다. 웹 FrontEnd/BackEnd의 패키지 설치나 이전 AWS RDS 연결을 공통 실행 조건으로 삼지 않는다. HDFS 기능은 별도로 Hadoop 클라이언트·Java와 클러스터 접근 설정이 필요하다.

## 데이터 흐름

```text
국내 뉴스 / Yahoo Finance
  → 로컬 영속 큐·outbox
  → Kafka news.raw
  → HDFS Parquet

OpenDART / SEC EDGAR
  → 수집 대상·진행 상태·원본 보존
  → 스냅샷 및 검증 정보
  → HDFS
```

공시 수집기는 현재 뉴스 Kafka 파이프라인과 별도다. 뉴스·공시의 원본 저장 이후 AI 분석, Spark 집계, PostgreSQL RDB Loader는 이 폴더의 기능에 포함하지 않는다.

## 운영 상태와 적용 범위

2026-09-15 점검 기준:

- 뉴스 수집기와 HDFS Writer는 Hadoop Master에서 동작 중이며 국내·해외 추가 적재를 확인했다.
- 뉴스는 최신 목록과 미수집 기사·과거 기간을 순환한다. 모든 속보를 발행 즉시 수집하는 방식은 아니다. HelloT는 기존 기사 번호 범위 수집이며 일부 `article_not_found` 재시도가 있다.
- DART 운영 재개기는 미해결 수집 오류로 `needs_attention` 상태였다. 코드가 저장소에 있다는 이유로 현재 전체 공시가 계속 수집 중이라고 판단하지 않는다.
- SEC는 저장을 완료한 배치 수집본이 있으며, 최신 공시를 지속 감시하는 스케줄러는 별도다.

이 변경은 소스와 실행 방법을 저장소로 분리한 것이다. 운영 서비스 재배포, 크롤러 재시작, 기존 큐·데이터 이관을 자동 수행하지 않는다. 실제 적용 시 기존 상태를 보존하고 각 모듈의 재개·중복 처리 절차를 따른다.

## 설정과 저장 데이터

- 인증정보는 환경변수 또는 서버의 별도 파일로 제공한다. `.env.example`에는 형식만 기록한다.
- SEC User-Agent의 연락처는 실행자가 실제 값으로 설정한다.
- 수집 원문, HDFS 스냅샷, SQLite 큐, 체크포인트, 로그, 인증서와 개인키는 Git에 포함하지 않는다.
- 뉴스의 생성된 JavaScript bundle과 배포 압축 파일도 Git에 넣지 않고 소스와 잠금 파일로 재생성한다.
- 수집 코드의 오프라인 테스트와 실제 원천 호출·HDFS 쓰기를 구분한다. 테스트 명령과 검증 범위는 각 README에 있다.

## 소스 출처

기존 `services/news_pipeline`, 국내 뉴스 추출 라이브러리, Yahoo 수집 모듈, DART·SEC 수집/업로드 스크립트를 필요한 의존성과 함께 분리했다. 출처가 있는 vendor 모듈은 해당 LICENSE와 출처 안내를 보존한다. 서로 다른 수집기의 CLI와 상태 파일 형식을 억지로 합치지 않아 기존 수집 재개 절차를 유지할 수 있다.
