import type { NewsSignal } from "./news-data.ts";
import { extractNewsArticle, type ExtractedNewsArticle } from "./news-article-extractor.ts";

const DEFAULT_BODY_LIMIT = 5;
const MAX_BODY_LIMIT = 100;
const DEFAULT_CONCURRENCY = 4;
const DEFAULT_ROBOTS_RECHECK_MS = 24 * 60 * 60 * 1_000;
const DEFAULT_FAILURE_RETRY_MS = 30 * 60 * 1_000;

export type NewsBodyCrawlResult = {
  items: NewsSignal[];
  bodyResults: NewsSignal[];
  stats: {
    enabled: true;
    attempted: number;
    succeeded: number;
    failed: number;
    robotsDisallowed: number;
    robotsUnavailable: number;
    limit: number;
  };
};

type NewsBodyCrawlerOptions = {
  limit?: number;
  concurrency?: number;
  robotsRecheckMs?: number;
  failureRetryMs?: number;
  now?: Date;
  extract?: (url: string) => Promise<ExtractedNewsArticle>;
};

export function shouldCrawlNewsBodies(includeBody: boolean, persistenceEnabled: boolean): boolean {
  return includeBody || persistenceEnabled;
}

export async function crawlNewsBodies(
  items: NewsSignal[],
  options: NewsBodyCrawlerOptions = {},
): Promise<NewsBodyCrawlResult> {
  const limit = clampInteger(options.limit, DEFAULT_BODY_LIMIT, 1, MAX_BODY_LIMIT);
  const concurrency = clampInteger(options.concurrency, DEFAULT_CONCURRENCY, 1, limit);
  const robotsRecheckMs = positiveNumber(options.robotsRecheckMs, DEFAULT_ROBOTS_RECHECK_MS);
  const failureRetryMs = positiveNumber(options.failureRetryMs, DEFAULT_FAILURE_RETRY_MS);
  const nowMs = (options.now ?? new Date()).getTime();
  const extract = options.extract ?? ((url: string) => extractNewsArticle(url, {
    timeoutMs: 6_000,
    maxResponseBytes: 1_500_000,
    maxContentChars: 80_000,
    minContentChars: 180,
    maxRedirects: 4,
    userAgent: "NexusRiskArticleExtractor/1.0 (+news intelligence; source-linked)",
  }));
  const targets = items
    .map((item, index) => ({ item, index, url: item.articleUrl ?? item.sourceUrl }))
    .filter((target) => Boolean(target.url) && !hasFreshCrawlOutcome(target.item, nowMs, robotsRecheckMs, failureRetryMs))
    .sort((left, right) => Number(Boolean(right.item.articleBody)) - Number(Boolean(left.item.articleBody)))
    .slice(0, limit);
  const output = [...items];
  let cursor = 0;
  let succeeded = 0;

  const workers = Array.from({ length: Math.min(concurrency, targets.length) }, async () => {
    while (cursor < targets.length) {
      const target = targets[cursor];
      cursor += 1;
      try {
        const article = await extract(target.url);
        output[target.index] = withExtractedBody(target.item, article);
        succeeded += 1;
      } catch (error) {
        const extractionError = safeExtractionError(error);
        const failedItem: NewsSignal = {
          ...target.item,
          articleExtraction: {
            status: "failed",
            requestedUrl: target.url,
            error: extractionError,
            checkedAt: new Date(nowMs).toISOString(),
          },
        };
        delete failedItem.articleBody;
        output[target.index] = failedItem;
      }
    }
  });
  await Promise.all(workers);
  const bodyResults = targets.map((target) => output[target.index]);
  const robotsDisallowed = bodyResults.filter((item) => item.articleExtraction?.error === "article_robots_disallowed").length;
  const robotsUnavailable = bodyResults.filter((item) => item.articleExtraction?.error === "article_robots_unavailable").length;

  return {
    items: output,
    bodyResults,
    stats: {
      enabled: true,
      attempted: targets.length,
      succeeded,
      failed: targets.length - succeeded,
      robotsDisallowed,
      robotsUnavailable,
      limit,
    },
  };
}

function withExtractedBody(item: NewsSignal, article: ExtractedNewsArticle): NewsSignal {
  return {
    ...item,
    articleBody: article.textContent,
    articleExtraction: {
      status: "extracted",
      requestedUrl: article.requestedUrl,
      finalUrl: article.finalUrl,
      method: article.method,
      charCount: article.charCount,
      wordCount: article.wordCount,
      truncated: article.truncated,
      robots: article.robots,
    },
  };
}

function safeExtractionError(error: unknown): string {
  const message = error instanceof Error ? error.message : "article_extraction_failed";
  if (/^article_[a-z0-9_:-]+$/i.test(message)) return message.slice(0, 120);
  return "article_extraction_failed";
}

function hasFreshCrawlOutcome(
  item: NewsSignal,
  nowMs: number,
  robotsMaxAgeMs: number,
  failureRetryMs: number,
): boolean {
  if (item.articleBody && item.articleExtraction?.status === "extracted") {
    const checkedAt = Date.parse(item.articleExtraction.robots?.checkedAt ?? "");
    return Number.isFinite(checkedAt) && checkedAt <= nowMs && nowMs - checkedAt < robotsMaxAgeMs;
  }
  if (item.articleExtraction?.status === "failed") {
    const checkedAt = Date.parse(item.articleExtraction.checkedAt ?? "");
    const retryMs = item.articleExtraction.error === "article_robots_disallowed"
      ? robotsMaxAgeMs
      : failureRetryMs;
    return Number.isFinite(checkedAt) && checkedAt <= nowMs && nowMs - checkedAt < retryMs;
  }
  return false;
}

function positiveNumber(value: number | undefined, fallback: number): number {
  return Number.isFinite(value) && (value ?? 0) > 0 ? value! : fallback;
}

function clampInteger(value: number | undefined, fallback: number, min: number, max: number): number {
  if (!Number.isInteger(value)) return fallback;
  return Math.min(max, Math.max(min, value!));
}
