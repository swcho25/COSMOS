import { readFile } from "node:fs/promises";
import { createPool } from "mysql2/promise";
import { extractNewsArticle } from "../lib/news-article-extractor.ts";
import { assertRobotsAllowed } from "../lib/robots-policy.ts";

const USER_AGENT = "NexusRiskArticleExtractor/1.0 (+news intelligence; source-linked)";
const PROVIDER_SUFFIX = "direct sitemap crawler";
const DEFAULT_WATCH_MINUTES = 60;
const TRACKING_PARAMETERS = new Set([
  "ref",
  "inflow",
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_content",
  "utm_term",
]);

const SOURCE_CONFIGS = [
  {
    id: "kpenews",
    label: "한국정경신문",
    sitemapUrls: ["https://kpenews.com/sitemap_googlenews.xml"],
    pageUrls: [],
    hostDelayMs: 5_000,
    maxArticles: 500,
    externalId(url) {
      return normalizedHost(url) === "kpenews.com" && url.pathname.toLowerCase() === "/view.aspx"
        ? positiveExternalId(url.searchParams.get("No") ?? url.searchParams.get("no"))
        : null;
    },
  },
  {
    id: "mdtoday",
    label: "메디컬투데이",
    sitemapUrls: ["https://www.mdtoday.co.kr/sitemap.xml"],
    pageUrls: [],
    hostDelayMs: 1_500,
    maxArticles: 500,
    externalId(url) {
      return normalizedHost(url) === "mdtoday.co.kr" && url.pathname === "/news/articleView.html"
        ? positiveExternalId(url.searchParams.get("idxno"))
        : null;
    },
  },
  {
    id: "sedaily",
    label: "서울경제",
    sitemapUrls: ["https://www.sedaily.com/sitemap/latestnews"],
    pageUrls: [],
    hostDelayMs: 1_500,
    maxArticles: 500,
    externalId(url) {
      if (normalizedHost(url) !== "sedaily.com") return null;
      return positiveExternalId(url.pathname.match(/^\/article\/(\d+)\/?$/i)?.[1]);
    },
  },
  {
    id: "newspim",
    label: "뉴스핌",
    sitemapUrls: [
      "https://www.newspim.com/sitemap/recent/all_1",
      "https://www.newspim.com/sitemap/recent/all_2",
    ],
    pageUrls: [],
    hostDelayMs: 1_200,
    maxArticles: 2_000,
    externalId(url) {
      if (normalizedHost(url) !== "newspim.com") return null;
      return positiveExternalId(url.pathname.match(/^\/news\/view\/(\d+)\/?$/i)?.[1]);
    },
  },
  {
    id: "newstomato",
    label: "뉴스토마토",
    sitemapUrls: [],
    pageUrls: ["http://www.newstomato.com/"],
    hostDelayMs: 1_500,
    maxArticles: 500,
    externalId(url) {
      return normalizedHost(url) === "newstomato.com" && url.pathname.toLowerCase() === "/readnews.aspx"
        ? positiveExternalId(url.searchParams.get("no"))
        : null;
    },
  },
];

const options = readOptions(process.argv.slice(2));
const selectedSources = SOURCE_CONFIGS.filter((source) => options.sources.has(source.id));
if (!selectedSources.length) throw new Error("no publisher sources selected");

const pool = createPool(await mysqlConnectionOptionsFromEnvironment());
const hostQueues = new Map();
const hostLastRequestAt = new Map();
let stopRequested = false;

process.on("SIGINT", requestStop);
process.on("SIGTERM", requestStop);

try {
  await ensureSchema();
  do {
    await runCycle();
    if (!options.watch || stopRequested) break;
    console.log(JSON.stringify({
      phase: "watch_wait",
      minutes: options.intervalMinutes,
      nextAt: new Date(Date.now() + options.intervalMinutes * 60_000).toISOString(),
    }));
    await interruptibleDelay(options.intervalMinutes * 60_000);
  } while (!stopRequested);
} catch (error) {
  console.error(JSON.stringify({ phase: "fatal", error: safeError(error), at: new Date().toISOString() }));
  process.exitCode = 1;
} finally {
  await pool.end();
}

