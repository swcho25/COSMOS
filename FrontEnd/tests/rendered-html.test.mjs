import assert from "node:assert/strict";
import { access, readFile, readdir } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = new URL("../", import.meta.url);

async function workerFetch(pathname = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}-${pathname}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request(`http://localhost${pathname}`, { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("renders the COSMOS galaxy shell", async () => {
  const response = await workerFetch("/");
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /COSMOS/);
  assert.match(html, /NEWS × RISK NETWORK/);
  assert.match(html, /뉴스 기반 기업 관계 지도/);
  assert.match(html, /전체 은하/);
  assert.match(html, /커뮤니티/);
  assert.match(html, /내 페이지/);
  assert.doesNotMatch(html, /LG에너지솔루션, 포드와 9\.6조원/);
  assert.doesNotMatch(html, /codex-preview|SkeletonPreview|Your site is taking shape/);
});

test("serves functional navbar destinations", async () => {
  const routes = [
    ["/news", "최신 금융 뉴스"],
    ["/network", "뉴스 기반 기업 관계 지도"],
    ["/companies", "연결 기업 데이터"],
    ["/data", "데이터 수집 및 품질 현황"],
    ["/community", "지금 주목받는 분석"],
  ];
  for (const [pathname, heading] of routes) {
    const response = await workerFetch(pathname);
    assert.equal(response.status, 200, pathname);
    const html = await response.text();
    assert.match(html, new RegExp(heading));
    assert.match(html, /href="\/"/);
  }
});

test("keeps home and news documents dynamic so reloads receive the current client", async () => {
  const [homePage, newsPage] = await Promise.all([
    readFile(new URL("app/page.tsx", root), "utf8"),
    readFile(new URL("app/news/page.tsx", root), "utf8"),
  ]);
  assert.match(homePage, /dynamic = "force-dynamic"/);
  assert.match(newsPage, /dynamic = "force-dynamic"/);
  assert.match(homePage, /revalidate = 0/);
  assert.match(newsPage, /revalidate = 0/);
});

test("exposes a guarded live-news API", async () => {
  const response = await workerFetch("/api/news?scenario=unknown");
  assert.equal(response.status, 400);
  const payload = await response.json();
  assert.equal(payload.error, "unknown_scenario");
  assert.deepEqual(payload.available, ["all", "battery", "semiconductor", "governance"]);
});

test("validates company news searches before calling providers", async () => {
  const response = await workerFetch("/api/news?scenario=all&company=a");
  assert.equal(response.status, 400);
  const payload = await response.json();
  assert.equal(payload.error, "invalid_company_query");
});

test("ships ontology, API, pipeline and social preview artifacts", async () => {
  await Promise.all([
    access(new URL("../AI/ontology/nexus-risk.ttl", root)),
    access(new URL("../BackEnd/services/api/main.py", root)),
    access(new URL("../AI/pipelines/dart_supply_chain_etl.py", root)),
    access(new URL("../BackEnd/infra/neo4j/bootstrap.cypher", root)),
    access(new URL("public/og.png", root)),
    access(new URL("public/company-logos/000660.png", root)),
    access(new URL("public/company-logos/NVDA.png", root)),
  ]);
  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("app/page.tsx", root), "utf8"),
    readFile(new URL("app/layout.tsx", root), "utf8"),
    readFile(new URL("package.json", root), "utf8"),
  ]);
  assert.doesNotMatch(page, /_sites-preview|codex-preview/);
  assert.match(layout, /openGraph/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
});

