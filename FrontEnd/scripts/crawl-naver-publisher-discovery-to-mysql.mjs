import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createPool } from "mysql2/promise";
import { extractNewsArticle } from "../lib/news-article-extractor.ts";

const USER_AGENT = "NexusRiskArticleExtractor/1.0 (+news intelligence; source-linked)";
const STORAGE_KEY = "production:naver-discovery";
const PROVIDER = "NAVER API publisher discovery";
const DEFAULT_INTERVAL_MINUTES = 60;
const DEFAULT_BACKFILL_PAGES = 3;
const DEFAULT_HOST_DELAY_MS = 1_500;
const DEFAULT_QUERIES = [
  "반도체 HBM",
  "인공지능 AI 산업",
  "로봇 자동화",
  "배터리 이차전지",
  "전기차 자동차 부품",
  "디스플레이 OLED",
  "조선 해운",
  "철강 산업",
  "석유화학 소재",
  "에너지 전력",
  "원전 원자력",
  "태양광 풍력 재생에너지",
  "바이오 제약",
  "의료기기 헬스케어",
  "방산 우주항공",
  "건설 부동산",
  "물류 공급망",
  "유통 소비재",
  "금융 증권",
  "기업 지배구조",
  "인수합병 M&A",
  "수출 무역",
  "환율 금리",
  "클라우드 데이터센터",
  "사이버보안 보안",
  "통신 5G 6G",
  "게임 콘텐츠",
  "정부 산업정책",
  "스타트업 투자",
  "ESG 탄소중립",
];
const TRACKING_PARAMETERS = new Set([
  "ref",
  "inflow",
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_content",
  "utm_term",
]);

loadLocalEnvironment();

const options = readOptions(process.argv.slice(2));
const mysqlUrl = requiredEnvironment("MYSQL_URL");
const pool = createPool(await mysqlConnectionOptions(mysqlUrl));
const hostQueues = new Map();
const hostLastRequestAt = new Map();
let stopRequested = false;
let firstCycle = true;

process.on("SIGINT", requestStop);
process.on("SIGTERM", requestStop);

try {
  await ensureSchema();
  await seedPublisherRegistryFromExistingNews();
  if (options.seedOnly) {
    console.log(JSON.stringify({ phase: "seed_complete", registry: await registryStats(), at: new Date().toISOString() }));
  } else {
    const api = await selectNaverApi();
    do {
      await runCycle(api, firstCycle ? options.backfillPages : 1);
      firstCycle = false;
      if (!options.watch || stopRequested) break;
      console.log(JSON.stringify({
        phase: "watch_wait",
        minutes: options.intervalMinutes,
        nextAt: new Date(Date.now() + options.intervalMinutes * 60_000).toISOString(),
      }));
      await interruptibleDelay(options.intervalMinutes * 60_000);
    } while (!stopRequested);
  }
} catch (error) {
  console.error(JSON.stringify({ phase: "fatal", error: safeError(error), at: new Date().toISOString() }));
  process.exitCode = 1;
} finally {
  await pool.end();
}

