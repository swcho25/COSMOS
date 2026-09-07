import type { Pool, PoolConnection, RowDataPacket } from "mysql2/promise";
import type { NewsSignal } from "../lib/news-data";
import type { NewsStore } from "./news-store";

const CREATE_NEWS_ITEMS_SQL = `
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
    INDEX idx_news_items_storage_published (storage_key, published_at_iso)
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
`;

const pools = new Map<string, Pool>();
const schemaPromises = new WeakMap<object, Promise<void>>();

type StoredNewsRow = RowDataPacket & {
  payload_json: string;
  article_url: string | null;
  article_body: string | null;
  article_extraction_json: string | null;
};

export function createMySqlNewsStore(mysqlUrl: string): NewsStore {
  const normalizedUrl = validateMySqlUrl(mysqlUrl);
  const pool = poolForUrl(normalizedUrl);
  return {
    kind: "mysql",
    label: "MySQL",
    loadHistory: (storageKey, now, limit = 60) => loadMySqlNewsHistory(pool, storageKey, now, limit),
    saveHistory: (storageKey, items, provider, collectedAtIso) => saveMySqlNewsHistory(pool, storageKey, items, provider, collectedAtIso),
    saveBodies: (storageKey, items, extractedAtIso) => saveMySqlNewsBodies(pool, storageKey, items, extractedAtIso),
  };
}

export async function closeMySqlNewsPools(): Promise<void> {
  const active = [...pools.values()];
  pools.clear();
  await Promise.all(active.map((pool) => pool.end()));
}

async function saveMySqlNewsHistory(
  pool: Pool,
  storageKey: string,
  items: NewsSignal[],
  provider: string,
  collectedAtIso: string,
): Promise<void> {
  await ensureMySqlNewsSchema(pool);
  if (!items.length) return;
  await withTransaction(pool, async (connection) => {
    for (const item of items) {
      await connection.execute(`
        INSERT INTO news_items (
          storage_key, article_id, scenario_id, source_url, published_at_iso,
          collected_at_iso, provider, article_url, article_body,
          article_extraction_json, article_extracted_at_iso, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON DUPLICATE KEY UPDATE
          scenario_id = VALUES(scenario_id),
          source_url = VALUES(source_url),
          published_at_iso = VALUES(published_at_iso),
          collected_at_iso = VALUES(collected_at_iso),
          provider = VALUES(provider),
          article_url = COALESCE(VALUES(article_url), article_url),
          article_body = COALESCE(VALUES(article_body), article_body),
          article_extraction_json = COALESCE(VALUES(article_extraction_json), article_extraction_json),
          article_extracted_at_iso = COALESCE(VALUES(article_extracted_at_iso), article_extracted_at_iso),
          payload_json = VALUES(payload_json)
      `, newsValues(item, storageKey, provider, collectedAtIso));
    }
    await connection.execute(`
      DELETE FROM news_items
      WHERE storage_key = ?
        AND article_id NOT IN (
          SELECT article_id FROM (
            SELECT article_id
            FROM news_items
            WHERE storage_key = ?
            ORDER BY published_at_iso DESC, collected_at_iso DESC
            LIMIT 10000
          ) AS recent_news
        )
    `, [storageKey, storageKey]);
  });
}

async function saveMySqlNewsBodies(
  pool: Pool,
  storageKey: string,
  items: NewsSignal[],
  extractedAtIso: string,
): Promise<number> {
  await ensureMySqlNewsSchema(pool);
  const bodyResults = items.filter((item) => Boolean(item.articleExtraction));
  if (!bodyResults.length) return 0;
  await withTransaction(pool, async (connection) => {
    for (const item of bodyResults) {
      await connection.execute(`
        UPDATE news_items SET
          article_url = ?,
          article_body = CASE
            WHEN ? = 'failed' THEN NULL
            ELSE COALESCE(?, article_body)
          END,
          article_extraction_json = ?,
          article_extracted_at_iso = CASE
            WHEN ? = 'failed' THEN NULL
            WHEN ? = 'extracted' THEN ?
            ELSE article_extracted_at_iso
          END,
          payload_json = ?
        WHERE storage_key = ? AND article_id = ?
      `, [
        item.articleUrl ?? item.articleExtraction?.requestedUrl ?? item.sourceUrl,
        item.articleExtraction?.status ?? null,
        item.articleBody ?? null,
        JSON.stringify(item.articleExtraction),
        item.articleExtraction?.status ?? null,
        item.articleExtraction?.status ?? null,
        extractedAtIso,
        JSON.stringify(item),
        storageKey,
        item.id,
      ]);
    }
  });
  return bodyResults.length;
}

async function loadMySqlNewsHistory(
  pool: Pool,
  storageKey: string,
  now: Date,
  limit: number,
): Promise<NewsSignal[]> {
  await ensureMySqlNewsSchema(pool);
  const boundedLimit = Math.min(10_000, Math.max(1, Math.trunc(limit)));
  const [rows] = await pool.execute<StoredNewsRow[]>(`
    SELECT payload_json, article_url, article_body, article_extraction_json
    FROM news_items
    WHERE storage_key = ?
    ORDER BY published_at_iso DESC, collected_at_iso DESC
    LIMIT ${boundedLimit}
  `, [storageKey]);
  return rows.flatMap((row) => hydrateStoredNews(row, now));
}

