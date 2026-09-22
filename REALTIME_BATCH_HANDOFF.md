# 실시간 배치에 이어 붙일 것 — AI 파이프라인 인수인계

작성 2026-09-22 · 박준우

지금 실시간 수집·매칭은 잘 돌고 있습니다. 여기에 **네 단계만 이어 붙이면** 뉴스가
화면까지 흐릅니다. 아래 순서대로 읽으시면 됩니다.

---

## 1. 지금 돌고 있는 것

```
cosmos-naver-news@{deliver,search}   10분   네이버 뉴스 수집 (일일 24,000건 한도)
cosmos-news-collector                상시   해외 크롤러
cosmos-news-hdfs-writer              2분    Kafka -> HDFS
cosmos-news-analysis                 1분    기사-기업 매칭 (dict-v1.3)
cosmos-document-loader               2분    새 문서를 Postgres 로
cosmos-dart-daily                    1시간  DART 공시
cosmos-sec-incremental               5분    SEC 증분
```

`cosmos-news-analysis` 가 쓰는 곳:
`/data-lake/analyzed/news/company-mentions/model_version=dict-v1.3/`

**여기서 멈춰 있습니다.** 기업 매칭까지만 하고 감성·관계는 없습니다.

---

## 2. 붙일 단계 — 주기를 둘로 나눠 주세요

| 주기 | 단계 | 쓰는 곳 |
| --- | --- | --- |
| **10분** | 근거 문장 추출 → 감성 → 관련도·영향도 | `public` 에 직접 |
| **하루 1~2회** | 관계 추출 → 새 run → `active_run` 전환 | 버전 테이블 |

관계는 10분마다 바뀌지 않습니다. "삼성전자가 SK하이닉스와 경쟁한다"를 10분마다
갱신할 이유가 없습니다. 반대로 기사·감성은 사용자가 바로 느끼는 부분이라 10분이
맞습니다.

---

## 3. 앞쪽 10분 배치는 이미 쉬워졌습니다 (V8 덕분)

V8 마이그레이션이 아래 네 뷰를 `active_run UNION public` 으로 바꿔 놨습니다.

```
current_source_document
current_news_article
current_disclosure
current_company_document     <- 감성·관련도·영향도가 여기 들어갑니다
```

즉 **10분 배치는 `public.company_document` 에 쓰기만 하면 화면에 뜹니다.**
스냅샷을 새로 만들 필요가 없습니다.

`relationship_*` 뷰는 V8 이 건드리지 않았습니다. 그래서 관계만 여전히 스냅샷
하나에 묶여 있습니다 (→ 6번 참고).

---

## 4. GPU 는 필요 없습니다

측정해 봤습니다.

```
하루 수집량        40~65MB   ≈ 기사 1.2만~1.7만건
  ↓ 기업 매칭 통과율 14.87% (실측)
쌍                 약 2,200건/일
  ↓ 근거 문장 1~2개
문장               약 3,000건/일
  ↓ CPU 11문장/초
소요               약 5분/일
```

10분 주기로 쪼개면 **한 번에 약 2초**입니다. 서버 CPU 로 충분합니다.

GPU(184문장/초)는 **밀린 물량 18만건을 한 번 따라잡을 때만** 필요하고,
그건 제가 로컬에서 별도로 처리합니다.

---

## 5. 반드시 지켜야 할 함정 넷

### ① FinBERT 라벨 순서가 모델마다 반대입니다

| 모델 | 0 | 1 | 2 |
| --- | --- | --- | --- |
| `snunlp/KR-FinBert-SC` | 부정 | 중립 | 긍정 |
| `ProsusAI/finbert` | **긍정** | 부정 | 중립 |

`argmax` 를 그대로 쓰면 **영어 기사 감성이 통째로 뒤집힙니다.** 호재가 악재가 됩니다.

모델 폴더 `config.json` 의 `id2label` 을 읽어 매핑하세요. 검증 함수가 이미 있습니다 —
`AI/graph/lake_sentiment.py` 의 `label_indexes()` 가 세 클래스인지, `label2id` 와
`id2label` 이 일치하는지 확인하고 아니면 예외를 던집니다.

**리비전 고정 필수.** 허깅페이스에서 같은 이름으로 갱신되면 순서가 또 바뀝니다.

```
snunlp/KR-FinBert-SC   f8586286cc3161fb648e9fee09a456069fd846d0
ProsusAI/finbert       4556d13015211d73dccd3fdd39d39232506f3e43
```

번들에 이미 들어 있습니다: `artifacts/news_impact_v2_bundle/models/sentiment_{ko,en}/`

