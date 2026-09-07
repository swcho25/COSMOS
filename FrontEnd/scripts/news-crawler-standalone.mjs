import { createConnection } from "mysql2/promise";
import { closeMySqlNewsPools, createMySqlNewsStore } from "../db/news-mysql-repository.ts";
import { crawlNewsBodies } from "../lib/news-body-crawler.ts";
import {
  classifyNewsScenario,
  liveNewsProfiles,
  naverNewsApiUrl,
  parseNaverNewsApi,
  toNewsSignal,
} from "../lib/news-crawler.ts";

loadLocalEnvironment();

const clientId = requiredEnvironment("NAVER_API_HUB_CLIENT_ID");
const clientSecret = requiredEnvironment("NAVER_API_HUB_CLIENT_SECRET");
const mysqlUrls = configuredMySqlUrls();
const stores = mysqlUrls.map((url) => ({ url, store: createMySqlNewsStore(url) }));
const scenarios = Object.keys(liveNewsProfiles);
const storageKey = "production:scenario:all";
const watchMode = process.argv.includes("--watch");
const backfillMode = process.argv.includes("--backfill");
const bodyLimit = boundedInteger(process.env.NEWS_BODY_BATCH_SIZE, 100, 1, 100);
const refreshSeconds = boundedInteger(process.env.NEWS_CRAWLER_INTERVAL_SECONDS, 300, 60, 86_400);
const backlogSeconds = boundedInteger(process.env.NEWS_BACKLOG_INTERVAL_SECONDS, 30, 10, 300);

try {
  if (backfillMode) await collectHistoricalMetadata();
  do {
    await collectCurrentMetadata();
    let result;
    do {
      result = await crawlAndStoreOneBatch();
      if (result.stats.attempted >= bodyLimit) await delay(backlogSeconds * 1_000);
    } while (backfillMode && result.stats.attempted >= bodyLimit);

    if (watchMode) await delay(refreshSeconds * 1_000);
  } while (watchMode);
} finally {
  await closeMySqlNewsPools();
}

async function collectHistoricalMetadata() {
  let fetched = 0;
  for (const scenario of scenarios) {
    for (let start = 1; start <= 901; start += 100) {
      const items = await fetchNaverPage(scenario, start, 100);
      if (!items.length) break;
      const collectedAtIso = new Date().toISOString();
      await Promise.all(stores.map(({ store }) => store.saveHistory(
        storageKey,
        items,
        "NAVER API HUB standalone historical backfill",
        collectedAtIso,
      )));
      fetched += items.length;
      console.log(JSON.stringify({
        phase: "historical_backfill",
        scenario,
        start,
        pageItems: items.length,
        fetched,
      }));
      if (items.length < 100) break;
      await delay(500);
    }
  }
}

async function collectCurrentMetadata() {
  const pages = await Promise.all(scenarios.map((scenario) => fetchNaverPage(scenario, 1, 100)));
  const items = uniqueNewsItems(pages.flat());
  const collectedAtIso = new Date().toISOString();
  await Promise.all(stores.map(({ store }) => store.saveHistory(
    storageKey,
    items,
    "NAVER API HUB standalone current sync",
    collectedAtIso,
  )));
  console.log(JSON.stringify({ phase: "current_metadata", items: items.length, collectedAtIso }));
}

