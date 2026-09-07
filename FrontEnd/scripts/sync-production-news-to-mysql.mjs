import { createConnection } from "mysql2/promise";
import { closeMySqlNewsPools, createMySqlNewsStore } from "../db/news-mysql-repository.ts";
import { crawlNewsBodies } from "../lib/news-body-crawler.ts";

const productionApi = process.env.PRODUCTION_NEWS_API_URL
  ?? "https://d1ak6pqxj9kjoj.cloudfront.net/api/news";
const mysqlUrls = configuredMySqlUrls();
const stores = mysqlUrls.map((url) => ({ url, store: createMySqlNewsStore(url) }));
const watchMode = process.argv.includes("--watch");
const backfillMode = process.argv.includes("--backfill");
const positionalArgs = process.argv.slice(2).filter((value) => !value.startsWith("--"));
const scenario = positionalArgs[0] ?? "all";
const bodyLimit = boundedBodyLimit(positionalArgs[1]);
const intervalSeconds = boundedIntervalSeconds(process.env.NEWS_SYNC_INTERVAL_SECONDS);
const backlogIntervalSeconds = boundedBacklogIntervalSeconds(process.env.NEWS_BACKLOG_INTERVAL_SECONDS);
const storageKey = `production:scenario:${scenario}`;

try {
  if (backfillMode) await synchronizeHistoricalMetadata();
  do {
    let attempted = 0;
    try {
      attempted = (await synchronizeOnce()).attempted;
    } catch (error) {
      if (!watchMode) throw error;
      console.error(JSON.stringify({
        ok: false,
        at: new Date().toISOString(),
        error: error instanceof Error ? error.message : String(error),
        retryAfterSeconds: intervalSeconds,
      }));
    }
    if (watchMode) {
      const nextDelaySeconds = attempted >= bodyLimit ? backlogIntervalSeconds : intervalSeconds;
      await delay(nextDelaySeconds * 1_000);
    }
  } while (watchMode);
} finally {
  await closeMySqlNewsPools();
}

