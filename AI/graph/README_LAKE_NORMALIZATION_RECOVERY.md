# 과거 뉴스 정제 배치의 제한 메모리 복구

`recover_lake_normalization.py`는 기존 과거 전체 배치에서 **정제와 전역 문서 ID 생성까지만** 복구한다. 기업 관련성 판단, 관계 생성, 감성 추론, HDFS 결과 게시, 서비스 DB 적재는 실행하지 않는다. 관련성 필터를 적용하기 전에 기존 배치의 후속 게시 단계를 자동 재개하지 않는다.

## 2026-09-17 실제 복구 실행 상태

- 새 run: `/home/ubuntu/ner-work/lake_backfill_recovery_20260917_v1`
- 새 code: `/home/ubuntu/ner-work/lake_backfill_recovery_20260917_code_v1`
- 새 runtime: `/home/ubuntu/ner-work/lake_backfill_recovery_20260917_runtime` (`requests=2.34.2`, `pyarrow=25.0.1`). 원래 runtime은 변경하지 않았다.
- 완료한 114행 파일의 probe는 원래 Spark 압축 payload SHA 및 건수가 완전히 일치했다. 최대 RSS는 83,472KiB였다.
- 기존 OOM 파일 42,238행의 probe는 34.99초에 완료했다(약 1,207행/초, SQLite 적재를 제외한 읽기·정제 측정). 최대 RSS는 182,952KiB였다.
- 실제 파일 한 개 제한 적재도 통과했다. SQLite 체크포인트 **143개 / 471,136행**, 상태 `PAUSED`, `quick_check=ok`, 최대 RSS **245,504KiB(약 240MiB)**를 확인했다.
- **2026-09-17 01:45:45 UTC(10:45:45 KST)**부터 전체 정제만 서버 백그라운드에서 재개했다. PID **1790883**, CPU affinity `[2]`, nice `10`, stdin 폐쇄 및 별도 세션으로 실행되어 로컬 PC와 독립적이다.
- 별도 SSH로 01:46:39 UTC에 PID의 명령·시작 tick·boot ID·CPU affinity·nice를 검증했다. 당시 **144개 / 471,531행 커밋**, 다음 파일 9,000행 처리 중, 상태 `RUNNING`이었다. 이는 그 시각의 진행량이며 전체 완료 건수가 아니다.
- 실행 영수증: 새 run의 `normalization_launch-1790883.json`, `normalization_launch-1790883.verified.json`.
- 진행 상태: 새 run의 `recovery_status.json`. 로그: `logs/normalization_full_20260917.log`.
- 실데이터 검증 영수증과 시간/RSS 기록: 새 code의 `recovery_ops/smoke.done.json`, `*.time.txt`.

## 확인한 중단 상태

- 원본 run: `/home/ubuntu/ner-work/lake_backfill_20260916_v1`
- 원본은 4,600개 파일 중 142개, 428,898행을 커밋한 후 Spark `TaskResultGetter`의 Java heap OOM으로 정체했다.
- 다음 파일: `/datasets/news/snapshots/20260908-prepared/news/region=domestic/year=2019/month=05/s3_kr_ssafy_0.parquet`
- 다음 파일 ID: `692acba7350e48fb690b0dd5`; 42,238행, 5개 row group, 파일 33,238,341바이트.
- 2026-09-17 01:20:29 UTC에 원래 supervisor가 `TERMINATED`로 종료했고, supervisor/Spark/guard PID가 모두 사라졌다. 종료 후 SQLite `quick_check=ok`, 142개 체크포인트와 428,898행이 그대로 확인됐다.

## 복구 계약

1. 기존 code/run/plan은 보존하고, 서로 겹치지 않는 **새 code와 새 run**을 사용한다.
2. `prepare`는 원본 supervisor lock을 잡고 종료 상태 및 알려진 프로세스를 확인한다. 원본 계획의 코드·입력 해시를 검증하고 SQLite backup API로 커밋된 상태를 복사한다. 원본 SQLite 파일과 WAL을 단순 복사하지 않는다.
3. 커밋 영수증, 기존 문서 ID 맵, 참조 중인 압축 payload 및 격리 기록을 복사하고 해시를 검증한다. 체크포인트가 없는 0바이트 미완료 파일은 완료 데이터로 가져오지 않는다.
4. HDFS에는 `GETFILESTATUS`와 `OPEN`만 호출한다. 한 Parquet 파일만 새 run의 임시 캐시에 받고 PyArrow `iter_batches`로 기본 512행씩 읽는다. 처리 후 이 호출에서 만든 캐시 파일만 제거한다.
5. 기존 `map_news`·`normalize_article`·`BackfillState` 코드를 그대로 사용한다. Hive 경로의 `region/year/month`, `source_path`, Spark UTC 세션의 timestamp 직렬화도 복원한다.
6. 파일마다 payload와 영수증을 먼저 fsync한 뒤 SQLite 체크포인트를 커밋한다. 중단된 파일은 전체 롤백하고 다음 실행에서 새 UUID payload로 재처리한다.
7. `recovery_prepared.json`에 코드·입력·PyArrow 버전을 고정한다. 재개 시 변경을 검출한다. 완료 결과는 `identity_summary.json`, `ingest.done.json`이며 `publish_enabled=false`다.

