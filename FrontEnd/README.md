# COSMOS RISK

정형 데이터(Open DART·재무)와 비정형 데이터(금융 뉴스)에서 기업·주주·원자재 관계를 추출하고, 특정 충격이 기업 네트워크를 따라 전이되는 경로와 강도를 분석하는 금융 공급망 리스크 프로젝트입니다.

> 지분율·계약·실적 사실은 연결된 공식 IR과 실제 보도를 기준으로 합니다. 전이 가중치·영향도·예상 시총 충격은 프로토타입 모델 추정치이며 투자 판단 자료가 아닙니다.

## 구현 범위

- **온톨로지**: `Company`, `Shareholder`, `Industry`, `Material`, `Event` 클래스와 `OWNS_STAKE`, `SUPPLIES_TO`, `USES_MATERIAL`, `SHORTAGE_RISK` 관계
- **데이터 파이프라인**: DART 주요 매출처 표 정규화, 뉴스 관계 추출 baseline, PySpark triple 중복 제거
- **그래프 저장·추론**: Neo4j 제약조건, 지배회사/독점 공급자 Cypher 규칙, GDS Personalized PageRank 쿼리
- **분석 엔진**: 관계 유형별 계수와 감쇠율을 반영한 weighted graph diffusion, 최대 파급 경로, 지배관계 추론
- **서비스**: 독립 FastAPI 분석 API와 웹 데모 API
- **실시간 뉴스 수집**: NAVER API HUB 뉴스 검색을 서버에서 수집하고 5분 캐시, API 장애·키 미설정 시 Google News RSS로 자동 대체
- **뉴스 영구 저장**: 수집한 뉴스 제목·요약·원문 링크·추출 본문과 분석 결과를 D1에 누적 저장하고, API 장애 시에도 저장된 뉴스 이력을 제공
- **실제 뉴스별 관계 지도**: 목록에서 선택한 실제 기사 1건마다 뉴스 이벤트·직접 언급 기업·추출 트리플·간접 전이 기업을 독립된 방향성 그래프로 생성
- **카테고리 통합망**: 같은 카테고리의 최신 뉴스 최대 6건을 별도 보조 탭에서 통합해 반복 노출과 누적 연결 강도를 탐색
- **연결 강도**: NLP 신뢰도, 뉴스 톤, 최신성, 기업별 직접·간접 노출 점수를 결합해 뉴스→기업 가중치를 계산

## 빠른 실행

웹 대시보드:

```bash
npm install
npm run dev
```

FastAPI:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r ../BackEnd/services/api/requirements.txt
uvicorn services.api.main:app --reload
```

Neo4j + API:

```bash
docker compose up --build
```

- 대시보드: `http://localhost:3000`
- FastAPI 문서: `http://localhost:8000/docs`
- Neo4j Browser: `http://localhost:7474`

운영 전 `../BackEnd/docker-compose.yml`의 데모 비밀번호는 반드시 변경해야 합니다.

## 분석 점수

원천 충격 점수를 `S₀`, 엣지 가중치를 `w`, 관계별 계수를 `r`, hop 감쇠율을 `λ`라고 할 때 다음 노드의 점수는 아래처럼 계산합니다.

```text
S(v) = max over paths p to v [ S₀ × ∏(wₑ × rₑ × λ) ]
```

- `SHORTAGE_RISK`: 1.00
- `USES_MATERIAL`: 1.00
- `SUPPLIES_TO`: 0.92
- `OWNS_STAKE`: 0.74
- `BELONGS_TO`: 0.30
- 기본 감쇠율 `λ`: 0.82

실제 운영에서는 이 결과와 재무 취약도, 재고일수, 대체 공급자 수, 신용 스프레드, 이벤트 신뢰도를 결합해 최종 리스크를 산출하도록 확장할 수 있습니다.

## 실시간 뉴스 수집