async function runCycle(api, pagesPerQuery) {
  const startedAt = new Date().toISOString();
  const existingUrls = await loadExistingUrls();
  const recentFailures = await loadRecentFailureHashes();
  const candidates = await discoverCandidates(api, pagesPerQuery);
  const targets = candidates
    .filter((candidate) => !existingUrls.has(candidate.normalizedUrl))
    .filter((candidate) => !recentFailures.has(candidate.urlHash))
    .slice(0, options.limit);
  const stats = {
    api: api.mode,
    queries: options.queries.length,
    pagesPerQuery,
    discoveredUrls: candidates.length,
    existingUrls: candidates.length - targets.length,
    targets: targets.length,
    attempted: 0,
    stored: 0,
    notFound: 0,
    robotsDisallowed: 0,
    robotsUnavailable: 0,
    failed: 0,
    retried: 0,
  };
  let cursor = 0;

  console.log(JSON.stringify({ phase: "cycle_start", stats, startedAt }));
  const workers = Array.from({ length: Math.min(options.concurrency, targets.length) }, async () => {
    while (!stopRequested) {
      const candidate = targets[cursor];
      cursor += 1;
      if (!candidate) return;
      await registerPublisherCandidate(candidate);
      stats.attempted += 1;
      try {
        const article = await extractWithRetries(candidate, stats);
        if (!article) continue;
        const articleBody = cleanArticleBody(candidate.domain, article.textContent);
        if (articleBody.length < 80) throw new Error("article_body_not_found_after_cleanup");
        await storeArticle(candidate, article, articleBody);
        await saveUrlAttempt(candidate, "stored", null, 1);
        await recordPublisherSuccess(candidate, article.siteName, article.robots.checkedAt);
        existingUrls.add(candidate.normalizedUrl);
        stats.stored += 1;
      } catch (error) {
        const message = safeError(error);
        if (message === "article_robots_disallowed") stats.robotsDisallowed += 1;
        else if (message === "article_robots_unavailable") stats.robotsUnavailable += 1;
        else stats.failed += 1;
        await saveUrlAttempt(candidate, failureStatus(message), message, options.retries);
        await recordPublisherFailure(candidate, message);
        console.error(JSON.stringify({
          phase: "article_failed",
          domain: candidate.domain,
          url: candidate.normalizedUrl,
          error: message,
          at: new Date().toISOString(),
        }));
      }

      const completed = stats.stored + stats.notFound + stats.robotsDisallowed
        + stats.robotsUnavailable + stats.failed;
      if (completed > 0 && completed % options.progressEvery === 0) {
        console.log(JSON.stringify({ phase: "progress", stats, registry: await registryStats(), at: new Date().toISOString() }));
      }
    }
  });
  await Promise.all(workers);
  console.log(JSON.stringify({
    phase: stopRequested ? "cycle_stopped" : "cycle_complete",
    stats,
    registry: await registryStats(),
    database: await databaseStats(),
    startedAt,
    finishedAt: new Date().toISOString(),
  }));
}

async function discoverCandidates(api, pagesPerQuery) {
  const candidates = new Map();
  for (const query of options.queries) {
    for (let page = 0; page < pagesPerQuery; page += 1) {
      if (stopRequested) return [...candidates.values()];
      const start = page * 100 + 1;
      const payload = await fetchNaverPage(api, query, start, 100);
      for (const item of parseNaverItems(payload, query)) {
        const existing = candidates.get(item.normalizedUrl);
        if (existing) {
          existing.queries.add(query);
        } else {
          candidates.set(item.normalizedUrl, item);
        }
      }
      console.log(JSON.stringify({
        phase: "api_discovery",
        api: api.mode,
        query,
        page: page + 1,
        returned: Array.isArray(payload.items) ? payload.items.length : 0,
        uniqueUrls: candidates.size,
        at: new Date().toISOString(),
      }));
      if (!Array.isArray(payload.items) || payload.items.length < 100) break;
      await interruptibleDelay(250);
    }
  }
  return [...candidates.values()].sort((left, right) =>
    Date.parse(right.publishedAtIso) - Date.parse(left.publishedAtIso));
}

async function selectNaverApi() {
  const clients = configuredNaverClients();
  if (!clients.length) throw new Error("naver_api_credentials_required");
  const failures = [];
  for (const client of clients) {
    try {
      await fetchNaverPage(client, options.queries[0], 1, 1);
      console.log(JSON.stringify({ phase: "naver_api_selected", mode: client.mode, at: new Date().toISOString() }));
      return client;
    } catch (error) {
      failures.push({ mode: client.mode, error: safeError(error) });
    }
  }
  console.error(JSON.stringify({ phase: "naver_api_auth_failed", failures, at: new Date().toISOString() }));
  throw new Error("naver_api_credentials_invalid");
}

