import { readFile } from "node:fs/promises";
import { createPool } from "mysql2/promise";
import { extractNewsArticle } from "../lib/news-article-extractor.ts";

const SOURCE_NAME = "hellot";
const STORAGE_KEY = "production:hellot:no-range";
const PROVIDER = "HelloT direct article backfill";
const DEFAULT_START_NO = 10_000;
const DEFAULT_END_NO = 114_676;
const DEFAULT_DELAY_MS = 1_000;
const DEFAULT_CONCURRENCY = 1;
const DEFAULT_RETRIES = 4;
const USER_AGENT = "NexusRiskArticleExtractor/1.0 (+news intelligence; source-linked)";

const options = readOptions(process.argv.slice(2));
const pool = createPool(await mysqlConnectionOptionsFromEnvironment());
let stopRequested = false;
let cursor = options.start;
let lastRequestAt = 0;
let nextArticleNo = 1;
let rateLimiter = Promise.resolve();

const stats = {
  range: options.end - options.start + 1,
  considered: 0,
  requested: 0,
  stored: 0,
  existing: 0,
  notFound: 0,
  failed: 0,
  retried: 0,
};

process.on("SIGINT", requestStop);
process.on("SIGTERM", requestStop);

try {
  await ensureSchema();
  const resume = await loadResumeState();
  nextArticleNo = resume.nextArticleNo;

  console.log(JSON.stringify({
    phase: "start",
    source: SOURCE_NAME,
    storageKey: STORAGE_KEY,
    start: options.start,
    end: options.end,
    range: stats.range,
    delayMs: options.delayMs,
    concurrency: options.concurrency,
    retries: options.retries,
    alreadyStoredBodies: resume.existingNumbers.size,
    terminalNotFound: resume.notFoundNumbers.size,
    startedAt: new Date().toISOString(),
  }));

  const workers = Array.from({ length: options.concurrency }, (_, workerIndex) =>
    runWorker(workerIndex + 1, resume),
  );
  await Promise.all(workers);

  if (!stopRequested) await renumberArticles();
  const databaseStats = await readDatabaseStats();
  console.log(JSON.stringify({
    phase: stopRequested ? "stopped" : "complete",
    stats,
    database: databaseStats,
    finishedAt: new Date().toISOString(),
  }));
} catch (error) {
  console.error(JSON.stringify({
    phase: "fatal",
    error: safeError(error),
    stats,
    failedAt: new Date().toISOString(),
  }));
  process.exitCode = 1;
} finally {
  await pool.end();
}

async function runWorker(workerId, resume) {
  while (!stopRequested) {
    const articleNumber = takeNextNumber();
    if (articleNumber === null) return;
    stats.considered += 1;

    if (resume.existingNumbers.has(articleNumber)) {
      stats.existing += 1;
      continue;
    }
    if (resume.notFoundNumbers.has(articleNumber)) {
      stats.notFound += 1;
      continue;
    }

    const articleUrl = hellotArticleUrl(articleNumber);
    try {
      const article = await extractWithRetries(articleUrl, articleNumber);
      if (!article) continue;
      const articleNo = allocateArticleNo();
      await storeArticle(articleNumber, articleNo, article);
      await saveAttempt(articleNumber, articleUrl, "stored", null, 1);
      resume.existingNumbers.add(articleNumber);
      stats.stored += 1;
    } catch (error) {
      const message = safeError(error);
      stats.failed += 1;
      await saveAttempt(articleNumber, articleUrl, "failed", message, options.retries);
      console.error(JSON.stringify({
        phase: "article_failed",
        workerId,
        articleNumber,
        url: articleUrl,
        error: message,
        at: new Date().toISOString(),
      }));
      if (isRobotsStop(message)) {
        stopRequested = true;
        throw error;
      }
    }

    const completed = stats.stored + stats.existing + stats.notFound + stats.failed;
    if (completed > 0 && completed % options.progressEvery === 0) {
      console.log(JSON.stringify({
        phase: "progress",
        cursor,
        stats,
        at: new Date().toISOString(),
      }));
    }
  }
}

