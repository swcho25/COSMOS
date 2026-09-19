# 과거 뉴스 전체 이력의 서버 배치

**2026-09-17 상태 정정:** 아래 원래 실행은 Spark Java heap OOM 후 진행이 멈춰 있었다.
완료한 142/4,600파일·428,898행을 확인한 뒤 01:20:29 UTC에 정상 종료했다.
SQLite 무결성과 체크포인트 보존을 확인했다. 원래 후속 분석·게시 단계는 재개하지 않는다.
정제/전역 ID만 복구하는 별도 실행 절차는 `README_LAKE_NORMALIZATION_RECOVERY.md`를 따른다.
기업별 경제 관련성 전체 후보 추출은 `README_KG_FULL_RESELECTION.md`의 별도 작업이다.

아래는 2026-09-16 17:14 KST에 서버에서 시작한 원래 실행의 구성 기록이다. 현재 상태는 위 정정을 따른다.
사용자가 PC를 종료할 예정이라 전체 과거 처리를 서버 백그라운드에 맡기도록 요청했다.
로컬 PC나 SSH 연결 없이 서버에서 순차 실행한다. 정기 갱신 작업은 만들지 않았다.

## 실행 식별자

- 서버: `ubuntu@j15c205a.p.ssafy.io`
- 실행 ID: `lake-news-history-20260916-v1`
- 작업 폴더: `/home/ubuntu/ner-work/lake_backfill_20260916_v1`
- 고정 코드: `/home/ubuntu/ner-work/lake_backfill_20260916_code_v1`
- 전용 Python: `/home/ubuntu/ner-work/lake_backfill_20260916_v1_runtime/bin/python`
- 최초 supervisor PID: `1531691` — 이후 확인 시 PID 파일과 실제 명령을 대조한다.
- 계획 SHA256: `6453cca4dd6ded5be28ebd20a5607a51d021e81f50e9dc0886dbf52d41e5e910`
- 입력 manifest SHA256: `13628123e83103c83bfd85861dd8b67e464ca5c2896ac7e863a871a611f89fc3`

최초 연결 종료 후 새 SSH 연결에서 PPID=1, SID=PID, nice=10,
CPU affinity `[0, 1]`을 확인했다. Spark는 `local[1]`, driver 4 GiB,
Parquet 비벡터 읽기를 사용한다. 분석 worker는 2개, 감성은 서버 CPU를 쓴다.
서버 재부팅까지 자동 복구하는 서비스는 아니며, 종료되면 아래 방식으로 재개한다.

## 처리 범위

입력 목록은 4,600개 Parquet 파일로 고정했다. 발행 시각이
`2026-09-16T00:00:00Z` 이전인 기존 뉴스 전체를 읽으며 시작일 제한이 없다.

- `/datasets/news/snapshots/20260908-prepared/news`
- `/datasets/news/live/mysql_overseas/data`
- `/data-lake/raw/realtime/news`의 완료한 운영 `news.raw` 배치

새로 도착하는 파일, 미완료 배치, 검증 fixture는 이번 고정 목록에 추가하지 않는다.
원천 행 수에는 중복·부적격 자료가 포함되어 있으므로 최종 기사 수와 같지 않다.
기존 90일 ID 매핑을 시드로 사용해 전역 중복을 해소한다. 기존 서로 다른 UUID가
하나로 충돌하면 임의로 바꾸지 않고 실패 기록을 남긴다.

1. 입력 파일 경로·크기·수정시각을 확정한다.
2. Spark로 파일별 읽기, 본문·발행시각 정제, 디스크 기반 중복·문서 ID 처리를 한다.
3. 정제 본문에서 기업과 한국어 규칙 관계를 다시 추출한다. 현재 계열사 관계를
   모든 과거 기사에 소급 적용하지 않는다.
4. 기업 언급 기사의 제목·첫 200자를 기존 한국어/영어 FinBERT로 분석한다.
   언어가 없거나 미지원이면 감성을 NULL로 남긴다.
5. Parquet의 건수·키·스키마·내용을 검증하고 새 실행 폴더로 HDFS에 게시한다.

정제 뉴스·언급·뉴스 관계·문서 ID·감성은 전체 뉴스 이력 범위다.
기업/관계 특징, 7D·30D·90D 집계와 그래프는 위 기준 시각의 기간별 결과다.
공시·가격은 완료한 최근 90일 배치의 고정 입력을 재사용한다.
**모든 과거 날짜의 그래프 스냅샷이나 공시 전체 이력 재분석을 생성하는 실행은 아니다.**
현재 사전으로 수행한 소급 분석이며 과거 시점에 관측 가능했던 정보라는 인증도 아니다.

GNN 학습·주가 방향 추론과는 별도다. 기존 GNN 번들, 서비스 DB, Spring/프론트는
변경하지 않는다. commit, push, MR도 하지 않았다.

## 상태 확인

서버에서 다음 파일을 확인한다.

```bash
cat /home/ubuntu/ner-work/lake_backfill_20260916_v1/status.json
cat /home/ubuntu/ner-work/lake_backfill_20260916_v1/pid.json
cat /home/ubuntu/ner-work/lake_backfill_20260916_v1/ingest_progress.json
```