function configuredNaverClients() {
  const requestedMode = process.env.NAVER_API_MODE?.trim().toLowerCase();
  const clients = [];
  const hubId = process.env.NAVER_API_HUB_CLIENT_ID?.trim();
  const hubSecret = process.env.NAVER_API_HUB_CLIENT_SECRET?.trim();
  const legacyId = process.env.NAVER_CLIENT_ID?.trim();
  const legacySecret = process.env.NAVER_CLIENT_SECRET?.trim();
  if (hubId && hubSecret && requestedMode !== "openapi") {
    clients.push({ mode: "hub", id: hubId, secret: hubSecret });
  }
  if (legacyId && legacySecret) {
    if (requestedMode === "hub") clients.push({ mode: "hub", id: legacyId, secret: legacySecret });
    else if (requestedMode === "openapi") clients.push({ mode: "openapi", id: legacyId, secret: legacySecret });
    else {
      clients.push({ mode: "openapi", id: legacyId, secret: legacySecret });
      clients.push({ mode: "hub", id: legacyId, secret: legacySecret });
    }
  }
  return clients.filter((client, index) => clients.findIndex((candidate) =>
    candidate.mode === client.mode && candidate.id === client.id) === index);
}

async function fetchNaverPage(api, query, start, display) {
  const params = new URLSearchParams({ query, display: String(display), start: String(start), sort: "date" });
  const endpoint = api.mode === "hub"
    ? `https://naverapihub.apigw.ntruss.com/search/v1/news?${params}`
    : `https://openapi.naver.com/v1/search/news.json?${params}`;
  const headers = api.mode === "hub"
    ? { "X-NCP-APIGW-API-KEY-ID": api.id, "X-NCP-APIGW-API-KEY": api.secret }
    : { "X-Naver-Client-Id": api.id, "X-Naver-Client-Secret": api.secret };
  const response = await fetch(endpoint, {
    headers: { accept: "application/json", ...headers },
    signal: AbortSignal.timeout(30_000),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const code = payload?.errorCode ?? payload?.error?.errorCode ?? response.status;
    throw new Error(`naver_api_${api.mode}_${code}`);
  }
  return payload;
}

function parseNaverItems(payload, query) {
  if (!Array.isArray(payload?.items)) return [];
  return payload.items.flatMap((record) => {
    const sourceUrl = typeof record.originallink === "string" && record.originallink.trim()
      ? record.originallink.trim()
      : typeof record.link === "string" ? record.link.trim() : "";
    if (!sourceUrl) return [];
    try {
      const normalizedUrl = normalizeArticleUrl(sourceUrl);
      const parsed = new URL(normalizedUrl);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return [];
      const domain = normalizedHost(parsed);
      if (!domain) return [];
      const published = new Date(typeof record.pubDate === "string" ? record.pubDate : "");
      return [{
        normalizedUrl,
        urlHash: createHash("sha256").update(normalizedUrl).digest("hex"),
        domain,
        title: cleanInlineText(record.title),
        description: cleanInlineText(record.description),
        publishedAtIso: Number.isNaN(published.getTime()) ? new Date().toISOString() : published.toISOString(),
        queries: new Set([query]),
      }];
    } catch {
      return [];
    }
  });
}

async function extractWithRetries(candidate, stats) {
  let lastError;
  for (let attempt = 1; attempt <= options.retries; attempt += 1) {
    await waitForHost(candidate.normalizedUrl);
    try {
      return await extractNewsArticle(candidate.normalizedUrl, {
        timeoutMs: 20_000,
        maxResponseBytes: 2_000_000,
        maxContentChars: 500_000,
        minContentChars: 80,
        maxRedirects: 4,
        userAgent: USER_AGENT,
      });
    } catch (error) {
      lastError = error;
      const message = safeError(error);
      if (isRobotsStop(message)) throw error;
      if (isPermanentNotFound(message)) {
        stats.notFound += 1;
        await saveUrlAttempt(candidate, "not_found", message, attempt);
        return null;
      }
      if (attempt >= options.retries) break;
      stats.retried += 1;
      const backoffMs = retryDelayMs(message, attempt);
      console.error(JSON.stringify({
        phase: "retry",
        domain: candidate.domain,
        url: candidate.normalizedUrl,
        attempt,
        backoffMs,
        error: message,
        at: new Date().toISOString(),
      }));
      await interruptibleDelay(backoffMs);
    }
  }
  throw lastError ?? new Error("article_extraction_failed");
}

async function storeArticle(candidate, article, articleBody) {
  const collectedAtIso = new Date().toISOString();
  const publishedAtIso = normalizedPublishedTime(article.publishedTime, candidate.publishedAtIso);
  const scenarioId = classifyScenario(`${article.title}\n${articleBody}`);
  const articleId = `naver:${candidate.urlHash.slice(0, 32)}`;
  const canonicalUrl = normalizeArticleUrl(article.finalUrl);
  const extraction = {
    status: "extracted",
    requestedUrl: article.requestedUrl,
    finalUrl: article.finalUrl,
    method: article.method,
    charCount: articleBody.length,
    wordCount: countWords(articleBody),
    truncated: article.truncated,
    robots: article.robots,
  };
  const payload = {
    id: articleId,
    scenarioId: scenarioId === "industry" ? undefined : scenarioId,
    tag: scenarioTag(scenarioId),
    title: cleanTitle(article.title || candidate.title),
    source: article.siteName ?? candidate.domain,
    sourceUrl: canonicalUrl,
    articleUrl: canonicalUrl,
    sourceType: "NAVER API",
    time: publishedAtIso.slice(0, 10),
    publishedAt: publishedAtIso.slice(0, 10),
    publishedAtIso,
    location: "대한민국",
    sentiment: "중립",
    confidence: 100,
    summary: cleanSummary(article.excerpt || candidate.description, articleBody),
    paragraphs: articleBody.split(/\n{2,}/).filter(Boolean).slice(0, 3),
    evidence: [],
    entities: [],
    relations: [],
    affected: [],
    discoveryQueries: [...candidate.queries],
  };

  await pool.execute(`
    INSERT INTO news_items (
      storage_key, article_id, scenario_id, source_url, published_at_iso,
      collected_at_iso, provider, article_no, article_url, article_body,
      article_extraction_json, article_extracted_at_iso, payload_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
    ON DUPLICATE KEY UPDATE
      scenario_id = VALUES(scenario_id),
      source_url = VALUES(source_url),
      published_at_iso = VALUES(published_at_iso),
      collected_at_iso = VALUES(collected_at_iso),
      provider = VALUES(provider),
      article_url = VALUES(article_url),
      article_body = VALUES(article_body),
      article_extraction_json = VALUES(article_extraction_json),
      article_extracted_at_iso = VALUES(article_extracted_at_iso),
      payload_json = VALUES(payload_json)
  `, [
    STORAGE_KEY,
    articleId,
    scenarioId,
    canonicalUrl,
    publishedAtIso,
    collectedAtIso,
    PROVIDER,
    canonicalUrl,
    articleBody,
    JSON.stringify(extraction),
    collectedAtIso,
    JSON.stringify(payload),
  ]);
}

async function ensureSchema() {
  await pool.execute(`
    CREATE TABLE IF NOT EXISTS news_items (
      news_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      storage_key VARCHAR(191) NOT NULL,
      article_id VARCHAR(191) NOT NULL,
      scenario_id VARCHAR(32) NOT NULL,
      source_url TEXT NOT NULL,
      published_at_iso VARCHAR(40) NOT NULL,
      collected_at_iso VARCHAR(40) NOT NULL,
      provider VARCHAR(64) NOT NULL,
      article_no BIGINT UNSIGNED,
      article_url TEXT,
      article_body MEDIUMTEXT,
      article_extraction_json LONGTEXT,
      article_extracted_at_iso VARCHAR(40),
      payload_json LONGTEXT NOT NULL,
      PRIMARY KEY (storage_key, article_id),
      UNIQUE INDEX ux_news_items_news_id (news_id),
      INDEX idx_news_items_storage_published (storage_key, published_at_iso),
      INDEX idx_news_items_article_no (article_no)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  `);
  await ensureNewsIdColumn();
  await pool.execute(`
    CREATE TABLE IF NOT EXISTS news_publisher_sources (
      publisher_domain VARCHAR(191) NOT NULL,
      display_name VARCHAR(191) NOT NULL,
      status VARCHAR(32) NOT NULL,
      discovered_via VARCHAR(64) NOT NULL,
      sample_url TEXT NOT NULL,
      robots_allowed TINYINT(1),
      attempt_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      success_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      failure_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      last_error VARCHAR(191),
      discovered_at_iso VARCHAR(40) NOT NULL,
      last_checked_at_iso VARCHAR(40),
      last_success_at_iso VARCHAR(40),
      PRIMARY KEY (publisher_domain),
      INDEX idx_news_publisher_sources_status (status, publisher_domain)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  `);
  await pool.execute(`
    CREATE TABLE IF NOT EXISTS news_url_crawl_attempts (
      url_hash CHAR(64) NOT NULL,
      publisher_domain VARCHAR(191) NOT NULL,
      article_url TEXT NOT NULL,
      status VARCHAR(32) NOT NULL,
      attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
      last_error VARCHAR(191),
      updated_at_iso VARCHAR(40) NOT NULL,
      PRIMARY KEY (url_hash),
      INDEX idx_news_url_attempts_status (status, updated_at_iso),
      INDEX idx_news_url_attempts_domain (publisher_domain, updated_at_iso)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  `);
}

async function ensureNewsIdColumn() {
  const [tables] = await pool.execute(`
    SELECT COUNT(*) AS table_count
    FROM information_schema.tables
    WHERE table_schema = DATABASE() AND table_name = 'news_items'
  `);
  if (Number(tables[0]?.table_count ?? 0) === 0) throw new Error("news_items_table_required");
  try {
    await pool.execute(`
      ALTER TABLE news_items
      ADD COLUMN news_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      ADD UNIQUE INDEX ux_news_items_news_id (news_id)
    `);
  } catch (error) {
    if (!/duplicate column|duplicate key name/i.test(safeError(error))) throw error;
  }
}

async function seedPublisherRegistryFromExistingNews() {
  const [rows] = await pool.execute(`
    SELECT source_url, article_url, provider, article_extracted_at_iso
    FROM news_items
    WHERE article_body IS NOT NULL
      AND CHAR_LENGTH(article_body) > 0
      AND JSON_UNQUOTE(JSON_EXTRACT(article_extraction_json, '$.robots.status')) = 'allowed'
  `);
  const domains = new Map();
  for (const row of rows) {
    const rawUrl = row.article_url || row.source_url;
    try {
      const normalizedUrl = normalizeArticleUrl(rawUrl);
      const domain = normalizedHost(new URL(normalizedUrl));
      const existing = domains.get(domain);
      if (existing) {
        existing.successCount += 1;
        if ((row.article_extracted_at_iso ?? "") > existing.lastSuccessAt) {
          existing.lastSuccessAt = row.article_extracted_at_iso;
          existing.sampleUrl = normalizedUrl;
        }
      } else {
        domains.set(domain, {
          displayName: row.provider || domain,
          sampleUrl: normalizedUrl,
          successCount: 1,
          lastSuccessAt: row.article_extracted_at_iso || new Date().toISOString(),
        });
      }
    } catch {
      // Ignore malformed historical URLs.
    }
  }
  const now = new Date().toISOString();
  for (const [domain, value] of domains) {
    await pool.execute(`
      INSERT INTO news_publisher_sources (
        publisher_domain, display_name, status, discovered_via, sample_url,
        robots_allowed, attempt_count, success_count, failure_count,
        last_error, discovered_at_iso, last_checked_at_iso, last_success_at_iso
      ) VALUES (?, ?, 'active', 'existing_db', ?, 1, ?, ?, 0, NULL, ?, ?, ?)
      ON DUPLICATE KEY UPDATE
        status = 'active',
        sample_url = VALUES(sample_url),
        robots_allowed = 1,
        success_count = GREATEST(success_count, VALUES(success_count)),
        last_error = NULL,
        last_checked_at_iso = VALUES(last_checked_at_iso),
        last_success_at_iso = GREATEST(last_success_at_iso, VALUES(last_success_at_iso))
    `, [
      domain,
      value.displayName.slice(0, 191),
      value.sampleUrl,
      value.successCount,
      value.successCount,
      now,
      now,
      value.lastSuccessAt,
    ]);
  }
}

async function registerPublisherCandidate(candidate) {
  const now = new Date().toISOString();
  await pool.execute(`
    INSERT INTO news_publisher_sources (
      publisher_domain, display_name, status, discovered_via, sample_url,
      robots_allowed, discovered_at_iso
    ) VALUES (?, ?, 'discovered', 'naver_api', ?, NULL, ?)
    ON DUPLICATE KEY UPDATE
      sample_url = VALUES(sample_url),
      discovered_via = IF(discovered_via = 'existing_db', discovered_via, VALUES(discovered_via))
  `, [candidate.domain, candidate.domain, candidate.normalizedUrl, now]);
}

async function recordPublisherSuccess(candidate, siteName, checkedAt) {
  const now = new Date().toISOString();
  await pool.execute(`
    UPDATE news_publisher_sources SET
      display_name = ?,
      status = 'active',
      robots_allowed = 1,
      attempt_count = attempt_count + 1,
      success_count = success_count + 1,
      last_error = NULL,
      last_checked_at_iso = ?,
      last_success_at_iso = ?
    WHERE publisher_domain = ?
  `, [(siteName || candidate.domain).slice(0, 191), checkedAt || now, now, candidate.domain]);
}

async function recordPublisherFailure(candidate, message) {
  const now = new Date().toISOString();
  const status = failureStatus(message);
  await pool.execute(`
    UPDATE news_publisher_sources SET
      status = CASE WHEN success_count > 0 THEN status ELSE ? END,
      robots_allowed = CASE
        WHEN ? = 'robots_disallowed' THEN 0
        WHEN ? = 'robots_unavailable' THEN NULL
        ELSE robots_allowed
      END,
      attempt_count = attempt_count + 1,
      failure_count = failure_count + 1,
      last_error = ?,
      last_checked_at_iso = ?
    WHERE publisher_domain = ?
  `, [status, status, status, message, now, candidate.domain]);
}

async function saveUrlAttempt(candidate, status, lastError, attemptCount) {
  await pool.execute(`
    INSERT INTO news_url_crawl_attempts (
      url_hash, publisher_domain, article_url, status, attempt_count, last_error, updated_at_iso
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON DUPLICATE KEY UPDATE
      publisher_domain = VALUES(publisher_domain),
      article_url = VALUES(article_url),
      status = VALUES(status),
      attempt_count = attempt_count + VALUES(attempt_count),
      last_error = VALUES(last_error),
      updated_at_iso = VALUES(updated_at_iso)
  `, [
    candidate.urlHash,
    candidate.domain,
    candidate.normalizedUrl,
    status,
    attemptCount,
    lastError,
    new Date().toISOString(),
  ]);
}

async function loadExistingUrls() {
  const [rows] = await pool.execute(`
    SELECT source_url, article_url
    FROM news_items
    WHERE article_body IS NOT NULL AND CHAR_LENGTH(article_body) > 0
      AND JSON_UNQUOTE(JSON_EXTRACT(article_extraction_json, '$.robots.status')) = 'allowed'
  `);
  return new Set(rows.flatMap((row) => [row.article_url, row.source_url].flatMap((value) => {
    try {
      return value ? [normalizeArticleUrl(value)] : [];
    } catch {
      return [];
    }
  })));
}

async function loadRecentFailureHashes() {
  const cutoff = new Date(Date.now() - options.failureRetryHours * 60 * 60_000).toISOString();
  const [rows] = await pool.execute(`
    SELECT url_hash
    FROM news_url_crawl_attempts
    WHERE status <> 'stored' AND updated_at_iso >= ?
  `, [cutoff]);
  return new Set(rows.map((row) => row.url_hash));
}

async function registryStats() {
  const [rows] = await pool.execute(`
    SELECT status, COUNT(*) AS publishers, SUM(success_count) AS successes, SUM(failure_count) AS failures
    FROM news_publisher_sources
    GROUP BY status
    ORDER BY status
  `);
  return rows;
}

async function databaseStats() {
  const [rows] = await pool.execute(`
    SELECT COUNT(*) AS rows_count,
      SUM(OCTET_LENGTH(article_body)) AS body_bytes,
      COUNT(DISTINCT LOWER(SUBSTRING_INDEX(SUBSTRING_INDEX(source_url, '://', -1), '/', 1))) AS domains
    FROM news_items
    WHERE storage_key = ?
  `, [STORAGE_KEY]);
  return rows[0];
}

function waitForHost(inputUrl) {
  const host = new URL(inputUrl).host.toLowerCase();
  const previous = hostQueues.get(host) ?? Promise.resolve();
  const scheduled = previous.then(async () => {
    const lastRequestAt = hostLastRequestAt.get(host) ?? 0;
    const remaining = options.hostDelayMs - (Date.now() - lastRequestAt);
    if (remaining > 0) await interruptibleDelay(remaining);
    hostLastRequestAt.set(host, Date.now());
  });
  hostQueues.set(host, scheduled.catch(() => {}));
  return scheduled;
}

function normalizeArticleUrl(value) {
  const url = new URL(value);
  url.hash = "";
  for (const key of [...url.searchParams.keys()]) {
    if (TRACKING_PARAMETERS.has(key.toLowerCase())) url.searchParams.delete(key);
  }
  return url.toString().replace(/\/$/, "");
}

function normalizedHost(url) {
  return url.hostname.toLowerCase().replace(/^www\./, "");
}

function cleanArticleBody(domain, value) {
  let output = value.replace(/\r/g, "").replace(/[ \t]+\n/g, "\n").trim();
  if (domain === "sedaily.com") {
    output = output.replace(/^뉴스 듣기\s+글자 크기[\s\S]{0,300}?다음 채널구독\s*/u, "");
  }
  return output.replace(/\n{3,}/g, "\n\n").trim();
}

function cleanInlineText(value) {
  if (typeof value !== "string") return "";
  return value
    .replace(/<[^>]*>/g, " ")
    .replace(/&quot;/g, '"')
    .replace(/&apos;|&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

function cleanTitle(value) {
  return cleanInlineText(value).replace(/^\[(?:헬로티 HelloT|서울경제)\]\s*/i, "");
}

function cleanSummary(value, body) {
  return (cleanInlineText(value) || body.slice(0, 300)).slice(0, 1_000);
}

function normalizedPublishedTime(value, fallback) {
  const parsed = new Date(value || fallback);
  return Number.isNaN(parsed.getTime()) ? new Date().toISOString() : parsed.toISOString();
}

function classifyScenario(text) {
  if (/(?:배터리|이차전지|2차전지|양극재|음극재|전고체|리튬)/i.test(text)) return "battery";
  if (/(?:반도체|HBM|메모리|파운드리|웨이퍼|칩셋|낸드|DRAM|D램)/i.test(text)) return "semiconductor";
  if (/(?:지배구조|주주총회|자사주|유상증자|최대주주|이사회)/i.test(text)) return "governance";
  return "industry";
}

function scenarioTag(scenarioId) {
  if (scenarioId === "battery") return "배터리";
  if (scenarioId === "semiconductor") return "반도체";
  if (scenarioId === "governance") return "지배구조";
  return "산업";
}

function countWords(value) {
  return value.trim() ? value.trim().split(/\s+/).length : 0;
}

function failureStatus(message) {
  if (message === "article_robots_disallowed") return "robots_disallowed";
  if (message === "article_robots_unavailable") return "robots_unavailable";
  if (/article_fetch_http_(?:403|406|429)/.test(message)) return "http_blocked";
  if (isPermanentNotFound(message)) return "not_found";
  return "extract_failed";
}

function retryDelayMs(message, attempt) {
  if (/article_fetch_http_(?:403|406|429)/.test(message)) {
    return Math.min(30 * 60_000, 60_000 * (2 ** (attempt - 1)));
  }
  return Math.min(60_000, 2_000 * (2 ** (attempt - 1)));
}

function isPermanentNotFound(message) {
  return message === "article_not_found" || /article_fetch_http_(?:404|410)/.test(message);
}

function isRobotsStop(message) {
  return message === "article_robots_disallowed" || message === "article_robots_unavailable";
}

function readOptions(args) {
  const values = new Map();
  const flags = new Set();
  for (let index = 0; index < args.length; index += 1) {
    const token = args[index];
    if (!token.startsWith("--")) throw new Error(`unexpected argument: ${token}`);
    const name = token.slice(2);
    if (name === "watch" || name === "seed-only") {
      flags.add(name);
      continue;
    }
    const value = args[index + 1];
    if (value === undefined || value.startsWith("--")) throw new Error(`missing value for --${name}`);
    values.set(name, value);
    index += 1;
  }
  const queries = values.has("queries")
    ? values.get("queries").split("|").map((value) => value.trim()).filter(Boolean)
    : DEFAULT_QUERIES;
  if (!queries.length) throw new Error("at least one discovery query is required");
  return {
    watch: flags.has("watch"),
    seedOnly: flags.has("seed-only"),
    queries,
    intervalMinutes: integerOption(values.get("interval-minutes") ?? process.env.NAVER_DISCOVERY_INTERVAL_MINUTES, DEFAULT_INTERVAL_MINUTES, 15, 1_440),
    backfillPages: integerOption(values.get("backfill-pages") ?? process.env.NAVER_DISCOVERY_BACKFILL_PAGES, DEFAULT_BACKFILL_PAGES, 1, 10),
    concurrency: integerOption(values.get("concurrency"), 8, 1, 20),
    retries: integerOption(values.get("retries"), 3, 1, 10),
    limit: integerOption(values.get("limit"), 10_000, 1, 50_000),
    hostDelayMs: integerOption(values.get("host-delay-ms"), DEFAULT_HOST_DELAY_MS, 1_000, 60_000),
    failureRetryHours: integerOption(values.get("failure-retry-hours"), 6, 1, 168),
    progressEvery: integerOption(values.get("progress-every"), 50, 1, 10_000),
  };
}

async function mysqlConnectionOptions(value) {
  const url = new URL(value);
  if (url.protocol !== "mysql:") throw new Error("MYSQL_URL must use mysql://");
  const caPath = process.env.MYSQL_SSL_CA_PATH?.trim();
  const ssl = caPath
    ? { ca: await readFile(caPath, "utf8"), rejectUnauthorized: true, servername: url.hostname }
    : undefined;
  return {
    host: url.hostname,
    port: Number(url.port || 3306),
    user: decodeURIComponent(url.username),
    password: decodeURIComponent(url.password),
    database: decodeURIComponent(url.pathname.slice(1)),
    charset: "utf8mb4",
    waitForConnections: true,
    connectionLimit: Math.max(4, options.concurrency + 1),
    queueLimit: 0,
    enableKeepAlive: true,
    keepAliveInitialDelay: 0,
    ssl,
  };
}

function integerOption(value, fallback, min, max) {
  if (value === undefined || value === "") return fallback;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`integer option must be between ${min} and ${max}`);
  }
  return parsed;
}

function loadLocalEnvironment() {
  for (const file of [".env.local", ".env"]) {
    try {
      process.loadEnvFile(file);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
}

function requiredEnvironment(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function requestStop() {
  stopRequested = true;
  console.log(JSON.stringify({ phase: "stop_requested", at: new Date().toISOString() }));
}

async function interruptibleDelay(milliseconds) {
  const deadline = Date.now() + milliseconds;
  while (!stopRequested && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, Math.min(5_000, deadline - Date.now())));
  }
}

function safeError(error) {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/mysql:\/\/[^\s@]+@/gi, "mysql://***@").slice(0, 191);
}