행 수 배치는 단일 비정상적으로 큰 행이나 Parquet dictionary의 크기까지 엄격하게 제한하지는 않는다. 따라서 아래 실제 파일 probe 및 첫 파일 제한 실행에서 최대 RSS를 확인해야 한다. 메모리를 줄이려고 기사 본문을 자르지는 않는다.

## 서버 실행 순서

아래는 이번 복구에 사용한 절차다. 현재 run은 이미 준비·검증·실행했으므로 같은 폴더에 `prepare`를 다시 실행하지 않는다. 새 복구를 만들 때는 새로운 run 이름을 사용한다. 새 code 폴더에는 원래 고정 code 폴더를 복사하고 새 `recover_lake_normalization.py`만 추가한다. 이 모듈의 6개 핵심 의존 파일이 원래 계획의 해시와 일치하지 않으면 준비를 거부한다. 기존 파일을 최신 로컬 버전으로 임의 교체하지 않는다.

별도 복구 Python runtime에는 기존 의존성 외에 `pyarrow==25.0.1`이 필요하다. 원래 runtime에는 `requests`가 있지만 PyArrow는 없었다. 기존 runtime을 변경하지 않고 새 runtime을 사용한다. 이번 NameNode HTTP 주소는 서버 설정에서 `http://100.117.115.44:19870`으로 확인했다.

```bash
RECOVERY_PY=/home/ubuntu/ner-work/lake_backfill_recovery_20260917_runtime/bin/python
CODE=/home/ubuntu/ner-work/lake_backfill_recovery_20260917_code_v1
RUN=/home/ubuntu/ner-work/lake_backfill_recovery_20260917_v1
OLD=/home/ubuntu/ner-work/lake_backfill_20260916_v1
NN_HTTP=http://100.117.115.44:19870

"$RECOVERY_PY" "$CODE/recover_lake_normalization.py" prepare \
  --resume-run "$OLD" --run-dir "$RUN" --batch-size 512 --minimum-free-gb 20

# 이미 완료한 마지막 파일: 새 reader의 압축 payload SHA와 원래 Spark 결과 비교.
/usr/bin/time -v taskset -c 2 "$RECOVERY_PY" "$CODE/recover_lake_normalization.py" probe \
  --run-dir "$RUN" --webhdfs-url "$NN_HTTP" --hdfs-user ubuntu \
  --source-id dea00a498dc63d76645d1435

# 실패했던 다음 파일: SQLite를 변경하지 않고 전체 파일 읽기/정제 및 RSS 확인.
/usr/bin/time -v taskset -c 2 "$RECOVERY_PY" "$CODE/recover_lake_normalization.py" probe \
  --run-dir "$RUN" --webhdfs-url "$NN_HTTP" --hdfs-user ubuntu \
  --source-id 692acba7350e48fb690b0dd5

# 기존 142개는 검증 후 건너뛰고 미완료 파일 한 개만 커밋하여 PAUSED.
/usr/bin/time -v taskset -c 2 "$RECOVERY_PY" "$CODE/recover_lake_normalization.py" normalize \
  --run-dir "$RUN" --webhdfs-url "$NN_HTTP" --hdfs-user ubuntu --max-files 1

# 메모리·동등성·체크포인트 결과 확인 후 전체 정제 재개. 동일 인자를 재사용한다.
taskset -c 2 "$RECOVERY_PY" "$CODE/recover_lake_normalization.py" normalize \
  --run-dir "$RUN" --webhdfs-url "$NN_HTTP" --hdfs-user ubuntu
```

`probe`는 source ID에 해당하는 파일 전체를 읽지만 SQLite에는 적재하지 않는다. 이미 커밋된 파일이면 압축 payload SHA와 건수를 비교하고 불일치 시 실패한다. 미완료 파일의 비교 결과는 `null`이다. 이는 비교할 기존 정답 payload가 없다는 뜻이다. 영수증은 새 run의 `probes/`에 남는다.

`normalize --max-files 1`은 **이번 실행에서 새로 처리한 파일 한 개**를 의미한다. 기존 완료 파일 142개는 제한에 포함되지 않는다. 원래 supervisor를 재개하지 않으며, 이 스크립트 자체도 자동으로 백그라운드화하지 않는다. 컴퓨터 종료와 독립된 서버 실행이 필요하면 별도 감독 프로세스가 stdin을 닫고 stdout/stderr를 서버 로그로 연결하여 새 세션으로 실행해야 한다.

## 로컬 검증

```powershell
& 'AI/ner/.venv/Scripts/python.exe' -X utf8 -m unittest discover -s AI/graph/tests -p test_recover_lake_normalization.py -v
```

합성 Parquet로 Hive 복원, timestamp/date/decimal/binary 직렬화, 기존 정규화 결과 및 문서 ID 동등성, 체크포인트 재개, 중간 예외/원본 변경 시 파일 전체 롤백, 기존 payload 변조 거부, 종료 상태 확인, probe 비적재 및 불일치 검출, WebHDFS 읽기 전용 동작과 과도한 다운로드 차단을 검사한다. 로컬 합성 테스트는 서버 실데이터 smoke를 대체하지 않는다.
