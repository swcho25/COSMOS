# 전체 기업 언급 후보의 지식그래프 관련성 재선별

2026-09-17. 기존 기업명 연결 후보를 Spark로 준비하고, 별도 GPU 워커가 기사×기업별
경제적 관련성을 다시 판정하는 작업이다. Spark 후보 준비와 LLM 판정은 별도 단계이며,
준비 파일 생성만으로 전체 뉴스 선별이나 지식그래프 반영이 끝난 것은 아니다.

## 이번 전체의 범위

원천은 변경하지 않는 아래 완료본이다.

- 뉴스: `/datasets/news/snapshots/20260908-prepared/news`
- 기업 연결: `/data-lake/sandbox/news/junwoo/company-mentions/model_version=dict-v1.3/`
- 사용 run: `dict-v1.3-domestic-20260911-0655`, `dict-v1.3-overseas-20260911-0655`

두 NER 완료 manifest의 분모는 다음과 같다. 이번 Spark export의 실제 조인·고유 쌍 건수는
완료 후 새 `counts.json` 및 `manifest.json`으로 다시 대조한다.

| 구분 | 기업 언급 기사 | 기사×기업 후보 |
|---|---:|---:|
| 국내 | 1,156,420 | 2,082,695 |
| 해외 | 341,892 | 574,111 |
| 합계 | 1,498,312 | 2,656,806 |

한 기사에 기업 두 곳이 등장하면 두 번 판정한다. 1,498,312개 기사가 곧 1,498,312번의
모델 호출이라는 뜻이 아니다. 각 쌍도 첫 판정에 따라 확인 판정을 추가하므로 호출 수가 늘어난다.

원래 NER는 `text_eligible=true`, `is_body_representative=true` 자료에 실행된 결과다.
이번 재선별은 그 기존 후보 전체를 사용하며 연도·학습 구간·기업 목록·신뢰도·스포츠·키워드
조건을 추가하지 않는다. 기존 NER가 못 찾은 기업, 기업이 특정되지 않은 산업 기사,
고정 snapshot 이후 실시간으로 들어온 자료는 이번 범위에 없다. 따라서 원시 뉴스 1,300만 건
전체를 새로 읽고 모든 기업 후보를 발굴하는 작업과 구분해야 한다.

## 처리 단계와 완료 기준

1. **서버 Spark 후보 준비** — 같은 원천의 기업 언급과 기사 원문을 조인하고 기존
   `news_content.sanitize_article`로 위젯·삽입 헤드라인 등을 정제한다. 임의 길이로 본문을
   자르지 않으며 원시 발행 시각·URL과 원문 해시를 보존한다. 날짜만 있는 자료를 임의 UTC
   시각으로 바꾸거나, 최신 회사 지식으로 과거 법인 관계를 보충하지 않는다.
2. **입력 배포** — 입력 shard와 완료 manifest를 묶고 서버·로컬 양쪽에서 해시를 검증한다.
   다운로드하거나 압축을 푸는 것은 판정 결과를 만드는 작업이 아니다.
3. **GPU 경제 관련성 판정** — 기존에 받은 Qwen 가중치를 프로세스당 한 번 읽고 쌍별로
   `include`, `exclude`, `review`를 저장한다. 가격 예측용 신규성·미래 수익률 조건은 적용하지 않는다.
4. **결과 검증과 별도 그래프 구성** — 모든 입력 쌍 처리와 해시·행수·고유 분모가 일치해야
   워커 완료 영수증이 나온다. `include` 연결만 별도 뉴스→기업 그래프 후보로 사용할 수 있다.

기업 A와 B가 모두 채택됐다는 이유만으로 두 기업 사이의 공급·경쟁·제휴 관계를 만들 수는 없다.
회사 간 관계 유형·방향에는 추가 근거가 필요하다. 현재 결과는 `human_gold=false`,
`ready_for_gnn_training=false`, `production_approved=false`이며 운영 서비스·기존 집계를 덮어쓰지 않는다.

## 구현과 입출력