`/api/news?scenario=all`은 배터리·반도체·지배구조 검색식을 NAVER API HUB 뉴스 검색에 요청합니다. 응답은 5분간 캐시하며 새 기사와 DB에 저장된 기사 이력을 합쳐 최신순으로 표시합니다. NAVER 검색 API는 기사 본문을 반환하지 않으므로, DB가 연결된 일반 뉴스 요청에서도 최신 기사 최대 5건의 원 언론사 URL(없으면 네이버 링크)을 서버에서 자동으로 열어 본문을 추출합니다. 추출 전에 각 호스트의 `/robots.txt`를 전용 사용자 에이전트로 조회하고 기사 URL과 모든 리다이렉트 목적지가 허용된 경우에만 본문을 요청합니다. robots 조회 실패·서버 오류·명시적 차단은 보수적으로 저장하지 않으며, 허용 확인 결과와 시각을 `article_extraction_json`에 기록합니다. 성공한 본문과 추출 메타데이터는 DB의 `article_body`, `article_extraction_json`, `article_extracted_at_iso` 컬럼에 저장됩니다. `includeBody=1`은 저장 여부가 아니라 API 응답에 `articleBody`, `articleExtraction`을 함께 노출할지를 제어합니다. 저장된 본문은 24시간마다 robots 정책을 다시 확인하고, 재검사 또는 재추출에 실패하면 본문 칼럼을 비웁니다. 화면에서는 선택한 실제 기사 1건을 중심으로 직접 언급, 기사에서 추출한 주어-관계-목적어 트리플, 공급망 모델이 계산한 간접 전이를 서로 다른 색과 근거 유형으로 표시합니다. 카테고리 통합망은 별도 보조 탭에서 제공합니다. NAVER 키가 없거나 API 호출에 실패하면 Google News RSS를 사용하고, 외부 피드까지 실패하면 DB에 저장된 최신 뉴스 이력을 제공합니다.

운영 환경에는 다음 값을 서버 비밀 환경변수로 설정합니다. 브라우저 번들, 소스코드, `.env.example`에는 실제 값을 넣지 않습니다.

```dotenv
NAVER_API_HUB_CLIENT_ID=
NAVER_API_HUB_CLIENT_SECRET=
```

기사 원문 링크에서 본문 추출이 가능한지 로컬에서 확인하려면 아래 명령을 실행합니다. URL을 생략하면 공개 기사 4건으로 스모크 테스트하며, 원문 전체는 출력하지 않고 추출 방식·문자 수·짧은 미리보기만 표시합니다.

```bash
npm run news:extract:test -- https://example.com/news/article
```

일반 `/api/news` 요청은 DB가 연결돼 있으면 본문을 자동 저장합니다. 저장된 본문을 API 응답에서도 함께 확인하려면 `includeBody=1`을 사용합니다. `bodyLimit`은 한 요청에서 새로 본문을 가져올 최신 기사 수이며 1~10, 기본값은 5입니다. 언론사의 접근 정책이나 동적 렌더링 방식에 따라 개별 기사는 `articleExtraction.status: "failed"`가 될 수 있습니다. 운영 시에는 각 언론사의 이용약관과 콘텐츠 저장 권한을 확인해야 합니다.

```http
GET /api/news?scenario=semiconductor&includeBody=1&bodyLimit=5
```

배포 뉴스와 MySQL DB를 동기화하면서 과거 검색 결과를 먼저 백필하고 robots 허용 본문을 최대 100건씩 계속 채우려면 다음 장기 실행 작업을 사용합니다. 시작할 때 배터리·반도체·지배구조 검색어별로 NAVER 검색 API의 최대 범위인 1000건까지 페이지를 순회한 뒤, 신규 뉴스는 5분마다 확인합니다. 본문 미처리 항목이 100건 이상 남아 있으면 기본 30초 간격으로 다음 묶음을 처리합니다. `NEWS_SYNC_INTERVAL_SECONDS`와 `NEWS_BACKLOG_INTERVAL_SECONDS`로 주기를 조정할 수 있습니다.

MySQL의 원본 `news_items` 테이블에는 자동 증가 고유키 `news_id`와 최신순 표시 번호 `article_no`가 있습니다. 정확한 전체 건수는 `SELECT COUNT(*) FROM news_items`로 확인합니다. `production_news_bodies` 뷰를 열면 robots 승인이 확인된 본문만 별도로 1부터 다시 번호가 붙고 `news_id`도 함께 표시됩니다.

```bash
npm run news:mysql:watch-production -- all 100
```

인증된 Sites 배포 API를 사용할 때는 `PRODUCTION_NEWS_API_URL`과 `SITES_BYPASS_TOKEN`을 실행 환경에 설정합니다. 검색 API 자체의 `start` 상한이 1000이므로 이 방식은 연결된 세 검색 주제의 최대 3000개 결과를 대상으로 하며, 인터넷 전체 언론사의 모든 과거 기사를 의미하지 않습니다.

### 집에서 독립 크롤러 실행

배포 API를 거치지 않는 독립 실행 코드는 `scripts/news-crawler-standalone.mjs`입니다. 저장소를 받은 뒤 `.env.example`을 `.env.local`로 복사하고 `NAVER_API_HUB_CLIENT_ID`, `NAVER_API_HUB_CLIENT_SECRET`, `MYSQL_URL`을 설정합니다. 비밀키가 들어 있는 `.env.local`은 Git에 올리지 않습니다.

