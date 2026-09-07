import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { build } from "esbuild";

const root = new URL("../", import.meta.url);
const bundled = await build({
  entryPoints: [fileURLToPath(new URL("db/news-repository.ts", root))],
  bundle: true,
  write: false,
  platform: "node",
  format: "esm",
});
const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].contents).toString("base64")}`;
const { loadNewsHistory, saveNewsBodies, saveNewsHistory } = await import(moduleUrl);

class FakeStatement {
  constructor(database, sql) {
    this.database = database;
    this.sql = sql;
    this.values = [];
  }

  bind(...values) {
    this.values = values;
    return this;
  }

  async all() {
    if (/PRAGMA table_info/.test(this.sql)) {
      return { results: [...this.database.columns].map((name) => ({ name })) };
    }
    if (/SELECT payload_json/.test(this.sql)) {
      const [storageKey, limit] = this.values;
      return {
        results: [...this.database.rows.values()]
          .filter((row) => row.storage_key === storageKey)
          .slice(0, limit)
          .map((row) => ({
            payload_json: row.payload_json,
            article_url: row.article_url,
            article_body: row.article_body,
            article_extraction_json: row.article_extraction_json,
          })),
      };
    }
    return { results: [] };
  }

  async run() {
    if (/ALTER TABLE news_items ADD COLUMN/.test(this.sql)) {
      const column = this.sql.match(/ADD COLUMN ([a-z_]+)/)?.[1];
      if (column) this.database.columns.add(column);
      return { success: true };
    }
    if (/DELETE FROM news_items/.test(this.sql)) return { success: true };
    if (/UPDATE news_items SET/.test(this.sql)) {
      const [articleUrl, failedStatus, articleBody, extractionJson, timestampFailedStatus,
        status, extractedAtIso, payloadJson, storageKey, articleId] = this.values;
      const row = this.database.rows.get(`${storageKey}:${articleId}`);
      if (row) {
        row.article_url = articleUrl;
        row.article_body = failedStatus === "failed" ? null : articleBody ?? row.article_body;
        row.article_extraction_json = extractionJson;
        row.article_extracted_at_iso = timestampFailedStatus === "failed"
          ? null
          : status === "extracted" ? extractedAtIso : row.article_extracted_at_iso;
        row.payload_json = payloadJson;
      }
      return { success: true };
    }
    if (/INSERT INTO news_items/.test(this.sql)) {
      const [storageKey, articleId, scenarioId, sourceUrl, publishedAtIso, collectedAtIso, provider,
        articleUrl, articleBody, extractionJson, extractedAtIso, payloadJson] = this.values;
      const key = `${storageKey}:${articleId}`;
      const previous = this.database.rows.get(key);
      this.database.rows.set(key, {
        storage_key: storageKey,
        article_id: articleId,
        scenario_id: scenarioId,
        source_url: sourceUrl,
        published_at_iso: publishedAtIso,
        collected_at_iso: collectedAtIso,
        provider,
        article_url: articleUrl ?? previous?.article_url ?? null,
        article_body: articleBody ?? previous?.article_body ?? null,
        article_extraction_json: extractionJson ?? previous?.article_extraction_json ?? null,
        article_extracted_at_iso: extractedAtIso ?? previous?.article_extracted_at_iso ?? null,
        payload_json: payloadJson,
      });
      return { success: true };
    }
    return { success: true };
  }
}

class FakeD1Database {
  constructor() {
    this.columns = new Set([
      "storage_key", "article_id", "scenario_id", "source_url", "published_at_iso",
      "collected_at_iso", "provider", "payload_json",
    ]);
    this.rows = new Map();
  }

  prepare(sql) {
    return new FakeStatement(this, sql);
  }

  async batch(statements) {
    return Promise.all(statements.map((statement) => statement.run()));
  }
}

function news(articleBody, articleExtraction) {
  return {
    id: "article-1",
    scenarioId: "semiconductor",
    tag: "반도체",
    title: "본문 저장 테스트",
    source: "테스트 뉴스",
    sourceUrl: "https://news.example.com/article-1",
    articleUrl: "https://news.example.com/article-1",
    sourceType: "NAVER API",
    time: "방금 전",
    publishedAt: "2026. 09. 01. 10:00",
    publishedAtIso: "2026-09-01T01:00:00.000Z",
    location: "NAVER API HUB",
    sentiment: "중립",
    confidence: 90,
    summary: "기사 요약",
    paragraphs: ["기사 요약"],
    articleBody,
    articleExtraction,
    evidence: [],
    entities: [],
    relations: [],
    affected: [],
  };
}

test("migrates old D1 tables and restores a persisted article body", async () => {
  const db = new FakeD1Database();
  const storageKey = "scenario:semiconductor";
  const collectedAtIso = "2026-09-01T01:05:00.000Z";
  const metadataOnly = news(undefined, undefined);

  await saveNewsHistory(db, storageKey, [metadataOnly], "NAVER API HUB", collectedAtIso);
  assert.equal(db.columns.has("article_body"), true);

  const extraction = {
    status: "extracted",
    requestedUrl: metadataOnly.articleUrl,
    finalUrl: metadataOnly.articleUrl,
    method: "readability",
    charCount: 15,
    wordCount: 4,
    truncated: false,
  };
  await saveNewsBodies(db, storageKey, [news("DB에 저장할 뉴스 기사 본문", extraction)], collectedAtIso);

  // 다음 메타데이터 새로고침이 본문 컬럼을 null로 덮지 않는지도 확인한다.
  await saveNewsHistory(db, storageKey, [metadataOnly], "NAVER API HUB", "2026-09-01T01:10:00.000Z");
  const stored = await loadNewsHistory(db, storageKey, new Date("2026-09-01T01:11:00.000Z"));

  assert.equal(stored[0].articleBody, "DB에 저장할 뉴스 기사 본문");
  assert.equal(stored[0].articleExtraction.status, "extracted");
  assert.equal(stored[0].articleExtraction.method, "readability");
});

test("clears a previously stored body after a robots.txt block", async () => {
  const db = new FakeD1Database();
  const storageKey = "scenario:semiconductor";
  const collectedAtIso = "2026-09-01T01:05:00.000Z";
  const extracted = news("삭제되어야 할 뉴스 기사 본문", {
    status: "extracted",
    requestedUrl: "https://news.example.com/article-1",
    method: "readability",
  });
  await saveNewsHistory(db, storageKey, [extracted], "NAVER API HUB", collectedAtIso);

  const blocked = news(undefined, {
    status: "failed",
    requestedUrl: "https://news.example.com/article-1",
    error: "article_robots_disallowed",
  });
  await saveNewsBodies(db, storageKey, [blocked], "2026-09-01T02:00:00.000Z");
  const stored = await loadNewsHistory(db, storageKey, new Date("2026-09-01T02:01:00.000Z"));

  assert.equal(stored[0].articleBody, undefined);
  assert.equal(stored[0].articleExtraction.error, "article_robots_disallowed");
});
