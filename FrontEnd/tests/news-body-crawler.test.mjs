import assert from "node:assert/strict";
import test from "node:test";
import { crawlNewsBodies, shouldCrawlNewsBodies } from "../lib/news-body-crawler.ts";

function news(id, url) {
  return {
    id,
    tag: "반도체",
    title: `기사 ${id}`,
    source: "테스트 뉴스",
    sourceUrl: url,
    articleUrl: url,
    sourceType: "NAVER API",
    time: "방금 전",
    publishedAt: "2026. 09. 01. 10:00",
    location: "NAVER API HUB",
    sentiment: "중립",
    confidence: 88,
    summary: "검색 API가 제공한 요약",
    paragraphs: ["검색 API가 제공한 요약"],
    evidence: [],
    entities: [],
    relations: [],
    affected: [],
  };
}

test("automatically crawls bodies whenever durable news storage is enabled", () => {
  assert.equal(shouldCrawlNewsBodies(false, true), true);
  assert.equal(shouldCrawlNewsBodies(true, false), true);
  assert.equal(shouldCrawlNewsBodies(false, false), false);
});

test("adds transient article bodies with bounded concurrency and per-item failures", async () => {
  const input = [
    news("one", "https://n.news.naver.com/article/001/1"),
    news("two", "https://n.news.naver.com/article/001/2"),
    news("three", "https://n.news.naver.com/article/001/3"),
  ];
  const extract = async (url) => {
    if (url.endsWith("/2")) throw new Error("article_fetch_http_403");
    return {
      requestedUrl: url,
      finalUrl: url,
      title: "추출 기사",
      textContent: `추출된 기사 본문 ${url}`,
      charCount: 100,
      wordCount: 10,
      method: "article-selector",
      truncated: false,
    };
  };

  const result = await crawlNewsBodies(input, { limit: 2, concurrency: 2, extract });

  assert.deepEqual(result.stats, {
    enabled: true,
    attempted: 2,
    succeeded: 1,
    failed: 1,
    robotsDisallowed: 0,
    robotsUnavailable: 0,
    limit: 2,
  });
  assert.match(result.items[0].articleBody, /추출된 기사 본문/);
  assert.equal(result.items[0].articleExtraction.status, "extracted");
  assert.equal(result.items[1].articleExtraction.status, "failed");
  assert.equal(result.items[1].articleExtraction.error, "article_fetch_http_403");
  assert.equal(result.items[2].articleExtraction, undefined);
  assert.equal(input[0].articleBody, undefined);
});

test("reuses an already extracted body instead of crawling it again", async () => {
  const stored = {
    ...news("stored", "https://news.example.com/stored"),
    articleBody: "DB에 저장된 기존 기사 본문",
    articleExtraction: {
      status: "extracted",
      requestedUrl: "https://news.example.com/stored",
      method: "readability",
      charCount: 18,
      wordCount: 5,
      truncated: false,
      robots: {
        status: "allowed",
        checkedAt: "2026-09-01T01:00:00.000Z",
        policyUrl: "https://news.example.com/robots.txt",
        userAgent: "NexusRiskArticleExtractor",
      },
    },
  };
  let calls = 0;
  const result = await crawlNewsBodies([stored], {
    now: new Date("2026-09-01T02:00:00.000Z"),
    extract: async () => {
      calls += 1;
      throw new Error("should_not_run");
    },
  });

  assert.equal(calls, 0);
  assert.deepEqual(result.stats, {
    enabled: true,
    attempted: 0,
    succeeded: 0,
    failed: 0,
    robotsDisallowed: 0,
    robotsUnavailable: 0,
    limit: 5,
  });
  assert.deepEqual(result.bodyResults, []);
  assert.equal(result.items[0].articleBody, "DB에 저장된 기존 기사 본문");
});

test("removes a stored body when a robots.txt recheck disallows it", async () => {
  const stored = {
    ...news("blocked-later", "https://news.example.com/blocked-later"),
    articleBody: "이전에 저장된 기사 본문",
    articleExtraction: {
      status: "extracted",
      requestedUrl: "https://news.example.com/blocked-later",
      method: "readability",
      robots: {
        status: "allowed",
        checkedAt: "2026-08-30T01:00:00.000Z",
        policyUrl: "https://news.example.com/robots.txt",
        userAgent: "NexusRiskArticleExtractor",
      },
    },
  };

  const result = await crawlNewsBodies([stored], {
    now: new Date("2026-09-01T02:00:00.000Z"),
    extract: async () => {
      throw new Error("article_robots_disallowed");
    },
  });

  assert.equal(result.items[0].articleBody, undefined);
  assert.equal(result.items[0].articleExtraction.error, "article_robots_disallowed");
  assert.equal(result.stats.robotsDisallowed, 1);
});