- `kg_candidate_export.py`: 날짜·정제·canonical 입력 SHA·전량 분모 계약을 검사하는 순수 함수.
- `spark/export_kg_candidates.py`: 분산 조인과 JSONL shard 생성. 원천 inventory·완료 marker를
  처리 전후 검사한다. 새 sandbox 출력만 허용한다.
- `knowledge_graph_batch_inference.py`: 고정 모델·정책을 재사용하는 제한된 크기의 GPU 추론.
- `knowledge_graph_reselection_worker.py`: shard를 스트리밍하고 SQLite transaction에
  결과·원문 SHA·입력 cursor를 함께 저장한다. 중단 후 동일 입력·설정으로 `--resume`할 수 있다.
- `transfer_kg_reselection.py`: 서버 완료 대기·검증·다운로드·추론 실행 연결 도구.
  **이 도구를 실행하면 준비 완료 뒤 GPU 워커도 실행되므로 단순 다운로드 명령으로 사용하지 않는다.**
- `split_kg_reselection_queue.py`: 검증한 준비 입력을 두 PC의 겹치지 않는 shard로 분배한다.
  각 PC의 부분 작업 분모와 전체 부모 manifest를 보존한다. 분할 완료는 추론 완료가 아니다.

Spark 출력은 아래 경로의 새 시험 run이다.

`/data-lake/sandbox/news/junwoo/kg-reselection/full-dict-v13-20260917-v1`

| 파일/폴더 | 내용 |
|---|---|
| `protocol.json` | 원천 파일·완료 manifest·체크섬·코드·모델 receipt 고정 정보 |
| `counts.json` | 원천 행수, 기업 언급 수, 고유 쌍 수, 누락 조인·충돌 검사 |
| `inputs/part-*.txt` | 최대 1,000행의 JSONL 입력 shard; 아직 분류 결과가 아님 |
| `provenance/` | 쌍별 입력 SHA와 정제 기사 SHA |
| `source_provenance/` | 원시 시각·URL·본문 해시·원천 경로와 정제 기록 |
| `mention_provenance/` | 원래 기업명 매칭 근거·신뢰도·스포츠 표기 등 |
| `manifest.json`, `_VERIFIED` | 모든 파일 검증 후 게시되는 전체 export 완료 증거 |
| `failure.json` | 실패 시 확인할 원인과 그때까지의 건수 |

각 입력은 `article_id`, `ticker`, `company_name`, `published_at`, `title`, `body`,
`source_url`의 7필드다. 여기서 `article_id`는 준비본의 `record_id`다.
제외·보류도 최종 모델 출력에 보존하며, 본문이 길어 모델에 전체를 전달할 수 없거나 출력이
잘린 경우는 보류한다. 실패·누락 행을 제외로 위장하지 않는다.

누락 원문 조인, 동일 ID의 상충하는 원문, 같은 기사×종목에 상충하는 기업명, 미해결 기업명,
정제 후 완전히 빈 기사가 있으면 export는 전체 완료를 거부한다. 단순히 해당 행을 버리고
분모가 줄어든 파일을 전체 처리 결과라고 게시하지 않는다. 완전히 같은 canonical 입력만 중복을 제거한다.

worker manifest에는 입력 파일만 `files:[{path,sha256,rows,bytes}]`에 넣고,
`expected_unique_pairs`로 최종 고유 쌍 분모를 고정한다. 파일 경로는 manifest 상대 경로다.
입력 배포는 manifest·protocol·marker·각 파일 해시를 검사하며 경로 탈출, 링크·중복 tar 항목을 거부한다.
동시 실행은 OS 잠금으로 차단한다.

## 현재 실행 상태

2026-09-17 10:34 KST 확인 시 서버의 후보 준비 작업은 `EXPORTING`이었다.

- 서버 작업 폴더: `/home/ubuntu/ner-work/kg_reselection_20260917_v1`
- 감독 프로세스: 1782010, Spark 프로세스: 1782289
- Spark: `local[2]`, driver memory 3 GiB, shuffle 64개, shard 최대 1,000행
- 실제 서버 Java/Hadoop 스트리밍 해시 및 합성 데이터 Spark 함수 smoke 검사 통과
- 확인 시점 실행 로그에 오류는 없었고 Spark RSS 약 1.19 GB, 서버 여유 디스크 약 116 GB