async function runCycle() {
  const cycleStartedAt = new Date().toISOString();
  const existingUrls = await loadExistingUrls();
  const discoveries = await Promise.all(selectedSources.map(discoverSource));
  const candidates = interleaveCandidates(discoveries);
  let cursor = 0;
  const stats = Object.fromEntries(selectedSources.map((source) => [source.id, {
    discovered: discoveries.find((result) => result.source.id === source.id)?.candidates.length ?? 0,
    requested: 0,
    stored: 0,
    existing: 0,
    notFound: 0,
    blocked: 0,
    failed: 0,
    retried: 0,
  }]));

  console.log(JSON.stringify({
    phase: "cycle_start",
    sources: selectedSources.map((source) => source.id),
    candidates: candidates.length,
    existingValidUrls: existingUrls.size,
    concurrency: options.concurrency,
    startedAt: cycleStartedAt,
  }));

  const workers = Array.from({ length: Math.min(options.concurrency, candidates.length) }, async () => {
    while (!stopRequested) {
      const candidate = candidates[cursor];
      cursor += 1;
      if (!candidate) return;
      const sourceStats = stats[candidate.source.id];
      const normalizedUrl = normalizeArticleUrl(candidate.url);
      if (existingUrls.has(normalizedUrl)) {
        sourceStats.existing += 1;
        continue;
      }

      try {
        const article = await extractWithRetries(candidate, sourceStats);
        if (!article) continue;
        const cleanedBody = cleanArticleBody(candidate.source.id, article.textContent);
        if (cleanedBody.length < 80) throw new Error("article_body_not_found_after_cleanup");
        await storeArticle(candidate, article, cleanedBody);
        await saveAttempt(candidate, "stored", null, 1);
        existingUrls.add(normalizedUrl);
        sourceStats.stored += 1;
      } catch (error) {
        const message = safeError(error);
        const blocked = isRobotsStop(message);
        sourceStats[blocked ? "blocked" : "failed"] += 1;
        await saveAttempt(candidate, blocked ? "blocked" : "failed", message, options.retries);
        console.error(JSON.stringify({
          phase: "article_failed",
          source: candidate.source.id,
          externalId: candidate.externalId,
          url: candidate.url,
          error: message,
          at: new Date().toISOString(),
        }));
      }

      const completed = Object.values(stats).reduce((total, value) =>
        total + value.stored + value.existing + value.notFound + value.blocked + value.failed, 0);
      if (completed > 0 && completed % options.progressEvery === 0) {
        console.log(JSON.stringify({ phase: "progress", completed, total: candidates.length, stats, at: new Date().toISOString() }));
      }
    }
  });
  await Promise.all(workers);

  console.log(JSON.stringify({
    phase: stopRequested ? "cycle_stopped" : "cycle_complete",
    stats,
    database: await readDatabaseStats(),
    startedAt: cycleStartedAt,
    finishedAt: new Date().toISOString(),
  }));
}

function interleaveCandidates(discoveries) {
  const output = [];
  const maxLength = Math.max(0, ...discoveries.map((result) => result.candidates.length));
  for (let index = 0; index < maxLength; index += 1) {
    for (const discovery of discoveries) {
      const candidate = discovery.candidates[index];
      if (candidate) output.push(candidate);
    }
  }
  return output;
}