문장에 한글이 있으면 한국어 모델, 없으면 영어 모델로 보냅니다.

### ② 귀속 가드를 직접 만들지 마세요

제가 만들었다가 통과율이 **2.5%** 까지 떨어졌습니다. 공백을 지우는 바람에 `삼성` 이
`삼성전자` 안에서 매칭됐습니다. 기존 함수를 쓰면 **92.6%** 입니다.

```python
from kg_target_sentiment import _surface   # 문장부호 안 지움, 영문 경계만 확인
```

규칙 셋:

- 문장에 대상 기업명이 없으면 버린다
- **같은 기사에 붙은 다른 기업**이 그 문장에 있으면 버린다 (전체 유니버스가 아니다)
- 한 기업에 대해 문장마다 라벨이 갈리면 **NULL**. 다수결로 밀지 않는다

### ③ 스팬 오프셋은 `title + "\n" + body` 기준입니다

본문만으로 문장을 자르면 **제목 길이만큼 전부 밀려서** 엉뚱한 문장이 근거로 붙습니다.

### ④ 부분 run 을 만들면 안 됩니다

관계 발행 시 **12개 버전 테이블을 전부** 채워야 합니다.

```
source_document · news_article · disclosure · company_document
document_evidence · company_relationship · relationship_evidence
relationship_score_current · relationship_score_history
company_metric_history · graph_snapshot · graph_snapshot_load
```

관계만 든 run 으로 전환하면 **뉴스 219,662건과 기업 연결 246,117건이 통째로
사라집니다.**

---

## 6. 먼저 확인해야 할 것 (백엔드)

지금 `/api/graphs/companies/*` 응답이 이렇습니다.

```
snapshotId  ab19d794-ad08-51b3-8a91-a060bcdfdafc
asOfAt      2026-09-16T00:00:00Z
```

**9/20 에 적재한 관계가 화면에 안 보입니다.** 백엔드가 9/16 스냅샷에 고정돼 있어서,
DB 에 행이 있어도 그 스냅샷에 안 붙으면 노출되지 않습니다.

여쭤볼 것:

> 새 `graph_snapshot` 을 만들고 `active_run` 을 전환하면 `/api/graphs/*` 가 그걸
> 바로 집나요? V8 이 문서 계열 뷰만 `public` 과 UNION 했고 관계 뷰는 건드리지 않아서,
> 관계는 여전히 스냅샷 하나에 묶여 있습니다.

**이게 안 풀리면 배치를 아무리 잘 만들어도 관계가 화면에 반영되지 않습니다.**
순서상 이것부터 확인하는 게 맞습니다.

---

## 7. 참고로 알아둘 것

### 7D 버튼이 비어 있는 이유

```
7D  관계 1개 · 30D 245개 · 90D 57개
```

확장·영문 배치가 `WINDOW = "30D"` 로 고정해서 7D·90D 행을 안 만들었습니다.
원래 설계(`relationship_score_components.py`)는 `WINDOWS = (7, 30, 90)` 으로 세 창을
다 만듭니다. 재처리 때 되돌릴 예정입니다.

다만 **7D 는 세 창을 다 만들어도 거의 빕니다.** 창이 `as_of` 기준 상대 기간이라
최근 7일 기사만 보는데, 지금 관계 추출 입력이 과거 아카이브 위주입니다.
**실시간 수집분이 관계 추출까지 흘러야 7D 가 채워집니다** — 이 배치가 그 역할입니다.

### 뉴스·공시 가중치 슬라이더가 안 먹는 이유

```
30D 245개 중   뉴스만 120 · 공시만 103 · 양쪽 다 22 (9%)
```

91% 가 한쪽 근거만 갖고 있어 가중치를 밀어도 값이 안 변합니다. 코드는 정상이고
데이터 문제입니다 (`blendScore` 는 한쪽만 있으면 있는 쪽을 그대로 씁니다).

---

## 8. 참고할 코드 — 단계별

전부 `AI/graph/` 아래입니다. **새로 만들 것은 거의 없고, 이어 붙이는 일입니다.**

### 근거 문장 추출

| 파일 | 역할 |
| --- | --- |
| `relevance_sentence_evidence.py` | 문장 분할 + 문자 오프셋. 원문 손실 없음 |
| `kg_target_sentiment.py` | `_surface()` — 귀속 확인 (감성에서도 씀) |

입력은 매칭 단계가 만든 `spans/` 의 `start`/`end` 입니다.
**오프셋 기준이 `title + 줄바꿈 + body`** 라는 점만 지키면 됩니다.