test("includes per-news evidence intelligence prototype", async () => {
  const [dashboard, portalNav, newsData, crawler, newsRoute, newsRepository, dbSchema, hostingConfig, companyRegistry, secRegistry, entityRecognition] = await Promise.all([
    readFile(new URL("app/risk-dashboard.tsx", root), "utf8"),
    readFile(new URL("app/portal-nav.tsx", root), "utf8"),
    readFile(new URL("lib/news-data.ts", root), "utf8"),
    readFile(new URL("lib/news-crawler.ts", root), "utf8"),
    readFile(new URL("app/api/news/route.ts", root), "utf8"),
    readFile(new URL("db/news-repository.ts", root), "utf8"),
    readFile(new URL("db/schema.ts", root), "utf8"),
    readFile(new URL(".openai/hosting.json", root), "utf8"),
    readFile(new URL("lib/krx-company-registry.generated.ts", root), "utf8"),
    readFile(new URL("lib/sec-company-registry.generated.ts", root), "utf8"),
    readFile(new URL("lib/company-entity-recognition.ts", root), "utf8"),
  ]);
  assert.match(dashboard, /소스 기반 핵심 사실/);
  assert.match(dashboard, /KNOWLEDGE TRIPLES/);
  assert.match(dashboard, /aria-modal="true"/);
  assert.match(dashboard, /원문 출처 열기/);
  assert.match(dashboard, /buildMultiNewsScenario/);
  assert.match(dashboard, /buildArticleNewsScenario/);
  assert.match(dashboard, /article-direct-/);
  assert.match(dashboard, /article-triple-/);
  assert.match(dashboard, /article-model-/);
  assert.match(dashboard, /const propagationEdges: RiskEdge\[\] = indirectCompanies\.flatMap/);
  assert.match(dashboard, /return directCompanies\.flatMap\(\(directCompany\) =>/);
  assert.match(dashboard, /baseConnection\.evidence/);
  assert.match(dashboard, /if \(!baseConnection\) return \[\]/);
  assert.match(dashboard, /news-impact-/);
  assert.match(portalNav, /href: "\/"/);
  assert.match(portalNav, /href: "\/community\/"/);
  assert.match(portalNav, /href: "\/mypage\/"/);
  assert.match(portalNav, /aria-current=/);
  assert.match(dashboard, /function companyLogoPath/);
  assert.match(dashboard, /nvidia: "NVDA"/);
  assert.match(dashboard, /ctx\.drawImage\(stockLogo/);
  assert.match(dashboard, /<CompanyLogo node=\{item\.node\}/);
  assert.match(dashboard, /refreshAfterSeconds\?: number/);
  assert.match(dashboard, /historyParams\.set\("history", "1"\)/);
  assert.doesNotMatch(dashboard, /feedStatus === "fallback" \? fallbackNews/);
  assert.match(dashboard, /window\.setTimeout/);
  assert.match(dashboard, /5분 자동 갱신/);
  assert.match(dashboard, /company-news-query/);
  assert.match(dashboard, /navbar-company-search/);
  assert.match(dashboard, /window\.location\.assign\(`\/news\/\?company=\$\{encodeURIComponent\(query\)\}`\)/);
  assert.match(dashboard, /params\.get\("company"\)/);
  assert.match(dashboard, /searchCompanyNews/);
  assert.match(dashboard, /params\.set\("company", searchedCompany\)/);
  assert.equal((newsData.match(/id: "news-/g) ?? []).length, 9);
  assert.equal((newsData.match(/evidence: \[/g) ?? []).length, 9);
  assert.ok((newsData.match(/sourceUrl:/g) ?? []).length >= 9);
  assert.match(crawler, /naverapihub\.apigw\.ntruss\.com\/search\/v1\/news/);
  assert.match(crawler, /recognizeCompanyEntities/);
  assert.match(crawler, /CO_MENTIONED/);
  assert.doesNotMatch(crawler, /relatedCandidates|rotatedRelatedPool/);
  assert.match(crawler, /naverCompanyNewsApiUrl/);
  assert.match(crawler, /resolveCompanyProfile/);
  assert.match(companyRegistry, /"name": "LG전자"/);
  assert.match(companyRegistry, /"code": "066570"/);
  assert.ok((companyRegistry.match(/"code":/g) ?? []).length >= 2_000);
  assert.match(secRegistry, /"tickers": \[/);
  assert.match(secRegistry, /"NVDA"/);
  assert.match(secRegistry, /"AAPL"/);
  assert.ok((secRegistry.match(/"cik":/g) ?? []).length >= 7_000);
  assert.match(entityRecognition, /hasValidBoundaries/);
  assert.match(entityRecognition, /ambiguousKoreanAliases/);
  assert.match(newsRoute, /X-NCP-APIGW-API-KEY-ID/);
  assert.match(newsRoute, /X-NCP-APIGW-API-KEY/);
  assert.match(newsRoute, /Google News RSS/);
  assert.match(newsRoute, /fetchCompanyFromNaver/);
  assert.match(newsRoute, /mentionsCompany/);
  assert.match(newsRoute, /companyRegistry/);
  assert.match(newsRoute, /SEC EDGAR/);
  assert.doesNotMatch(dashboard, /scenario-select/);
  assert.match(newsRoute, /s-maxage=300/);
  assert.match(newsRoute, /db\.saveHistory/);
  assert.match(newsRoute, /db\.loadHistory/);
  assert.match(newsRoute, /searchParams\.get\("history"\) === "1"/);
  assert.match(newsRoute, /\$\{db\.label\} 저장 뉴스/);
  assert.match(newsRepository, /ON CONFLICT\(storage_key, article_id\) DO UPDATE/);
  assert.match(newsRepository, /saveNewsBodies/);
  assert.match(newsRepository, /article_body = COALESCE/);
  assert.match(newsRepository, /LIMIT 10000/);
  assert.match(dbSchema, /idx_news_items_storage_published/);
  assert.match(dbSchema, /articleBody: text\("article_body"\)/);
  assert.match(hostingConfig, /"d1": "DB"/);
});

test("recognizes foreign companies while rejecting names inside ordinary compound words", async () => {
  const bundled = await build({
    entryPoints: [fileURLToPath(new URL("lib/company-entity-recognition.ts", root))],
    bundle: true,
    write: false,
    platform: "node",
    format: "esm",
  });
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].contents).toString("base64")}`;
  const { recognizeCompanyEntities, resolveCompanyEntity } = await import(moduleUrl);

  const companies = recognizeCompanyEntities("LG전자와 엔비디아, Apple이 AI 신제품 출시와 공급 계약을 발표했다.");
  assert.ok(companies.some((company) => company.label === "LG전자" && company.code === "066570"));
  assert.ok(companies.some((company) => company.label === "NVIDIA" && company.code === "NVDA"));
  assert.ok(companies.some((company) => company.label === "Apple" && company.code === "AAPL"));
  assert.equal(recognizeCompanyEntities("고효율 태양광 시스템을 건물에 설치했다.").some((company) => company.label === "태양"), false);
  assert.equal(recognizeCompanyEntities("코스닥 상장사 태양은 신규 공급계약을 공시했다.").some((company) => company.label === "태양"), true);
  assert.equal(recognizeCompanyEntities("참가 기업을 대상으로 교육 프로그램을 제공한다.").some((company) => company.label === "대상"), false);
  assert.equal(recognizeCompanyEntities("A new mobility system was introduced on time.").some((company) => ["NEW", "TIM"].includes(company.label)), false);
  assert.equal(resolveCompanyEntity("마이크로소프트")?.code, "MSFT");
  assert.equal(resolveCompanyEntity("TSMC")?.code, "TSM");
});

test("classifies brokerage target-price mentions as sources before graph mapping", async () => {
  const bundled = await build({
    entryPoints: [fileURLToPath(new URL("lib/news-role-analysis.ts", root))],
    bundle: true,
    write: false,
    platform: "node",
    format: "esm",
  });
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].contents).toString("base64")}`;
  const { applyCompanyRolesToNews, inferTargetPriceRoles, parseCompanyRoleOutput } = await import(moduleUrl);
  const news = {
    id: "target-price-regression",
    title: "한빛증권, 삼성전자 목표주가 11만원으로 상향",
    summary: "한빛증권 연구원은 삼성전자의 HBM 매출 증가를 전망하며 투자의견 매수를 유지했다.",
    affected: [
      { nodeId: "broker", label: "한빛증권", score: 80, reason: "기사 직접 언급" },
      { nodeId: "samsung", label: "삼성전자", score: 80, reason: "기사 직접 언급" },
    ],
    relations: [
      { source: "한빛증권", relation: "NEWS_MENTIONS", target: "삼성전자", confidence: 90 },
      { source: "삼성전자", relation: "EXPECTS", target: "HBM 매출 증가", confidence: 85 },
    ],
  };
  const heuristic = inferTargetPriceRoles(news);
  assert.equal(heuristic.companies.find((company) => company.name === "한빛증권").role, "source");
  const filtered = applyCompanyRolesToNews(news, heuristic);
  assert.deepEqual(filtered.affected.map((company) => company.label), ["삼성전자"]);
  assert.equal(filtered.relations.some((relation) => relation.source === "한빛증권" || relation.target === "한빛증권"), false);

  const parsed = parseCompanyRoleOutput([{
    generated_text: [{ role: "assistant", content: '<think>분석</think>{"companies":[{"name":"한빛증권","role":"source","confidence":0.98},{"name":"삼성전자","role":"affected","confidence":0.96}]}' }],
  }], ["한빛증권", "삼성전자"]);
  assert.equal(parsed.find((company) => company.name === "한빛증권").role, "source");
  assert.equal(parsed.find((company) => company.name === "삼성전자").role, "affected");
});

test("ships the local Hugging Face WebGPU role classifier", async () => {
  const [worker, analysis, dashboard, packageJson] = await Promise.all([
    readFile(new URL("lib/news-role.worker.ts", root), "utf8"),
    readFile(new URL("lib/news-role-analysis.ts", root), "utf8"),
    readFile(new URL("app/risk-dashboard.tsx", root), "utf8"),
    readFile(new URL("package.json", root), "utf8"),
  ]);
  assert.match(worker, /device: "webgpu"/);
  assert.match(worker, /dtype: "q4f16"/);
  assert.match(dashboard, /news-role\.worker\.ts\?worker&url/);
  assert.match(dashboard, /new Worker\(newsRoleWorkerUrl/);
  assert.doesNotMatch(dashboard, /new Worker\(new URL/);
  assert.match(analysis, /onnx-community\/Qwen3-0\.6B-Instruct-ONNX/);
  assert.match(analysis, /A증권.*source.*B.*affected/);
  assert.match(dashboard, /LOCAL AI ROLE FILTER/);
  assert.match(dashboard, /목표주가·투자의견을 낸 증권사는 분석 출처/);
  assert.match(packageJson, /@huggingface\/transformers/);
  assert.match(packageJson, /prune-server-static-assets/);
  const [clientStatic, serverStatic] = await Promise.all([
    readdir(new URL("dist/client/_next/static/", root)),
    readdir(new URL("dist/server/_next/static/", root)),
  ]);
  assert.ok(clientStatic.some((name) => /^ort-wasm.*\.wasm$/i.test(name)));
  assert.equal(serverStatic.some((name) => /^ort-wasm.*\.wasm$/i.test(name)), false);
});

test("keeps persisted news history stable and deduplicated", async () => {
  const bundled = await build({
    entryPoints: [fileURLToPath(new URL("db/news-repository.ts", root))],
    bundle: true,
    write: false,
    platform: "node",
    format: "esm",
  });
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].contents).toString("base64")}`;
  const { mergeNewsHistory, newsStorageKey } = await import(moduleUrl);
  const article = (id, sourceUrl, publishedAtIso) => ({ id, sourceUrl, publishedAtIso, title: id });
  const merged = mergeNewsHistory(
    [article("new", "https://news.example/new", "2026-08-25T00:00:00.000Z")],
    [
      article("new-copy", "https://news.example/new", "2026-08-25T00:00:00.000Z"),
      article("old", "https://news.example/old", "2026-08-24T00:00:00.000Z"),
    ],
    10,
  );
  assert.deepEqual(merged.map((item) => item.id), ["new", "old"]);
  const refreshed = mergeNewsHistory(
    [article("newer", "https://news.example/newer?utm_source=feed", "2026-08-26T00:00:00.000Z")],
    merged,
    10,
  );
  assert.deepEqual(refreshed.map((item) => item.id), ["newer", "new", "old"]);
  const preservedBody = mergeNewsHistory(
    [article("same", "https://news.example/same", "2026-08-27T00:00:00.000Z")],
    [{
      ...article("same", "https://news.example/same", "2026-08-27T00:00:00.000Z"),
      articleBody: "이미 D1에 저장된 기사 본문",
      articleExtraction: { status: "extracted", requestedUrl: "https://news.example/same" },
    }],
    10,
  );
  assert.equal(preservedBody[0].articleBody, "이미 D1에 저장된 기사 본문");
  assert.equal(preservedBody[0].articleExtraction.status, "extracted");
  assert.equal(newsStorageKey("all", " 삼성전자  "), "company:삼성전자");
  assert.equal(newsStorageKey("battery", ""), "scenario:battery");
});
