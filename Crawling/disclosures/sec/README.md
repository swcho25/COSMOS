# 해외 공시: SEC EDGAR 배치 수집기

Nasdaq의 현재 Nasdaq-100 구성증권을 SEC CIK와 연결한 뒤 기업정보와 공시 원문을 받는다.
같은 회사의 복수 주식종류는 CIK 기준으로 통합한다. `2016~2026` 같은 기간을 지정해도
**실행 시점의 구성기업에 대한 과거 공시**를 수집한다. 각 연도의 당시 구성종목을 복원하는
프로그램은 아니다.

기존 운영 이력은 해외 공시 **16,133개 원문 파일의 배치 수집·HDFS 스냅샷 적재 완료**다.
현재 코드는 최신 공시를 계속 감시하는 상시 수집기나 Kafka producer를 포함하지 않는다.
이 소스를 배포하는 것만으로 자동 수집이 시작되지는 않는다. HDFS 보조 스크립트는 로컬
배치 결과를 직접 업로드·검증하며, 데이터·완료 manifest·접속키는 이 디렉터리에 포함하지 않는다.

## 실행 환경

- Python **3.11 이상**. 수집·재개·테스트는 표준 라이브러리만 사용한다.
- 수집 시 Nasdaq와 SEC HTTPS 접근이 필요하다.
- HDFS 업로드·검증은 Hadoop client가 설정된 Linux에서 Bash, GNU coreutils, Java와 함께 실행한다.
- 아래 명령은 저장소의 `Crawling/disclosures/sec` 디렉터리에서 실행한다.

`requirements.txt`에는 설치할 외부 Python 패키지가 없다.

```bash
python3 --version
python3 scripts/collect_sec_edgar_nasdaq100.py --help
python3 scripts/resume_sec_edgar_from_manifest.py --help
```

## 환경변수

`.env.example`은 형식 예시다. 프로그램은 `.env` 파일을 자동으로 읽지 않는다.
실행 환경에서 `SEC_EDGAR_USER_AGENT`에 서비스명과 운영자의 실제 연락 이메일을 설정한다.
예시 이메일 `operator@example.com`은 그대로 실행하지 못하도록 검증한다.

```bash
export SEC_EDGAR_USER_AGENT='YOUR_APPLICATION/1.0 YOUR_REAL_CONTACT_EMAIL'
```

PowerShell에서는 `$env:SEC_EDGAR_USER_AGENT = 'YOUR_APPLICATION/1.0 YOUR_REAL_CONTACT_EMAIL'`을 사용한다.
실제 연락처나 비밀 파일은 Git에 추가하지 않는다.

## 새 배치 수집

기본 실행은 기업별 최신 핵심 공시 후보를 선택한 후 전체 최신순 최대 100건을 받는다.
기본 양식은 `10-K`, `10-Q`, `8-K`, `20-F`, `40-F`, `6-K`이며 수정신고서(`/A`)도 포함한다.

```bash
python3 scripts/collect_sec_edgar_nasdaq100.py \
  --out-dir output/sec-edgar-latest
```

기간 전체를 수집하려면 두 제한을 모두 `0`으로 지정한다. 아래 날짜는 예시이므로 원하는
시점으로 바꾼다. `--metadata-only`는 먼저 대상 목록을 확인할 때 사용한다.

```bash
python3 scripts/collect_sec_edgar_nasdaq100.py \
  --start-date 2016-01-01 --end-date 2026-09-15 \
  --per-company 0 --total-limit 0 --metadata-only \
  --out-dir output/sec-edgar-preflight

python3 scripts/collect_sec_edgar_nasdaq100.py \
  --start-date 2016-01-01 --end-date 2026-09-15 \
  --per-company 0 --total-limit 0 \
  --out-dir output/sec-edgar-batch
```

모든 신고 양식이 필요하면 `--forms all`을 추가한다. 기본 요청 간격은 0.25초이며
`--request-delay`는 0.1초 이상이어야 한다. 다운로드 작업 4개가 하나의 속도 제한을 공유한다.
이 제한은 한 프로세스 안에서 적용되므로 여러 수집기를 동시에 실행할 때는 총 요청량을
별도로 관리해야 한다.

출력 경로는 `--out-dir`로 지정하며 상대경로는 현재 작업 디렉터리 기준이다.

```text
output/sec-edgar-batch/
├── README.md
├── companies.json
├── selected_filings.jsonl
├── filings.jsonl
├── failures.jsonl
├── manifest.json
└── raw/<기업순번_티커>/<접수일_양식_ACCESSION>.txt
```