async function extractWithRetries(articleUrl, articleNumber) {
  let lastError;
  for (let attempt = 1; attempt <= options.retries; attempt += 1) {
    await waitForRateSlot();
    stats.requested += 1;
    try {
      const article = await extractNewsArticle(articleUrl, {
        timeoutMs: 15_000,
        maxResponseBytes: 2_000_000,
        maxContentChars: 500_000,
        minContentChars: 80,
        maxRedirects: 4,
        userAgent: USER_AGENT,
      });
      if (looksLikeMissingArticle(article)) {
        await saveAttempt(articleNumber, articleUrl, "not_found", "soft_not_found", attempt);
        stats.notFound += 1;
        return null;
      }
      return article;
    } catch (error) {
      lastError = error;
      const message = safeError(error);
      if (isRobotsStop(message)) throw error;
      if (isPermanentNotFound(message)) {
        await saveAttempt(articleNumber, articleUrl, "not_found", message, attempt);
        stats.notFound += 1;
        return null;
      }
      if (attempt >= options.retries) break;
      stats.retried += 1;
      const backoffMs = retryDelayMs(message, attempt);
      console.error(JSON.stringify({
        phase: "retry",
        articleNumber,
        attempt,
        backoffMs,
        error: message,
        at: new Date().toISOString(),
      }));
      await delay(backoffMs);
    }
  }
  throw lastError ?? new Error("article_extraction_failed");
}