### 감성

| 파일 | 역할 |
| --- | --- |
| `infer_missing_sentiment.py` | **참고할 본보기.** FinBERT 2종 배치, 언어 분기, 만장일치 규칙 |
| `lake_sentiment.py` | `label_indexes()` — 라벨 순서 검증. 반드시 통과시킬 것 |
| `kg_target_sentiment.py` | `_surface()` — 귀속 가드 |

```bash
PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe infer_missing_sentiment.py --limit 2000   # 시험
```

`--limit` 으로 소량 먼저 돌려보시면 됩니다. GPU 가 있으면 자동으로 씁니다.

모델은 이미 받아져 있습니다: `artifacts/news_impact_v2_bundle/models/sentiment_{ko,en}/`

### 관련도 · 영향도

| 파일 | 역할 |
| --- | --- |
| `fill_company_document_scores.py` | `relevance_score` · `impact_score` · `confidence` 계산 |
| `measure_news_salience.py` | 주가 지도학습을 기각한 측정 (왜 규칙인지의 근거) |

```bash
PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe fill_company_document_scores.py --emit-sql
```

`--emit-sql` 이 적재용 SQL 을 내줍니다. 공식은 파일 머리말에 있습니다.

### 관계 추출 (하루 1~2회)

| 파일 | 입력 | 산출 |
| --- | --- | --- |
| `extract_relations.py` | 한국어 기사 | SUPPLY · PARTNER · COMPETE |
| `extract_relations_en.py` | 영문 기사 | 같음 (표본 20건 정밀도 95%) |
| `build_grounded_disclosures.py` | DART 공시 | INVEST |
| `spark/extract_relation_candidates.py` | 상류 — 두 기업이 같은 문장에 있는 후보 |

각 파일 머리말에 **실측으로 얻은 규칙과 함정**이 적혀 있습니다. 특히
`extract_relations_en.py` 헤더의 함정 다섯 가지는 네 번 고쳐서 얻은 것입니다.

### 관계 점수 · 신뢰도

| 파일 | 역할 |
| --- | --- |
| `relationship_score_components.py` | **점수 공식의 단일 출처.** `aggregate()` 가 7D/30D/90D 를 한 번에 낸다 |
| `data/relation_evidence/recompute_confidence.sql` | 신뢰도 재계산 |

**`aggregate()` 를 쓰세요.** 예전 배치들이 이 함수를 안 쓰고 `WINDOW = "30D"` 로 직접
만들어서 7D·90D 가 비어 있습니다.

### 적재 SQL (본보기)

| 파일 | 역할 |
| --- | --- |
| `data/db_expanded/load_into_analysis_run.sql` | 관계·점수 적재 |
| `data/relation_evidence/load_relation_evidence.sql` | 근거 연결. **⚠️ 주석의 알려진 결함 확인** |
| `data/db_expanded/rollback_expanded_relations.sql` | 되돌리기 |

모두 멱등합니다. 여러 번 돌려도 같은 결과입니다.

### 사전 · 매처 (이미 쓰고 계신 것)

| 파일 | 역할 |
| --- | --- |
| `AI/ner/matcher.py` | `CompanyMatcher` · `trim_boilerplate()` · `mask_inserted_headlines()` |
| `AI/ner/data/aliases.csv` | 별칭 1,649개 |
| `AI/ner/data/companies.csv` | 202종목 |
| `AI/ner/spark/batch_mentions.py` | 스냅샷 전수 매칭 (전수 재처리에 쓴 것) |

---

## 9. 알려진 결함 하나

`relationship_evidence.evidence_id` 가 비어 있습니다 (적재분 1,031행).

화면은 `evidence_id` 로 `document_evidence` 의 **근거 문장**을 찾는데, 지금 로더가
`document_id` 까지만 연결해서 **"근거 문서 16건" 이라 떠 놓고 문장이 안 보입니다.**

고치는 법은 두 로더 SQL 의 ⚠️ 주석에 적어 뒀습니다. 새로 만드실 때는 **관계를 만든
그 문장의 `evidence_id` 를 그대로 들고 가면** 됩니다 — 관계 추출이 문장 단위로
돌아가므로 재추출이 아니라 그냥 넘기기만 하면 됩니다.

**같은 문서의 아무 문장이나 붙이면 관계와 무관한 문장이 노출되므로 그렇게 하면 안 됩니다.**

## 10. 더 자세한 내용

`AI/graph/AI_description_made_by_PARK.MD` — 단계별 모델·데이터·실측 수치 정리.