async function ensureMySqlNewsSchema(pool: Pool): Promise<void> {
  const key = pool as unknown as object;
  const pending = schemaPromises.get(key);
  if (pending) return pending;
  const promise = (async () => {
    await pool.execute(CREATE_NEWS_ITEMS_SQL);
    try {
      await pool.execute("ALTER TABLE news_items ADD COLUMN article_no BIGINT UNSIGNED NULL");
    } catch (error) {
      if (!/duplicate column/i.test(error instanceof Error ? error.message : String(error))) throw error;
    }
    try {
      await pool.execute(`
        ALTER TABLE news_items
        ADD COLUMN news_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        ADD UNIQUE INDEX ux_news_items_news_id (news_id)
      `);
    } catch (error) {
      if (!/duplicate column|duplicate key name/i.test(error instanceof Error ? error.message : String(error))) throw error;
    }
  })();
  schemaPromises.set(key, promise);
  try {
    await promise;
  } catch (error) {
    schemaPromises.delete(key);
    throw error;
  }
}

function hydrateStoredNews(row: StoredNewsRow, now: Date): NewsSignal[] {
  try {
    const item = JSON.parse(row.payload_json) as NewsSignal;
    if (!item?.id || !item.title || !item.sourceUrl) return [];
    return [{
      ...item,
      articleUrl: row.article_url ?? item.articleUrl,
      articleBody: row.article_body ?? item.articleBody,
      articleExtraction: parseArticleExtraction(row.article_extraction_json) ?? item.articleExtraction,
      time: relativeTime(item.publishedAtIso, now),
    }];
  } catch {
    return [];
  }
}

function newsValues(item: NewsSignal, storageKey: string, provider: string, collectedAtIso: string): Array<string | null> {
  return [
    storageKey,
    item.id,
    item.scenarioId ?? "governance",
    item.sourceUrl,
    item.publishedAtIso ?? collectedAtIso,
    collectedAtIso,
    provider,
    item.articleUrl ?? item.sourceUrl,
    item.articleBody ?? null,
    item.articleExtraction ? JSON.stringify(item.articleExtraction) : null,
    item.articleExtraction?.status === "extracted" ? collectedAtIso : null,
    JSON.stringify(item),
  ];
}

async function withTransaction(pool: Pool, operation: (connection: PoolConnection) => Promise<void>): Promise<void> {
  const connection = await pool.getConnection();
  try {
    await connection.beginTransaction();
    await operation(connection);
    await connection.commit();
  } catch (error) {
    await connection.rollback();
    throw error;
  } finally {
    connection.release();
  }
}

function poolForUrl(mysqlUrl: string): Pool {
  const existing = pools.get(mysqlUrl);
  if (existing) return existing;
  const moduleName = "mysql2/promise";
  const poolPromise = import(/* @vite-ignore */ moduleName) as Promise<typeof import("mysql2/promise")>;
  const lazyPool = createLazyPool(poolPromise, mysqlUrl);
  pools.set(mysqlUrl, lazyPool);
  return lazyPool;
}

function createLazyPool(modulePromise: Promise<typeof import("mysql2/promise")>, mysqlUrl: string): Pool {
  let poolPromise: Promise<Pool> | undefined;
  const getPool = () => poolPromise ??= modulePromise.then((mysql) => mysql.createPool(mysqlUrl));
  return new Proxy({} as Pool, {
    get(_target, property) {
      if (property === "then") return undefined;
      return (...args: unknown[]) => getPool().then((pool) => {
        const member = pool[property as keyof Pool];
        if (typeof member !== "function") return member;
        return (member as (...values: unknown[]) => unknown).apply(pool, args);
      });
    },
  });
}

function validateMySqlUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("mysql_invalid_url");
  }
  if (url.protocol !== "mysql:") throw new Error("mysql_invalid_protocol");
  if (!url.hostname || !url.pathname.slice(1)) throw new Error("mysql_incomplete_url");
  return url.toString();
}

function parseArticleExtraction(value: string | null): NewsSignal["articleExtraction"] | undefined {
  if (!value) return undefined;
  try {
    const parsed = JSON.parse(value) as NewsSignal["articleExtraction"];
    if (!parsed || (parsed.status !== "extracted" && parsed.status !== "failed") || !parsed.requestedUrl) return undefined;
    return parsed;
  } catch {
    return undefined;
  }
}

function relativeTime(publishedAtIso: string | undefined, now: Date): string {
  const published = publishedAtIso ? new Date(publishedAtIso) : null;
  if (!published || Number.isNaN(published.getTime())) return "저장된 뉴스";
  const minutes = Math.max(0, Math.floor((now.getTime() - published.getTime()) / 60_000));
  if (minutes < 1) return "방금 전";
  if (minutes < 60) return `${minutes}분 전`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}시간 전`;
  return `${Math.floor(hours / 24)}일 전`;
}