async function discoverSource(source) {
  const candidates = new Map();
  const discoveryErrors = [];
  for (const sitemapUrl of source.sitemapUrls) {
    try {
      const xml = await fetchDiscoveryDocument(source, sitemapUrl);
      for (const value of sitemapLocations(xml)) addDiscoveredUrl(source, value, candidates);
    } catch (error) {
      discoveryErrors.push({ url: sitemapUrl, error: safeError(error) });
    }
  }
  for (const pageUrl of source.pageUrls) {
    try {
      const html = await fetchDiscoveryDocument(source, pageUrl);
      for (const value of pageLinks(html, pageUrl)) addDiscoveredUrl(source, value, candidates);
    } catch (error) {
      discoveryErrors.push({ url: pageUrl, error: safeError(error) });
    }
  }

  const limit = Math.min(source.maxArticles, options.limitPerSource);
  const output = [...candidates.values()]
    .sort((left, right) => right.externalId - left.externalId)
    .slice(0, limit);
  console.log(JSON.stringify({
    phase: "discovery",
    source: source.id,
    label: source.label,
    candidates: output.length,
    errors: discoveryErrors,
    at: new Date().toISOString(),
  }));
  return { source, candidates: output };
}

async function fetchDiscoveryDocument(source, inputUrl) {
  await waitForHost(inputUrl, source.hostDelayMs);
  await assertRobotsAllowed(inputUrl, { userAgent: USER_AGENT });
  const response = await fetch(inputUrl, {
    headers: {
      accept: "application/xml,text/xml,text/html;q=0.9,*/*;q=0.1",
      "accept-language": "ko-KR,ko;q=0.9,en;q=0.5",
      "user-agent": USER_AGENT,
    },
    redirect: "follow",
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error(`discovery_fetch_http_${response.status}`);
  const contentLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(contentLength) && contentLength > 5_000_000) throw new Error("discovery_response_too_large");
  const text = await response.text();
  if (Buffer.byteLength(text, "utf8") > 5_000_000) throw new Error("discovery_response_too_large");
  return text;
}

function addDiscoveredUrl(source, value, candidates) {
  try {
    const normalized = normalizeArticleUrl(value);
    const parsed = new URL(normalized);
    const externalId = source.externalId(parsed);
    if (externalId === null) return;
    candidates.set(normalized, { source, url: normalized, externalId });
  } catch {
    // Ignore malformed links from publisher pages and sitemaps.
  }
}

async function extractWithRetries(candidate, sourceStats) {
  let lastError;
  for (let attempt = 1; attempt <= options.retries; attempt += 1) {
    await waitForHost(candidate.url, candidate.source.hostDelayMs);
    sourceStats.requested += 1;
    try {
      const article = await extractNewsArticle(candidate.url, {
        timeoutMs: 20_000,
        maxResponseBytes: 2_000_000,
        maxContentChars: 500_000,
        minContentChars: 80,
        maxRedirects: 4,
        userAgent: USER_AGENT,
      });
      return article;
    } catch (error) {
      lastError = error;
      const message = safeError(error);
      if (isRobotsStop(message)) throw error;
      if (isPermanentNotFound(message)) {
        sourceStats.notFound += 1;
        await saveAttempt(candidate, "not_found", message, attempt);
        return null;
      }
      if (attempt >= options.retries) break;
      sourceStats.retried += 1;
      const backoffMs = retryDelayMs(message, attempt, candidate.source.id);
      console.error(JSON.stringify({
        phase: "retry",
        source: candidate.source.id,
        externalId: candidate.externalId,
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
  const publishedAtIso = normalizedPublishedTime(article.publishedTime, collectedAtIso);
  const articleId = `${candidate.source.id}:${candidate.externalId}`;
  const scenarioId = classifyScenario(`${article.title}\n${articleBody}`);
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
    title: cleanTitle(article.title),
    source: candidate.source.label,
    sourceUrl: normalizeArticleUrl(article.finalUrl),
    articleUrl: normalizeArticleUrl(article.finalUrl),
    sourceType: "언론 보도",
    time: publishedAtIso.slice(0, 10),
    publishedAt: publishedAtIso.slice(0, 10),
    publishedAtIso,
    location: "대한민국",
    sentiment: "중립",
    confidence: 100,
    summary: cleanSummary(article.excerpt, articleBody),
    paragraphs: articleBody.split(/\n{2,}/).filter(Boolean).slice(0, 3),
    evidence: [],
    entities: [],
    relations: [],
    affected: [],
  };
  const canonicalUrl = normalizeArticleUrl(article.finalUrl);

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
    `production:${candidate.source.id}:sitemap`,
    articleId,
    scenarioId,
    canonicalUrl,
    publishedAtIso,
    collectedAtIso,
    `${candidate.source.label} ${PROVIDER_SUFFIX}`,
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
    CREATE TABLE IF NOT EXISTS news_crawl_attempts (
      source_name VARCHAR(64) NOT NULL,
      external_id BIGINT UNSIGNED NOT NULL,
      article_url TEXT NOT NULL,
      status VARCHAR(32) NOT NULL,
      attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
      last_error VARCHAR(191),
      updated_at_iso VARCHAR(40) NOT NULL,
      PRIMARY KEY (source_name, external_id),
      INDEX idx_news_crawl_attempts_status (source_name, status, external_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  `);
}

async function ensureNewsIdColumn() {
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

async function loadExistingUrls() {
  const [rows] = await pool.execute(`
    SELECT source_url, article_url
    FROM news_items
    WHERE article_body IS NOT NULL
      AND CHAR_LENGTH(article_body) > 0
      AND JSON_UNQUOTE(JSON_EXTRACT(article_extraction_json, '$.robots.status')) = 'allowed'
  `);
  return new Set(rows.flatMap((row) => {
    const values = [row.article_url, row.source_url].filter(Boolean);
    return values.flatMap((value) => {
      try {
        return [normalizeArticleUrl(value)];
      } catch {
        return [];
      }
    });
  }));
}

async function saveAttempt(candidate, status, lastError, attemptCount) {
  await pool.execute(`
    INSERT INTO news_crawl_attempts (
      source_name, external_id, article_url, status, attempt_count, last_error, updated_at_iso
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON DUPLICATE KEY UPDATE
      article_url = VALUES(article_url),
      status = VALUES(status),
      attempt_count = attempt_count + VALUES(attempt_count),
      last_error = VALUES(last_error),
      updated_at_iso = VALUES(updated_at_iso)
  `, [
    candidate.source.id,
    candidate.externalId,
    candidate.url,
    status,
    attemptCount,
    lastError,
    new Date().toISOString(),
  ]);
}

async function readDatabaseStats() {
  const [rows] = await pool.execute(`
    SELECT storage_key,
      COUNT(*) AS rows_count,
      SUM(OCTET_LENGTH(article_body)) AS body_bytes
    FROM news_items
    WHERE storage_key IN (${selectedSources.map(() => "?").join(",")})
      AND article_body IS NOT NULL
      AND CHAR_LENGTH(article_body) > 0
      AND JSON_UNQUOTE(JSON_EXTRACT(article_extraction_json, '$.robots.status')) = 'allowed'
    GROUP BY storage_key
    ORDER BY storage_key
  `, selectedSources.map((source) => `production:${source.id}:sitemap`));
  return rows;
}

function waitForHost(inputUrl, delayMs) {
  const host = new URL(inputUrl).host.toLowerCase();
  const previous = hostQueues.get(host) ?? Promise.resolve();
  const scheduled = previous.then(async () => {
    const lastRequestAt = hostLastRequestAt.get(host) ?? 0;
    const remaining = delayMs - (Date.now() - lastRequestAt);
    if (remaining > 0) await interruptibleDelay(remaining);
    hostLastRequestAt.set(host, Date.now());
  });
  hostQueues.set(host, scheduled.catch(() => {}));
  return scheduled;
}

function sitemapLocations(xml) {
  return [...xml.matchAll(/<loc>\s*([^<]+?)\s*<\/loc>/gi)]
    .map((match) => decodeXml(match[1].trim()));
}

function pageLinks(html, pageUrl) {
  return [...html.matchAll(/href\s*=\s*["']([^"']+)["']/gi)].flatMap((match) => {
    try {
      return [new URL(decodeXml(match[1]), pageUrl).toString()];
    } catch {
      return [];
    }
  });
}

function normalizeArticleUrl(value) {
  const url = new URL(value);
  url.hash = "";
  for (const key of [...url.searchParams.keys()]) {
    if (TRACKING_PARAMETERS.has(key.toLowerCase())) url.searchParams.delete(key);
  }
  if ((url.protocol === "https:" && url.port === "443") || (url.protocol === "http:" && url.port === "80")) url.port = "";
  return url.toString().replace(/\/$/, "");
}

function normalizedHost(url) {
  return url.hostname.toLowerCase().replace(/^www\./, "");
}

function positiveExternalId(value) {
  if (!value || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function decodeXml(value) {
  return value
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");
}

function cleanArticleBody(sourceId, value) {
  let output = value.replace(/\r/g, "").replace(/[ \t]+\n/g, "\n").trim();
  if (sourceId === "sedaily") {
    output = output.replace(/^뉴스 듣기\s+글자 크기[\s\S]{0,300}?다음 채널구독\s*/u, "");
  }
  return output.replace(/\n{3,}/g, "\n\n").trim();
}

function cleanTitle(value) {
  return value.replace(/^\[(?:헬로티 HelloT|서울경제)\]\s*/i, "").trim();
}

function cleanSummary(excerpt, body) {
  const value = excerpt?.trim() || body.slice(0, 300);
  return value.slice(0, 1_000);
}

function normalizedPublishedTime(value, fallback) {
  if (!value) return fallback;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? fallback : parsed.toISOString();
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

function retryDelayMs(message, attempt, sourceId) {
  if (/article_fetch_http_(?:403|406|429)/.test(message)) {
    const base = sourceId === "kpenews" ? 120_000 : 60_000;
    return Math.min(30 * 60_000, base * (2 ** (attempt - 1)));
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
    if (name === "watch") {
      flags.add(name);
      continue;
    }
    const value = args[index + 1];
    if (value === undefined || value.startsWith("--")) throw new Error(`missing value for --${name}`);
    values.set(name, value);
    index += 1;
  }
  const requestedSources = new Set((values.get("sources") ?? SOURCE_CONFIGS.map((source) => source.id).join(","))
    .split(",").map((value) => value.trim()).filter(Boolean));
  const knownSources = new Set(SOURCE_CONFIGS.map((source) => source.id));
  for (const source of requestedSources) {
    if (!knownSources.has(source)) throw new Error(`unknown publisher source: ${source}`);
  }
  return {
    watch: flags.has("watch"),
    sources: requestedSources,
    intervalMinutes: integerOption(values.get("interval-minutes"), DEFAULT_WATCH_MINUTES, 15, 1_440),
    concurrency: integerOption(values.get("concurrency"), 5, 1, 10),
    retries: integerOption(values.get("retries"), 3, 1, 10),
    limitPerSource: integerOption(values.get("limit-per-source"), 2_500, 1, 20_000),
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
    connectionLimit: Math.max(3, options.concurrency + 1),
    queueLimit: 0,
    enableKeepAlive: true,
    keepAliveInitialDelay: 0,
    ssl,
  };
}

async function mysqlConnectionOptionsFromEnvironment() {
  const mysqlUrl = process.env.MYSQL_URL?.trim();
  if (mysqlUrl) return mysqlConnectionOptions(mysqlUrl);

  const host = requiredEnvironment("MYSQL_HOST");
  const caPath = process.env.MYSQL_SSL_CA_PATH?.trim();
  const ssl = caPath
    ? { ca: await readFile(caPath, "utf8"), rejectUnauthorized: true, servername: host }
    : undefined;
  return {
    host,
    port: Number(process.env.MYSQL_PORT?.trim() || 3306),
    user: requiredEnvironment("MYSQL_USER"),
    password: requiredEnvironment("MYSQL_PASSWORD"),
    database: requiredEnvironment("MYSQL_DATABASE"),
    charset: "utf8mb4",
    waitForConnections: true,
    connectionLimit: Math.max(3, options.concurrency + 1),
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
