import { createHash } from "node:crypto";
import { closeMySqlNewsPools, createMySqlNewsStore } from "../db/news-mysql-repository.ts";
import { extractNewsArticle } from "../lib/news-article-extractor.ts";

const articleUrl = process.argv[2] ?? "https://n.news.naver.com/mnews/article/029/0003045160";
const mysqlUrl = process.env.MYSQL_URL ?? "mysql://nexus:nexus-local-password@127.0.0.1:3306/nexus_risk";
const storageKey = "scenario:semiconductor";

try {
  const store = createMySqlNewsStore(mysqlUrl);
  const article = await extractNewsArticle(articleUrl, {
    timeoutMs: 8_000,
    maxResponseBytes: 1_500_000,
    maxContentChars: 30_000,
    minContentChars: 180,
  });
  const now = new Date();
  const articleId = `mysql-verification-${createHash("sha256").update(article.finalUrl).digest("hex").slice(0, 16)}`;
  const baseItem = {
    id: articleId,
    scenarioId: "semiconductor",
    tag: "반도체",
    title: article.title,
    source: article.siteName ?? "NAVER 뉴스",
    sourceUrl: article.finalUrl,
    articleUrl: article.finalUrl,
    sourceType: "NAVER API",
    time: "방금 전",
    publishedAt: now.toISOString(),
    publishedAtIso: now.toISOString(),
    location: "NAVER 뉴스",
    sentiment: "중립",
    confidence: 90,
    summary: article.excerpt ?? `${article.title} 본문 저장 검증 기사`,
    paragraphs: [article.excerpt ?? article.title],
    evidence: [],
    entities: [],
    relations: [],
    affected: [],
  };
  await store.saveHistory(storageKey, [baseItem], "NAVER ARTICLE VERIFICATION", now.toISOString());
  await store.saveBodies(storageKey, [{
    ...baseItem,
    articleBody: article.textContent,
    articleExtraction: {
      status: "extracted",
      requestedUrl: article.requestedUrl,
      finalUrl: article.finalUrl,
      method: article.method,
      charCount: article.charCount,
      wordCount: article.wordCount,
      truncated: article.truncated,
    },
  }], now.toISOString());
  const stored = (await store.loadHistory(storageKey, new Date(), 500)).find((item) => item.id === articleId);
  if (!stored?.articleBody) throw new Error("mysql_article_body_not_persisted");

  console.log(JSON.stringify({
    ok: true,
    database: store.label,
    table: "news_items",
    storageKey,
    articleId,
    title: stored.title,
    articleUrl: stored.articleUrl,
    bodyChars: stored.articleBody.length,
    extractionStatus: stored.articleExtraction?.status,
    extractionMethod: stored.articleExtraction?.method,
    sha256: createHash("sha256").update(stored.articleBody).digest("hex"),
  }, null, 2));
} finally {
  await closeMySqlNewsPools();
}
