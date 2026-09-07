import { index, primaryKey, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const newsItems = sqliteTable("news_items", {
  storageKey: text("storage_key").notNull(),
  articleId: text("article_id").notNull(),
  scenarioId: text("scenario_id").notNull(),
  sourceUrl: text("source_url").notNull(),
  publishedAtIso: text("published_at_iso").notNull(),
  collectedAtIso: text("collected_at_iso").notNull(),
  provider: text("provider").notNull(),
  articleUrl: text("article_url"),
  articleBody: text("article_body"),
  articleExtractionJson: text("article_extraction_json"),
  articleExtractedAtIso: text("article_extracted_at_iso"),
  payloadJson: text("payload_json").notNull(),
}, (table) => [
  primaryKey({ columns: [table.storageKey, table.articleId] }),
  index("idx_news_items_storage_published").on(table.storageKey, table.publishedAtIso),
]);