현재 뉴스만 한 번 수집하고 robots 허용 본문을 저장하려면:

```bash
npm install
npm run news:crawler:standalone
```

과거 최대 범위를 먼저 백필한 뒤 신규 뉴스까지 계속 수집하려면:

```bash
npm run news:crawler:standalone -- --backfill --watch
```

중지할 때는 실행 중인 터미널에서 `Ctrl+C`를 누릅니다. `--backfill`은 주제별 최대 1000건의 메타데이터를 먼저 저장하고, 본문은 robots 허용 여부를 확인하면서 100개씩 처리합니다. 수집 결과는 `news_items` 테이블과 robots 승인 본문 전용 `production_news_bodies` 뷰에서 확인할 수 있습니다.

### NAVER 검색 기반 언론사 자동 발견

`scripts/crawl-naver-publisher-discovery-to-mysql.mjs`는 여러 산업·경제 검색어를 NAVER 뉴스 검색 API에 요청하고, 결과의 원문 URL에서 언론사 도메인을 자동 발견합니다. 새 도메인은 `news_publisher_sources`에 기록되며 robots 허용과 본문 추출에 성공한 기사만 `news_items`에 저장됩니다. URL별 재시도 상태는 `news_url_crawl_attempts`에 저장합니다.

현재 DB에 성공 본문이 있는 도메인만 언론사 레지스트리에 등록하려면 NAVER 키 없이 다음 명령을 실행할 수 있습니다.

```bash
npm run news:crawler:naver-discovery -- --seed-only
```

처음 세 페이지씩 백필하고 이후 매시간 새로운 검색 결과와 언론사를 자동 발견하려면 유효한 `NAVER_API_HUB_CLIENT_ID`, `NAVER_API_HUB_CLIENT_SECRET`, `MYSQL_URL`을 설정한 뒤 실행합니다.

```bash
npm run news:crawler:naver-discovery -- --watch --backfill-pages 3 --interval-minutes 60
```

활성 언론사와 언론사별 성공 건수는 다음 SQL로 확인합니다.

```sql
SELECT publisher_domain, display_name, status, success_count, failure_count
FROM news_publisher_sources
ORDER BY success_count DESC, publisher_domain;
```

NAVER 검색으로 한 번이라도 본문 추출에 성공해 `active`가 된 언론사는 robots.txt에 공개된 최신 뉴스 사이트맵도 자동 수집할 수 있습니다. 기존 전용 크롤러가 담당하는 헬로티·한국정경신문·메디컬투데이·서울경제·뉴스핌·뉴스토마토는 중복 요청에서 제외됩니다. 나머지 활성 언론사를 6시간마다 확인하고 언론사당 최신 기사 최대 300개를 저장하려면 다음 명령을 실행합니다.

```bash
npm run news:crawler:active-publishers -- --watch --interval-minutes 360 --limit-per-domain 300
```

언론사별 사이트맵 발견 상태는 다음 SQL로 확인합니다.

```sql
SELECT publisher_domain, status, sitemap_documents, candidate_count, last_error, checked_at_iso
FROM news_publisher_sitemap_state
ORDER BY candidate_count DESC, publisher_domain;
```

### 언론사별 10년 과거 기사 백필

`scripts/crawl-publisher-history-to-mysql.mjs`는 활성 언론사의 robots.txt 선언 사이트맵,
연도별 사이트맵, 사이트맵 인덱스의 월별 하위 문서, 홈페이지에서 발견한 기사 목록과
페이지네이션을 이용해 지정 기간의 과거 기사를 수집합니다. 기사와 발견 문서를 각각
DB 대기열에 기록하므로 중단한 뒤 같은 명령을 실행하면 남은 작업부터 재개합니다.
본문은 기사 URL의 robots 허용과 발행일 범위를 모두 확인한 경우에만 저장됩니다.

```bash
# 먼저 발견 결과만 소규모로 확인
npm run news:crawler:publisher-history -- --discover-only --max-documents-per-domain 20

# 최근 10년 백필을 중단 가능한 연속 모드로 실행
npm run news:crawler:publisher-history -- --years 10 --continuous

# 특정 언론사와 명시적 기간만 실행
npm run news:crawler:publisher-history -- --domains newsis.com,sedaily.com --since 2016-09-01 --until 2026-09-02
```