async function storeArticle(externalId, articleNo, article) {
  const collectedAtIso = new Date().toISOString();
  const publishedAtIso = normalizedPublishedTime(article.publishedTime, collectedAtIso);
  const articleId = `hellot:${externalId}`;
  const scenarioId = classifyScenario(`${article.title}\n${article.textContent}`);
  const extraction = {
    status: "extracted",
    requestedUrl: article.requestedUrl,
    finalUrl: article.finalUrl,
    method: article.method,
    charCount: article.charCount,
    wordCount: article.wordCount,
    truncated: article.truncated,
    robots: article.robots,
  };
  const payload = {
    id: articleId,
    scenarioId: scenarioId === "industry" ? undefined : scenarioId,
    tag: scenarioTag(scenarioId),
    title: article.title,
    source: article.siteName ?? "헬로티",
    sourceUrl: article.finalUrl,
    articleUrl: article.finalUrl,
    sourceType: "언론 보도",
    time: publishedAtIso.slice(0, 10),
    publishedAt: publishedAtIso.slice(0, 10),
    publishedAtIso,
    location: "대한민국",
    sentiment: "중립",
    confidence: 100,
    summary: article.excerpt ?? article.textContent.slice(0, 300),
    paragraphs: article.textContent.split(/\n{2,}/).filter(Boolean).slice(0, 3),
    evidence: [],
    entities: [],
    relations: [],
    affected: [],
  };

  await pool.execute(`
    INSERT INTO news_items (
      storage_key, article_id, scenario_id, source_url, published_at_iso,
      collected_at_iso, provider, article_no, article_url, article_body,
      article_extraction_json, article_extracted_at_iso, payload_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON DUPLICATE KEY UPDATE
      scenario_id = VALUES(scenario_id),
      source_url = VALUES(source_url),
      published_at_iso = VALUES(published_at_iso),
      collected_at_iso = VALUES(collected_at_iso),
      provider = VALUES(provider),
      article_no = COALESCE(article_no, VALUES(article_no)),
      article_url = VALUES(article_url),
      article_body = VALUES(article_body),
      article_extraction_json = VALUES(article_extraction_json),
      article_extracted_at_iso = VALUES(article_extracted_at_iso),
      payload_json = VALUES(payload_json)
  `, [
    STORAGE_KEY,
    articleId,
    scenarioId,
    article.finalUrl,
    publishedAtIso,
    collectedAtIso,
    PROVIDER,
    articleNo,
    article.finalUrl,
    article.textContent,
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

async function loadResumeState() {
  const [bodyRows] = await pool.execute(`
    SELECT source_url, article_url
    FROM news_items
    WHERE article_body IS NOT NULL
      AND CHAR_LENGTH(article_body) > 0
      AND JSON_UNQUOTE(JSON_EXTRACT(article_extraction_json, '$.robots.status')) = 'allowed'
      AND (source_url LIKE 'https://www.hellot.net/news/article.html?no=%'
        OR article_url LIKE 'https://www.hellot.net/news/article.html?no=%')
  `);
  const existingNumbers = new Set(bodyRows.flatMap((row) => {
    const value = articleNumberFromUrl(row.article_url) ?? articleNumberFromUrl(row.source_url);
    return value === null ? [] : [value];
  }));
  const [attemptRows] = await pool.execute(`
    SELECT external_id
    FROM news_crawl_attempts
    WHERE source_name = ? AND status = 'not_found'
      AND external_id BETWEEN ? AND ?
  `, [SOURCE_NAME, options.start, options.end]);
  const notFoundNumbers = new Set(attemptRows.map((row) => Number(row.external_id)));
  const [numberRows] = await pool.execute("SELECT COALESCE(MAX(article_no), 0) AS max_article_no FROM news_items");
  return {
    existingNumbers,
    notFoundNumbers,
    nextArticleNo: Number(numberRows[0]?.max_article_no ?? 0) + 1,
  };
}

async function saveAttempt(externalId, articleUrl, status, lastError, attemptCount) {
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
  `, [SOURCE_NAME, externalId, articleUrl, status, attemptCount, lastError, new Date().toISOString()]);
}

async function renumberArticles() {
  console.log(JSON.stringify({ phase: "renumber_start", at: new Date().toISOString() }));
  await pool.execute(`
    UPDATE news_items AS target
    JOIN (
      SELECT storage_key, article_id,
        ROW_NUMBER() OVER (
          ORDER BY published_at_iso DESC, collected_at_iso DESC, storage_key, article_id
        ) AS generated_article_no
      FROM news_items
    ) AS numbered
      ON numbered.storage_key = target.storage_key
     AND numbered.article_id = target.article_id
    SET target.article_no = numbered.generated_article_no
  `);
  console.log(JSON.stringify({ phase: "renumber_complete", at: new Date().toISOString() }));
}

async function readDatabaseStats() {
  const [rows] = await pool.execute(`
    SELECT
      COUNT(*) AS total_rows,
      SUM(CASE WHEN article_body IS NOT NULL AND CHAR_LENGTH(article_body) > 0 THEN 1 ELSE 0 END) AS body_rows,
      SUM(CASE WHEN JSON_UNQUOTE(JSON_EXTRACT(article_extraction_json, '$.robots.status')) = 'allowed' THEN 1 ELSE 0 END) AS robots_allowed_rows,
      SUM(CASE WHEN storage_key = ? THEN 1 ELSE 0 END) AS hellot_rows,
      SUM(CASE WHEN storage_key = ? THEN OCTET_LENGTH(article_body) ELSE 0 END) AS hellot_body_bytes
    FROM news_items
  `, [STORAGE_KEY, STORAGE_KEY]);
  return rows[0];
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
    connectionLimit: Math.max(2, options.concurrency + 1),
    queueLimit: 0,
    enableKeepAlive: true,
    keepAliveInitialDelay: 0,
    ssl,
  };
}

async function mysqlConnectionOptionsFromEnvironment() {
  const mysqlUrl = process.env.MYSQL_URL?.trim();
  if (mysqlUrl) return mysqlConnectionOptions(mysqlUrl);

  const caPath = process.env.MYSQL_SSL_CA_PATH?.trim();
  const ssl = caPath
    ? { ca: await readFile(caPath, "utf8"), rejectUnauthorized: true, servername: requiredEnvironment("MYSQL_HOST") }
    : undefined;
  return {
    host: requiredEnvironment("MYSQL_HOST"),
    port: Number(process.env.MYSQL_PORT || 3306),
    user: requiredEnvironment("MYSQL_USER"),
    password: requiredEnvironment("MYSQL_PASSWORD"),
    database: requiredEnvironment("MYSQL_DATABASE"),
    charset: "utf8mb4",
    waitForConnections: true,
    connectionLimit: Math.max(4, options.concurrency + 2),
    queueLimit: 0,
    enableKeepAlive: true,
    keepAliveInitialDelay: 0,
    ssl,
  };
}

function readOptions(args) {
  const values = new Map();
  for (let index = 0; index < args.length; index += 1) {
    const token = args[index];
    if (!token.startsWith("--")) throw new Error(`unexpected argument: ${token}`);
    const [rawName, inlineValue] = token.slice(2).split("=", 2);
    const value = inlineValue ?? args[index + 1];
    if (inlineValue === undefined) index += 1;
    values.set(rawName, value);
  }
  const start = integerOption(values.get("start"), DEFAULT_START_NO, 1, 10_000_000);
  const end = integerOption(values.get("end"), DEFAULT_END_NO, start, 10_000_000);
  return {
    start,
    end,
    delayMs: integerOption(values.get("delay-ms") ?? process.env.HELLOT_CRAWL_DELAY_MS, DEFAULT_DELAY_MS, 500, 60_000),
    concurrency: integerOption(values.get("concurrency") ?? process.env.HELLOT_CRAWL_CONCURRENCY, DEFAULT_CONCURRENCY, 1, 4),
    retries: integerOption(values.get("retries") ?? process.env.HELLOT_CRAWL_RETRIES, DEFAULT_RETRIES, 1, 10),
    progressEvery: integerOption(values.get("progress-every"), 25, 1, 10_000),
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

function takeNextNumber() {
  if (cursor > options.end) return null;
  const value = cursor;
  cursor += 1;
  return value;
}

function allocateArticleNo() {
  const value = nextArticleNo;
  nextArticleNo += 1;
  return value;
}

function waitForRateSlot() {
  const next = rateLimiter.then(async () => {
    const remaining = options.delayMs - (Date.now() - lastRequestAt);
    if (remaining > 0) await delay(remaining);
    lastRequestAt = Date.now();
  });
  rateLimiter = next.catch(() => {});
  return next;
}

function retryDelayMs(message, attempt) {
  if (/article_fetch_http_(?:403|429)/.test(message)) return Math.min(15 * 60_000, 60_000 * (2 ** (attempt - 1)));
  return Math.min(60_000, 2_000 * (2 ** (attempt - 1)));
}

function hellotArticleUrl(articleNumber) {
  return `https://www.hellot.net/news/article.html?no=${articleNumber}`;
}

function articleNumberFromUrl(value) {
  try {
    const parsed = new URL(value);
    if (parsed.hostname !== "www.hellot.net" || parsed.pathname !== "/news/article.html") return null;
    const articleNumber = Number(parsed.searchParams.get("no"));
    return Number.isInteger(articleNumber) ? articleNumber : null;
  } catch {
    return null;
  }
}

function looksLikeMissingArticle(article) {
  return /(?:기사를\s*찾을\s*수|요청하신\s*페이지|페이지를\s*찾을\s*수|존재하지\s*않는\s*기사)/i.test(article.title);
}

function isPermanentNotFound(message) {
  return message === "article_not_found" || /article_fetch_http_(?:404|410)/.test(message);
}

function isRobotsStop(message) {
  return message === "article_robots_disallowed" || message === "article_robots_unavailable";
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

function requestStop() {
  stopRequested = true;
  console.log(JSON.stringify({ phase: "stop_requested", at: new Date().toISOString() }));
}

function requiredEnvironment(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function safeError(error) {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/mysql:\/\/[^\s@]+@/gi, "mysql://***@").slice(0, 191);
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