`status.json`의 `stdout_log`가 현재 단계 로그다. 상태 문자열만 보지 말고
`pid.json`의 PID가 해당 작업 명령으로 살아 있는지도 확인한다.
단계별 상세 진행 파일은 다음과 같다.

| 단계 | 진행/결과 파일 |
|---|---|
| 입력 확정 | `inventory.done.json`, `source_manifest.json` |
| 정제·ID | `ingest_progress.json`, `source_receipts/`, `identity_summary.json` |
| 기업·관계 | `analysis/progress.json`, `analysis/analysis_manifest.json` |
| 감성·집계 | `finish_progress.json`, `backfill_publication.json` |
| 검증·게시 | `publish_progress.json`, `publish/receipt.json`, `publish.done.json` |

분석 단계는 `analysis/progress.json`의 최상위 `articles_processed` 등을 직접 읽는다.
supervisor의 `active_counts`에는 해당 분석기 형식이 반영되지 않아 비어 있을 수 있다.
정제 단계의 `source_rows`는 완료 파일 합계이며 `active_source_rows`는 현재 파일의
진행량이다. 실패 후 재개 시 두 값을 중복 합산하지 않는다.

디스크 보호는 정제기가 20 GiB 미만에서 중단하는 검사와 별도 일회성 guard로 구성한다.
guard는 30초마다 여유 공간을 확인하고 12 GiB 미만이면 이 supervisor에 SIGTERM을
보낸다. PID뿐 아니라 프로세스 시작 시각·명령·boot ID를 확인하며 다른 작업을 끄지 않는다.
`status/guard.json`이 guard의 상태와 여유 공간을 기록한다. 대상 작업 종료 시 guard도
종료하고 재시작이나 주기 작업을 예약하지 않는다. 순간적인 자원 고갈을 방지한다는
보장은 없으므로 실패 조사에서는 실제 PID와 디스크 여유를 함께 확인한다.

## 재개

먼저 이전 PID가 종료됐는지 확인한다. 동일 계획·코드·고정 입력만 재사용한다.
파일/분석 shard 단위 체크포인트를 검증하고 완료분을 건너뛴다. 진행 중이던
파일이나 shard는 새 시도 폴더에서 다시 처리한다. 실패한 결과를 완료로 취급하지 않는다.

```bash
nice -n 10 taskset -c 0,1 \
  /home/ubuntu/ner-work/lake_backfill_20260916_v1_runtime/bin/python \
  /home/ubuntu/ner-work/lake_backfill_20260916_code_v1/run_lake_backfill.py launch \
  --run-dir /home/ubuntu/ner-work/lake_backfill_20260916_v1 \
  --plan /home/ubuntu/ner-work/lake_backfill_20260916_v1/plan.locked.json
```

위 명령 자체가 분리 실행하므로 추가 `nohup`은 필요 없다.
코드/입력 해시가 달라졌다면 기존 계획을 덮어쓰지 말고 원인을 조사한다.
원본 삭제, 이전 결과 덮어쓰기, DB 적재를 복구 수단으로 사용하지 않는다.

재개한 supervisor는 PID가 바뀐다. `pid.json`의 새 PID를 확인해 별도 guard도 다시 실행한다.

```bash
/home/ubuntu/ner-work/lake_backfill_20260916_v1_runtime/bin/python \
  /home/ubuntu/ner-work/lake_backfill_20260916_ops_v1/guard_lake_backfill.py launch \
  --run-dir /home/ubuntu/ner-work/lake_backfill_20260916_v1 \
  --pid <새_supervisor_PID>
```

## 완료 판정과 검증 기록

이번 실행이 완료되면 기존 정식 HDFS 각 root 밑의
`run_id=lake-news-history-20260916-v1`에 새 결과가 생긴다.
뉴스 언급과 전체 감성은 `document_id`로 결합한다.
이전 `lake-recent90-20260916-v1` 결과는 보존한다.

최종 판정은 로컬 `publish/receipt.json`과 다음 전역 완료 표식이다.

```text
/data-lake/metadata/runs/run_id=lake-news-history-20260916-v1/_VERIFIED
```

이 표식 전에는 일부 결과 경로가 보여도 전체 완료라고 말하지 않는다.
완료한 실행 하나를 골라 읽어야 하며 여러 run을 통째로 합치면 중복된다.

실행 전 Linux 서버에서 상태 저장·분석·분리 실행/종료/재개 테스트 31개 통과.
게시기 단위 테스트 9개 통과. 실제 한국어/영어 기사 4개로 CPU 감성까지 처리한 뒤
검증 전용 HDFS 경로에 13종/17shard/5,196행을 저장·재읽기 검증했다.
이는 파이프라인 동작 확인이며 전체 이력 완료나 추출 품질 인증이 아니다.

검증 실행: `lake-backfill-smoke-20260916-v1`, 서버 폴더
`/home/ubuntu/ner-work/lake_backfill_20260916_smoke_v1`.
로컬 코드 해시·실행 기록은 `artifacts/lake_backfill_20260916/`에 보존한다.