이 상태는 특정 시점 관측이며 최신 상태는 서버 `status.json`과 `exporting.log`를 확인한다.
`READY_FOR_DOWNLOAD`는 준비 입력이 포장됐다는 뜻이며, 선별 결과가 완료됐다는 뜻이 아니다.
전체 GPU 추론은 아직 시작하지 않았다. 과거 lake backfill 복구 작업과 이 신규 후보 준비는
서로 다른 작업이며, 멈춰 있던 과거 작업의 부분 결과를 이번 완료본으로 사용하지 않는다.

## 속도 실측과 GPU 두 대의 의미

같은 알려진 기사×기업 32쌍으로 실행 방식만 비교했다. 모두 채택 19·제외 4·보류 9였고
선택·관계 값이 기존 실행과 같았다. 새로운 평가 표본이나 의미 정확도 향상을 입증한 결과는 아니다.

| 실행 방식 | 순수 추론 구간 | 모델 로딩 포함 전체 |
|---|---:|---:|
| 기존 파일럿 직렬 실행 | 323.105초 | 378.476초 |
| 새 워커 추론 코드, batch 1 | 282.128초 | 311.651초 |
| 새 워커 추론 코드, batch 2 | 505.492초 | 533.983초 |

현재 RTX 4050 6 GB에서는 batch 2가 더 느렸으므로 batch 크기를 늘리면 빨라진다고 볼 수 없다.
실험 파일은 `artifacts/kg_full_reselection_20260917_v1/batch{1,2}_benchmark/`에 있다.

batch 1의 관측 속도를 2,656,806쌍에 그대로 적용하면 **약 271 GPU일**이다. 이는 작업 규모를
설명하는 단순 환산이며 완료 예정일이 아니다. 실제 전체 입력의 길이·보류율·GPU 상태·I/O와
중단 시간에 따라 달라진다. Spark 후보 준비 시간과도 별개다.

동일 성능 GPU 두 대에 작업을 완벽히 나눠도 단순 환산은 약 136일이다. 사용자는 두 번째 노트북도
**RTX 4050, 6,141 MiB(약 6 GB)**라고 확인했다. 두 대의 전력 설정·실제 속도·사용 가능 시간,
연결된 컴퓨터와 데이터 접근 방식이 확인돼야 현실적인 처리량을 측정할 수 있다.
독립 입력 shard를 GPU마다 별도 작업 폴더와 SQLite로 처리한 뒤 동일 고정 정책·입력 해시로 합쳐야 한다.
현재 워커의 SQLite 한 개를 여러 GPU가 동시에 쓰는 방식은 지원하지 않는다.

두 번째 PC용 코드·동일 비교32쌍·모델 원본 receipt는
`artifacts/kg_full_reselection_20260917_v1/second_pc_code_and_benchmark.zip`에 묶었다.
내부 `README_KO.md`에 별도 환경 설치, 기존 약8GB 모델 파일 복사, 비교 실행 명령이 있다.
다른 PC의 모델 위치는 `COSMOS_RELEVANCE_MODEL_DIR`로 지정하며 원본 receipt를 수정하지 않는다.
두 번째 PC 설치·모델 전송·실제 실행은 아직 하지 않았다. 준비 입력의 정책/근거 코드와
모델 receipt가 로컬 실행과 다르면 GPU를 로딩하기 전에 중단한다.

portable 경로/검증 보강 이전의 속도 시험 코드는
`artifacts/kg_full_reselection_20260917_v1/benchmark_code_before_portable_fix/`에 보존했다.
모델·프롬프트·양자화·정밀도 설정과 이전 benchmark 출력은 변경하지 않았다.

전량 LLM 판정을 장기간 실행하기 전, 적은 비용의 1차 관련성 모델과 어려운 사례용 LLM을 나눌지,
더 빠른 GPU 환경에서 기존 기준을 실행할지 실제 비용·품질을 비교해야 한다. 어떤 방식으로 바꾸든
기존 실패 사례와 새 표본을 통해 광고 오채택·다른 법인 귀속·경제 정보 누락을 다시 확인해야 한다.
