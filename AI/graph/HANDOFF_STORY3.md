# 스토리 3 (영향도) 인계 문서

작성 2026-09-14 · 브랜치 `S15P21C205-48` · Jira https://ssafy.atlassian.net/browse/S15P21C205-48

마무리 갱신 2026-09-14: §8-(A) 완료. 양 시장 재실행 결과는 §2-3, 검증 기록은 §10.

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

## 1. 결론 세 줄

1. **관계를 타고 다음 세션으로 전파되는 것은 0이다.** 이벤트 스터디로 확정.
2. **같은 세션 동조는 크고 확실하다.** 제품이 보여줄 것은 예측이 아니라 **귀속**이다.
3. **이번 비교에서 GNN 의 룩업테이블 대비 우위를 확인하지 못했다.** KOSPI 차이는
   유의하지 않고(t=-0.89), NASDAQ 은 표보다 낮다(t=-5.04). 같은 실행에서 측정 완료.

최종 산출물은 **영향도 계수표**다. 뉴스로 A 가 움직였을 때 `영향(B) = coef(A→B) × 움직임(A)`.

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

### (B) 사용자 결정 대기 중

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
