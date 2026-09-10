# COSMOS Web

뉴스·공시에서 추출한 기업 간 관계를 3D 은하로 탐색하는 프론트엔드입니다.
React 19 + Vite + TypeScript + React Three Fiber 로 작성했으며, `docs/api-specification.md` 의 계약을 그대로 타입으로 옮겨 사용합니다.

```bash
npm install
npm run dev        # http://localhost:5173  (기본: 브라우저 내 목업 데이터)
npm run build      # tsc 타입 검사 + vite 빌드 → dist/
npm run typecheck
```

## 화면

| 경로 | 내용 |
| --- | --- |
| `/` | 전체 은하. 연관도가 높은 기업일수록 가까이 놓이는 나선 은하 원반(허브가 핵, 팔을 따라 별가루)과 관계 간선. 노드 클릭 → 워프 → 기업 중심 관계망(depth 궤도) + 우측 인텔리전스 패널(뉴스·주가·게시판). 관계선 클릭 → 관계 상세·근거 뉴스 패널. `Ctrl+K` 기업 검색, `?company={id}` 로 진입 가능 |
| `/news` | 좌측 뉴스 레일 + 우측 관계 지도(선택 뉴스 관련 기업 ±1홉) + 영향도 랭킹·최강 전이 경로·근거 문장. 노드 클릭 시 좌측이 그 기업 뉴스로 전환 |
| `/companies` | 관심 기업, 기업 디렉터리(검색·시장·산업), 투자·지분 관계 트리 |
| `/me` | 내 정보·관심 기업·스크랩·분석 가중치 (프로필 메뉴에서 진입) |
| `/lab/art` | 아트 디렉션 랩 (내비게이션 미노출). 항성 1·행성 3종·관계 빔·로고 이름표만 놓고 형태·머티리얼·팔레트를 확정하는 실험 페이지. 팔레트는 `src/features/lab/toon.ts` |

카메라는 두 모드입니다. **궤도 모드**(기본)는 드래그 회전·휠 확대, **탐색 모드**는 우주선처럼 직접 날아다닙니다 — `W A S D` 이동, `Q E` 위아래, `Shift` 가속, 드래그로 시선, 휠로 전진. `F` 키 또는 하단 중앙 버튼으로 전환하며 두 모드 모두 기업 클릭으로 워프합니다.

관계를 읽는 도구: 관계선을 호버하면 유형·방향·점수·근거 수 툴팁이 뜨고, 기업을 호버하면 1홉 이웃에 관계 유형 색 링(방향 쐐기 포함)과 이름표 역할 태그(공급사·고객·투자자·피투자·협력·경쟁)가 붙습니다. 기업 중심 뷰에는 1홉 요약 스트립이 있어 역할별로 걸러 볼 수 있고, `Shift+클릭`으로 기준 기업을 고정하면 다른 기업 호버 시 최단 경로가 상단에 표시됩니다. 바닥의 점 격자는 기업·성단의 연결 가중치만큼 패이는 중력 우물이며, 기업을 누르면 관련 행성이 그 우물로 빨려 들어가 궤도에 안착합니다.

간선은 `score`(0~100)가 높을수록 굵고 밝고 빠르게 흐르며, 색은 `impactDirection`(긍정 파랑 · 부정 빨강 · 중립 보라)을 나타냅니다. 관계 유형은 색이 아니라 펄스 방향(방향 관계)과 점선(무방향 관계)으로 구분합니다. 방향이 있는 관계는 source → target 으로 흐르고, 무방향 관계는 양 끝에서 흐릅니다.

## 데이터 모드

| `VITE_API_MODE` | 동작 |
| --- | --- |
| `mock` (기본) | `src/mock/` 이 명세 형태의 데이터를 브라우저에서 생성합니다. 170여 개 기업(KOSPI·NASDAQ 중심), 큐레이션된 관계 300여 개, 뉴스·주가·댓글. 로그인/관심/스크랩/가중치/댓글은 localStorage 에 저장됩니다. 체험 계정 `demo@cosmos.dev` / `cosmos123` |
| `live` | `/api` 를 실제 백엔드로 호출합니다. `vite.config.ts` 의 프록시(`VITE_API_PROXY`, 기본 `http://localhost:8080`)를 통해 Spring API 로 전달됩니다 |

두 구현은 `src/api/contract.ts` 의 `CosmosApi` 인터페이스를 공유하므로, 화면 코드는 모드와 무관합니다.

## 구조

```
src/
  api/        타입(types.ts) · 계약(contract.ts) · fetch 클라이언트(client.ts, Bearer + 401 TOKEN_EXPIRED 시 refresh 1회) · live 구현
  mock/       목업 데이터 시드(data.ts) · 우주 빌더(universe.ts) · 목업 API(mockApi.ts)
  lib/        레이아웃(graph.ts) · 팔레트·관계 메타(meta.ts) · 포맷 · react-query 훅(queries.ts)
  store/      zustand — session(인증), galaxy(뷰·필터·패널), ui(다이얼로그·토스트)
  three/      R3F 씬 — Scene(카메라 연출·워프·후처리), CompanyNodes(인스턴스 항성), RelationLines(리본 셰이더 간선), Labels, Starfield, Nebula, Warp, MiniScene
  features/   galaxy · news · companies · profile · auth
  components/ 헤더, 공통 UI, 주가 차트
```

## 기업 로고

`public/company-logos/` 의 로고는 `npm run logos:fetch` 로 수집합니다 (`scripts/fetch-logos.mjs`). 종목코드·티커별 대표 도메인(국내 계열사는 그룹 도메인 병행)을 Clearbit → 사이트 apple-touch-icon → Google favicon → DuckDuckGo 아이콘 → 사이트 favicon → logo.dev 순으로 시도해 저장하고, 결과 목록을 `src/lib/logo-codes.json` 에 씁니다. 파일이 없는 기업은 3D 노드와 아바타에 모노그램이 표시됩니다. 수집된 로고는 각 기업의 상표이며, logo.dev 폴백은 공개 문서용 키를 쓰므로 실제 서비스에서는 자체 키와 출처 표기가 필요합니다.

## 백엔드에 확인이 필요한 항목

- `relationshipType` 코드 목록. 현재 `SUPPLY` `INVEST` `PARTNER` `COMPETE` 를 가정하며, 그 외 코드는 회색으로 표시됩니다 (`src/lib/meta.ts`).
- 기업 중심 그래프(`/api/graphs/companies/{id}`)의 노드에 `industryName`·`market` 이 없어 전체 그래프를 한 번 받아 클라이언트에서 조인합니다. 응답에 두 필드를 추가하면 조인이 불필요합니다.
- 화면구성도의 게시판(글 제목·카테고리·댓글)은 명세의 단일 계층 댓글 API 로 축소해 구현했습니다. 주가 탭의 시가총액·PER 도 명세에 없어 OHLCV 기반 지표와 뉴스·관계 지표로 대체했습니다.