`filings.jsonl`에는 확보한 원문의 상대경로·바이트 크기·SHA-256이 들어간다.
기본 수집기는 기존 파일이 100바이트 이상이면 재사용하므로, 엄격한 무결성 검사가 필요하면
아래 manifest 기반 재개를 사용한다. 실행 중단 직전의 일부 파일은 메모리 내 목록만 갱신된
상태일 수 있으므로 같은 수집 명령 재실행과 manifest 재개의 역할을 구분한다.

## 기존 manifest에서 원문 복구·검증

기존 `filings.jsonl`과 기업정보·선택 목록 등 배치 메타데이터를 별도로 복원한 폴더를 지정한다.
재개기는 manifest에 있는 파일의 크기와 SHA-256을 확인하고 없거나 손상된 파일만 다시 받는다.
`selected_filings.jsonl`만으로는 재개할 수 없으며, manifest에 없는 새 공시를 발견하지 않는다.

```bash
python3 scripts/resume_sec_edgar_from_manifest.py \
  --root output/sec-edgar-batch --workers 4 --request-delay 0.2

# 같은 작업의 Linux wrapper. 임의의 현재 작업 디렉터리에서도 실행 가능하다.
bash scripts/run_sec_edgar_resume_remote.sh output/sec-edgar-batch --workers 4
```

wrapper는 `SEC_EDGAR_USER_AGENT` 대신 `SEC_EDGAR_USER_AGENT_FILE`로 지정한 연락처 파일을
읽을 수도 있다. 이 파일은 자동 삭제하지 않는다. 결과는 `resume-report.json`에 남으며
실패가 있으면 종료 코드 `1`을 반환한다.

## HDFS 스냅샷 업로드·검증

Hadoop client가 기존 클러스터를 가리키도록 `HADOOP_CONF_DIR`·`JAVA_HOME`을 설정한다.
`HDFS_BIN`을 생략하면 `${HADOOP_HOME:-/opt/hadoop}/bin/hdfs`를 사용한다. 서버 주소, SSH 키,
사용자 홈 경로, 특정 스냅샷 이름이나 파일 개수는 코드에 고정하지 않았다.

```bash
export HDFS_BIN=/opt/hadoop/bin/hdfs
export HADOOP_CONF_DIR=/opt/hadoop/etc/hadoop
export JAVA_HOME=/path/to/your/jdk

bash scripts/load_sec_edgar_hdfs_remote.sh \
  output/sec-edgar-batch /datasets/sec-edgar/snapshots/YOUR_SNAPSHOT.inprogress
bash scripts/verify_sec_edgar_hdfs_remote.sh \
  output/sec-edgar-batch /datasets/sec-edgar/snapshots/YOUR_SNAPSHOT.inprogress
```

업로드는 기존 대상 경로를 덮어쓰지 않고, 수집 결과 외 파일이나 심볼릭 링크가 있으면 거부한다.
파일·바이트 개수는 해당 로컬 배치에서 계산한다. 검증은 경로별 크기, 메타데이터 SHA-256,
전체 원문을 연결한 SHA-256, HDFS `fsck`를 확인한다. 전체 원문을 다시 읽으므로 데이터
크기에 따라 시간이 걸린다. 검증 중에는 로컬 배치와 HDFS 스냅샷을 수정하지 않는다.

두 보조 스크립트는 `_SUCCESS` 생성, 완료 경로로 이름 변경, 기존 데이터 삭제를 자동 수행하지
않는다. 검증이 성공한 배치의 공개는 운영 절차에 따라 별도로 진행한다. `_SUCCESS`가 있는
완료 스냅샷을 검증할 때도 같은 검증 스크립트를 사용할 수 있다.

## 오프라인 테스트

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n scripts/load_sec_edgar_hdfs_remote.sh
bash -n scripts/verify_sec_edgar_hdfs_remote.sh
bash -n scripts/run_sec_edgar_resume_remote.sh
```

테스트는 합성 메타데이터와 임시 파일을 사용한다. 인터넷·실제 공시·HDFS·운영 인증정보에
접근하지 않는다. 테스트 통과가 SEC 접속이나 운영 HDFS 적재를 대신 검증하지는 않는다.

공식 출처: [Nasdaq Global Index Watch](https://indexes.nasdaq.com/Index/Weighting/NDX),
[SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces),
[SEC Developer Resources](https://www.sec.gov/about/developer-resources).