월별 사이트맵 주소를 직접 추측하는 탐색은 요청량이 많아 기본값에서 꺼져 있습니다.
필요한 경우 `--probe-monthly-sitemaps`를 사용하거나
`config/news-publisher-backfill-profiles.json`의 언론사별 `monthlySitemapTemplates`,
`archiveUrls`, `archiveTemplates`에 확인된 공식 주소를 추가합니다.

```sql
SELECT publisher_domain, status, sitemap_documents, archive_pages,
       discovered_candidates, pending_candidates, stored_articles,
       existing_articles, oldest_candidate_iso, newest_candidate_iso, last_error
FROM news_publisher_backfill_state
ORDER BY status, publisher_domain;
```

robots 허용은 접근 정책 준수 여부만 뜻하며, 기사 본문 저장·이용에 대한 저작권 또는 이용허락을 보장하지 않습니다.

### AWS ECS Fargate 배포

국내 크롤러와 해외 크롤러 이미지는 각각 `deploy/crawlers/Dockerfile.domestic`과
`overseas-news-crawler/Dockerfile`로 빌드합니다. `deploy/crawlers/deploy-ecs.ps1`은
ECR, ECS, CloudWatch Logs, 전용 보안그룹, 해외 재개 큐용 EFS와 S3 버킷을
생성하거나 갱신합니다. RDS 비밀번호는 파일에 저장하지 않고 RDS가 관리하는
Secrets Manager 비밀을 ECS 작업에 주입합니다.

```powershell
./deploy/crawlers/deploy-ecs.ps1
```

배포 스크립트는 안전한 상태 이전을 위해 ECS 서비스의 최초 실행 개수를 0으로
둡니다. 해외 SQLite 큐를 S3 bootstrap 객체로 업로드한 다음 국내·해외 서비스를
각각 `desired-count 1`로 올립니다. 해외 서비스는 EFS의 단일 SQLite 파일을 쓰므로
동시에 두 작업을 실행하지 않습니다.

해외 크롤러는 별도 비공개 저장소를 submodule로 연결합니다. 새 컴퓨터에서는
다음과 같이 함께 받습니다.

```bash
git clone --recurse-submodules https://github.com/wildyoung/nexus-risk-intelligence.git
```

## API 예시

웹 데모 엔진:

```http
GET /api/analyze?scenario=battery
GET /api/health
GET /api/news?scenario=all
GET /api/news?scenario=all&includeBody=1&bodyLimit=5
```

독립 분석 엔진:

```http
POST /v1/analysis/propagate
POST /v1/analysis/rules
POST /v1/analysis/path-strength
```

요청 예시:

```json
{
  "nodes": [
    {"id": "lithium", "label": "리튬", "kind": "Material"},
    {"id": "supplier", "label": "A사", "kind": "Company"},
    {"id": "buyer", "label": "B사", "kind": "Company"}
  ],
  "edges": [
    {"source": "lithium", "target": "supplier", "relation": "SHORTAGE_RISK", "weight": 0.9},
    {"source": "supplier", "target": "buyer", "relation": "SUPPLIES_TO", "weight": 0.7}
  ],
  "shock_node_id": "lithium",
  "shock_score": 95,
  "attenuation": 0.82,
  "max_depth": 4
}
```

## 디렉터리

```text
app/                         웹 대시보드와 데모 API
lib/                         TypeScript 그래프 데이터·분석 엔진
../BackEnd/services/api/     FastAPI 및 Python 분석 엔진
../AI/pipelines/             DART·뉴스·Spark triple ETL
../AI/ontology/nexus-risk.ttl OWL/RDF 온톨로지
../BackEnd/infra/neo4j/      제약조건·규칙·GDS 쿼리
data/                        합성 triple 샘플
docs/architecture.md         설계, 품질관리, 확장 로드맵
```

## 검증

```bash
npm run build
npm run lint
python -m pytest ../BackEnd/services/api/tests
```

## 포트폴리오 시연 순서

1. 실시간 뉴스에서 기사 한 건을 선택하고 자동으로 연결된 공급망·지배구조 그래프를 확인합니다.
2. 기사 상세에서 인식 엔티티, 추출 관계, 원문 출처, 영향 기업을 함께 봅니다.
3. 선택 뉴스에 대한 그래프 확산을 실행하고 상위 영향 기업과 최대 파급 경로를 확인합니다.
4. 기업 노드의 실제 지분·공급관계 근거와 모델 추정치를 구분해 설명합니다.
5. `../AI/ontology/`, `../AI/pipelines/`, `../BackEnd/infra/neo4j/`, `../BackEnd/services/api/`를 통해 데이터부터 서빙까지 이어지는 구조를 설명합니다.