async function fetchNaverPage(scenario, start, display) {
  const response = await fetch(naverNewsApiUrl(scenario, { start, display }), {
    headers: {
      accept: "application/json",
      "X-NCP-APIGW-API-KEY-ID": clientId,
      "X-NCP-APIGW-API-KEY": clientSecret,
    },
    signal: AbortSignal.timeout(60_000),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.errorCode ?? payload?.message ?? `naver_api_${response.status}`);
  const collectedAt = new Date();
  return uniqueNewsItems(parseNaverNewsApi(payload).map((item) => {
    const resolvedScenario = classifyNewsScenario(item) ?? scenario;
    return toNewsSignal(item, resolvedScenario, collectedAt);
  }));
}

async function crawlAndStoreOneBatch() {
  const items = await stores[0].store.loadHistory(storageKey, new Date(), 10_000);
  const crawled = await crawlNewsBodies(items, { limit: bodyLimit });
  const extractedAtIso = new Date().toISOString();
  await Promise.all(stores.map(({ store }) => store.saveBodies(storageKey, crawled.bodyResults, extractedAtIso)));
  const targets = await Promise.all(stores.map(async ({ url, store }) => {
    await refreshArticleNumbersAndView(url);
    const stored = await store.loadHistory(storageKey, new Date(), 10_000);
    const approved = stored.filter((item) => item.articleBody
      && item.articleExtraction?.status === "extracted"
      && item.articleExtraction.robots?.status === "allowed");
    return {
      database: new URL(url).host,
      rows: stored.length,
      approvedBodies: approved.length,
      bodyBytes: approved.reduce((total, item) => total + Buffer.byteLength(item.articleBody, "utf8"), 0),
    };
  }));
  console.log(JSON.stringify({
    phase: "body_crawl",
    at: extractedAtIso,
    stats: crawled.stats,
    targets,
    nextDelaySeconds: crawled.stats.attempted >= bodyLimit ? backlogSeconds : refreshSeconds,
  }, null, 2));
  return { stats: crawled.stats, targets };
}

async function refreshArticleNumbersAndView(mysqlUrl) {
  const connection = await createConnection(mysqlUrl);
  try {
    try {
      await connection.execute("ALTER TABLE news_items ADD COLUMN article_no BIGINT UNSIGNED NULL");
    } catch (error) {
      if (!/duplicate column/i.test(error instanceof Error ? error.message : String(error))) throw error;
    }
    try {
      await connection.execute(`
        ALTER TABLE news_items
        ADD COLUMN news_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        ADD UNIQUE INDEX ux_news_items_news_id (news_id)
      `);
    } catch (error) {
      if (!/duplicate column|duplicate key name/i.test(error instanceof Error ? error.message : String(error))) throw error;
    }
    await connection.execute(`
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
    await connection.execute(`
      CREATE OR REPLACE VIEW production_news_bodies AS
      SELECT
        news_id,
        ROW_NUMBER() OVER (ORDER BY published_at_iso DESC, article_id) AS article_no,
        storage_key,
        article_id,
        scenario_id,
        JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.title')) AS title,
        provider,
        published_at_iso,
        article_url,
        CHAR_LENGTH(article_body) AS body_chars,
        JSON_UNQUOTE(JSON_EXTRACT(article_extraction_json, '$.method')) AS extraction_method,
        JSON_UNQUOTE(JSON_EXTRACT(article_extraction_json, '$.robots.checkedAt')) AS robots_checked_at,
        article_extracted_at_iso,
        article_body
      FROM news_items
      WHERE article_body IS NOT NULL
        AND CHAR_LENGTH(article_body) > 0
        AND JSON_UNQUOTE(JSON_EXTRACT(article_extraction_json, '$.robots.status')) = 'allowed'
    `);
  } finally {
    await connection.end();
  }
}

function configuredMySqlUrls() {
  const configured = process.env.MYSQL_URLS
    ? process.env.MYSQL_URLS.split(",")
    : [process.env.MYSQL_URL ?? "mysql://nexus:nexus-local-password@127.0.0.1:3306/nexus_risk"];
  const normalized = configured.map((value) => value.trim()).filter(Boolean);
  if (!normalized.length) throw new Error("mysql_targets_empty");
  return [...new Set(normalized)];
}

function uniqueNewsItems(items) {
  const seen = new Set();
  return items.filter((item) => {
    const key = item.sourceUrl.trim().replace(/\/$/, "");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
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
  if (!value) throw new Error(`${name} is required in .env.local or the process environment`);
  return value;
}

function boundedInteger(value, fallback, min, max) {
  if (value === undefined || value === "") return fallback;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`integer setting must be between ${min} and ${max}`);
  }
  return parsed;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