async function synchronizeOnce() {
  const url = new URL(productionApi);
  url.searchParams.set("scenario", scenario);
  url.searchParams.set("includeBody", "0");
  url.searchParams.set("bodyLimit", String(Math.min(bodyLimit, 10)));
  url.searchParams.set("mysqlSync", Date.now().toString());

  const response = await fetch(url, {
    headers: productionApiHeaders(),
    signal: AbortSignal.timeout(180_000),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.message ?? payload?.error ?? `production_news_${response.status}`);
  if (!Array.isArray(payload.items) || !payload.items.length) throw new Error("production_news_empty");

  const collectedAtIso = typeof payload.collectedAt === "string"
    ? payload.collectedAt
    : new Date().toISOString();
  const metadataItems = payload.items.map(withoutRemoteBody);
  await Promise.all(stores.map(({ store }) => store.saveHistory(
      storageKey,
      metadataItems,
      `${payload.provider ?? "CloudFront"} → MySQL robots-safe sync`,
      collectedAtIso,
    )));

  const beforeCrawl = await stores[0].store.loadHistory(storageKey, new Date(), 10_000);
  const crawled = await crawlNewsBodies(beforeCrawl, { limit: bodyLimit });
  const synchronizedAt = new Date().toISOString();
  await Promise.all(stores.slice(1).map(({ store }) => store.saveHistory(
    storageKey,
    crawled.items,
    `${payload.provider ?? "CloudFront"} → MySQL robots-safe mirror`,
    synchronizedAt,
  )));
  await Promise.all(stores.map(({ store }) => store.saveBodies(storageKey, crawled.items, synchronizedAt)));
  const synchronizedTargets = await Promise.all(stores.map(async ({ url, store }) => {
    const stored = await store.loadHistory(storageKey, new Date(), 10_000);
    const storedBodies = approvedBodies(stored);
    await createWorkbenchView(url);
    return {
      target: new URL(url).host,
      schema: new URL(url).pathname.slice(1),
      rowsSynchronized: stored.length,
      bodiesStoredWithRobotsApproval: storedBodies.length,
      bodyBytes: storedBodies.reduce((total, item) => total + Buffer.byteLength(item.articleBody, "utf8"), 0),
    };
  }));
  const storedBodies = approvedBodies(crawled.items);
  const latestBody = storedBodies[0];
  console.log(JSON.stringify({
    ok: true,
    at: new Date().toISOString(),
    source: url.origin,
    sourceDatabase: payload.persistence?.database ?? null,
    targetDatabase: "mysql",
    targets: synchronizedTargets,
    table: "news_items",
    workbenchView: "production_news_bodies",
    storageKey,
    bodyCrawl: crawled.stats,
    nextRunSeconds: watchMode ? (crawled.stats.attempted >= bodyLimit ? backlogIntervalSeconds : intervalSeconds) : null,
    latestBody: latestBody ? {
      articleId: latestBody.id,
      title: latestBody.title,
      articleUrl: latestBody.articleUrl,
      bodyChars: latestBody.articleBody.length,
      extractionMethod: latestBody.articleExtraction?.method,
      robotsCheckedAt: latestBody.articleExtraction?.robots?.checkedAt,
    } : null,
  }, null, 2));
  return crawled.stats;
}

async function synchronizeHistoricalMetadata() {
  const scenarios = ["battery", "semiconductor", "governance"];
  let fetched = 0;
  for (const backfillScenario of scenarios) {
    for (let start = 1; start <= 901; start += 100) {
      const url = new URL(productionApi);
      url.searchParams.set("scenario", backfillScenario);
      url.searchParams.set("backfill", "1");
      url.searchParams.set("start", String(start));
      url.searchParams.set("display", "100");
      url.searchParams.set("includeBody", "0");
      url.searchParams.set("backfillRun", Date.now().toString());
      const response = await fetch(url, {
        headers: productionApiHeaders(),
        signal: AbortSignal.timeout(180_000),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.message ?? payload?.error ?? `production_backfill_${response.status}`);
      if (!Array.isArray(payload.items)) throw new Error("production_backfill_invalid_items");
      if (!payload.items.length) break;

      const collectedAtIso = typeof payload.collectedAt === "string"
        ? payload.collectedAt
        : new Date().toISOString();
      const metadataItems = payload.items.map(withoutRemoteBody);
      await Promise.all(stores.map(({ store }) => store.saveHistory(
        storageKey,
        metadataItems,
        `${payload.provider ?? "NAVER API HUB"} historical backfill`,
        collectedAtIso,
      )));
      fetched += metadataItems.length;
      console.log(JSON.stringify({
        ok: true,
        phase: "historical_backfill",
        scenario: backfillScenario,
        start,
        pageItems: metadataItems.length,
        fetched,
      }));
      if (metadataItems.length < 100) break;
      await delay(500);
    }
  }
}

function approvedBodies(items) {
  return items.filter((item) => item.articleBody
    && item.articleExtraction?.status === "extracted"
    && item.articleExtraction.robots?.status === "allowed");
}

function configuredMySqlUrls() {
  const configured = process.env.MYSQL_URLS
    ? process.env.MYSQL_URLS.split(",")
    : process.env.MYSQL_URL
      ? [process.env.MYSQL_URL]
      : [
          "mysql://nexus:nexus-local-password@127.0.0.1:3307/nexus_risk",
          "mysql://nexus:nexus-local-password@127.0.0.1:3306/nexus_risk",
        ];
  const normalized = configured.map((value) => value.trim()).filter(Boolean);
  if (!normalized.length) throw new Error("mysql_targets_empty");
  return [...new Set(normalized)];
}

function withoutRemoteBody(item) {
  const metadata = { ...item };
  delete metadata.articleBody;
  delete metadata.articleExtraction;
  return metadata;
}

function boundedBodyLimit(value) {
  if (value === undefined) return 100;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 100) {
    throw new Error("bodyLimit must be an integer from 1 to 100");
  }
  return parsed;
}

function boundedIntervalSeconds(value) {
  if (value === undefined || value === "") return 300;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 60 || parsed > 86_400) {
    throw new Error("NEWS_SYNC_INTERVAL_SECONDS must be an integer from 60 to 86400");
  }
  return parsed;
}

function boundedBacklogIntervalSeconds(value) {
  if (value === undefined || value === "") return 30;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 10 || parsed > 300) {
    throw new Error("NEWS_BACKLOG_INTERVAL_SECONDS must be an integer from 10 to 300");
  }
  return parsed;
}

function productionApiHeaders() {
  const headers = { accept: "application/json" };
  const token = process.env.SITES_BYPASS_TOKEN?.trim();
  if (token) headers["OAI-Sites-Authorization"] = `Bearer ${token}`;
  return headers;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function createWorkbenchView(url) {
  const connection = await createConnection(url);
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
        SELECT
          storage_key,
          article_id,
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
      WHERE storage_key LIKE 'production:scenario:%'
        AND article_body IS NOT NULL
        AND CHAR_LENGTH(article_body) > 0
        AND JSON_UNQUOTE(JSON_EXTRACT(article_extraction_json, '$.robots.status')) = 'allowed'
    `);
  } finally {
    await connection.end();
  }
}
