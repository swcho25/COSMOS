import { mergeNewsSignalsNewestFirst, type NewsSignal } from "../lib/news-data";

const CREATE_NEWS_ITEMS_SQL = `
  CREATE TABLE IF NOT EXISTS news_items (
    storage_key TEXT NOT NULL,
    article_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    published_at_iso TEXT NOT NULL,
    collected_at_iso TEXT NOT NULL,
    provider TEXT NOT NULL,
    article_url TEXT,
    article_body TEXT,
    article_extraction_json TEXT,
    article_extracted_at_iso TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (storage_key, article_id)
  )
`;

const CREATE_NEWS_ITEMS_INDEX_SQL = `
  CREATE INDEX IF NOT EXISTS idx_news_items_storage_published
  ON news_items(storage_key, published_at_iso)
`;

type StoredNewsRow = {
  payload_json: string;
  article_url: string | null;
  article_body: string | null;
  article_extraction_json: string | null;
};

const NEWS_BODY_COLUMNS = [
  { name: "article_url", definition: "TEXT" },
  { name: "article_body", definition: "TEXT" },
  { name: "article_extraction_json", definition: "TEXT" },
  { name: "article_extracted_at_iso", definition: "TEXT" },
] as const;

const schemaPromises = new WeakMap<object, Promise<void>>();

type NewsD1PreparedStatement = {
  bind(...values: unknown[]): NewsD1PreparedStatement;
  run<T = unknown>(): Promise<T>;
  all<T = Record<string, unknown>>(): Promise<{ results: T[] }>;
};

export type NewsD1Database = {
  prepare(query: string): NewsD1PreparedStatement;
  batch<T = unknown>(statements: NewsD1PreparedStatement[]): Promise<T[]>;
};

export type NewsPersistenceState = {
  enabled: boolean;
  status: "stored" | "database_unavailable" | "database_error";
  historyCount: number;
  database?: "d1" | "mysql";
};

export function newsStorageKey(requested: string, companyQuery: string): string {
  const normalizedCompany = companyQuery.trim().toLocaleLowerCase("ko-KR").replace(/\s+/g, " ");
  return normalizedCompany ? `company:${normalizedCompany}` : `scenario:${requested}`;
}

export function newsDatabaseFromEnv(runtimeEnv: Record<string, unknown>): NewsD1Database | null {
  const candidate = runtimeEnv.DB;
  if (!candidate || typeof candidate !== "object") return null;
  if (!("prepare" in candidate) || typeof candidate.prepare !== "function") return null;
  return candidate as NewsD1Database;
}

export async function saveNewsHistory(
  db: NewsD1Database,
  storageKey: string,
  items: NewsSignal[],
  provider: string,
  collectedAtIso: string,
): Promise<void> {
  await ensureNewsSchema(db);
  if (!items.length) return;

  const statements = items.map((item) => db.prepare(`
    INSERT INTO news_items (
      storage_key, article_id, scenario_id, source_url, published_at_iso,
      collected_at_iso, provider, article_url, article_body,
      article_extraction_json, article_extracted_at_iso, payload_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(storage_key, article_id) DO UPDATE SET
      scenario_id = excluded.scenario_id,
      source_url = excluded.source_url,
      published_at_iso = excluded.published_at_iso,
      collected_at_iso = excluded.collected_at_iso,
      provider = excluded.provider,
      article_url = COALESCE(excluded.article_url, news_items.article_url),
      article_body = COALESCE(excluded.article_body, news_items.article_body),
      article_extraction_json = COALESCE(excluded.article_extraction_json, news_items.article_extraction_json),
      article_extracted_at_iso = COALESCE(excluded.article_extracted_at_iso, news_items.article_extracted_at_iso),
      payload_json = excluded.payload_json
  `).bind(
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
  ));
  await db.batch(statements);

  await db.prepare(`
    DELETE FROM news_items
    WHERE storage_key = ?
      AND rowid NOT IN (
        SELECT rowid FROM news_items
        WHERE storage_key = ?
        ORDER BY published_at_iso DESC, collected_at_iso DESC
        LIMIT 10000
      )
  `).bind(storageKey, storageKey).run();
}

export async function saveNewsBodies(
  db: NewsD1Database,
  storageKey: string,
  items: NewsSignal[],
  extractedAtIso: string,
): Promise<number> {
  await ensureNewsSchema(db);
  const bodyResults = items.filter((item) => Boolean(item.articleExtraction));
  if (!bodyResults.length) return 0;

  const statements = bodyResults.map((item) => db.prepare(`
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
  `).bind(
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
  ));
  await db.batch(statements);
  return bodyResults.length;
}

export async function loadNewsHistory(
  db: NewsD1Database,
  storageKey: string,
  now: Date,
  limit = 60,
): Promise<NewsSignal[]> {
  await ensureNewsSchema(db);
  const result = await db.prepare(`
    SELECT payload_json, article_url, article_body, article_extraction_json
    FROM news_items
    WHERE storage_key = ?
    ORDER BY published_at_iso DESC, collected_at_iso DESC
    LIMIT ?
  `).bind(storageKey, limit).all<StoredNewsRow>();

  return result.results.flatMap((row) => {
    try {
      const item = JSON.parse(row.payload_json) as NewsSignal;
      if (!item?.id || !item.title || !item.sourceUrl) return [];
      const articleExtraction = parseArticleExtraction(row.article_extraction_json) ?? item.articleExtraction;
      return [{
        ...item,
        articleUrl: row.article_url ?? item.articleUrl,
        articleBody: row.article_body ?? item.articleBody,
        articleExtraction,
        time: relativeTime(item.publishedAtIso, now),
      }];
    } catch {
      return [];
    }
  });
}

export function mergeNewsHistory(current: NewsSignal[], stored: NewsSignal[], limit = 60): NewsSignal[] {
  return mergeNewsSignalsNewestFirst(current, stored, limit);
}

async function ensureNewsSchema(db: NewsD1Database): Promise<void> {
  const key = db as unknown as object;
  const pending = schemaPromises.get(key);
  if (pending) return pending;

  const promise = (async () => {
    await db.batch([
      db.prepare(CREATE_NEWS_ITEMS_SQL),
      db.prepare(CREATE_NEWS_ITEMS_INDEX_SQL),
    ]);
    const tableInfo = await db.prepare("PRAGMA table_info(news_items)").all<{ name: string }>();
    const existingColumns = new Set(tableInfo.results.map((column) => column.name));
    for (const column of NEWS_BODY_COLUMNS) {
      if (existingColumns.has(column.name)) continue;
      try {
        await db.prepare(`ALTER TABLE news_items ADD COLUMN ${column.name} ${column.definition}`).run();
      } catch (error) {
        if (!/duplicate column name/i.test(error instanceof Error ? error.message : String(error))) throw error;
      }
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
