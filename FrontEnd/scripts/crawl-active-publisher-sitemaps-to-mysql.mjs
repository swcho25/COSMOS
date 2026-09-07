import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createPool } from "mysql2/promise";
import { extractNewsArticle } from "../lib/news-article-extractor.ts";

const USER_AGENT = "NexusRiskArticleExtractor/1.0 (+news intelligence; source-linked)";
const STORAGE_KEY = "production:active-publisher-sitemaps";
const PROVIDER = "Active publisher sitemap crawler";
const FIXED_CRAWLER_DOMAINS = new Set([
  "hellot.net",
  "kpenews.com",
  "mdtoday.co.kr",
  "sedaily.com",
  "newspim.com",
  "newstomato.com",
]);
const COMMON_ARTICLE_QUERY_KEYS = new Set([
  "no",
  "idxno",
  "article_no",
  "articleno",
  "newsno",
  "seq",
  "aid",
  "id",
]);
const TRACKING_PARAMETERS = new Set([
  "ref",
  "inflow",
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_content",
  "utm_term",
]);

loadLocalEnvironment();

const options = readOptions(process.argv.slice(2));
const pool = createPool(await mysqlConnectionOptionsFromEnvironment());
const hostQueues = new Map();
const hostLastRequestAt = new Map();
let stopRequested = false;

process.on("SIGINT", requestStop);
process.on("SIGTERM", requestStop);

try {
  await ensureSchema();
  do {
    await runCycle();
    if (!options.watch || stopRequested) break;
    console.log(JSON.stringify({
      phase: "watch_wait",
      minutes: options.intervalMinutes,
      nextAt: new Date(Date.now() + options.intervalMinutes * 60_000).toISOString(),
    }));
    await interruptibleDelay(options.intervalMinutes * 60_000);
  } while (!stopRequested);
} catch (error) {
  console.error(JSON.stringify({ phase: "fatal", error: safeError(error), at: new Date().toISOString() }));
  process.exitCode = 1;
} finally {
  await pool.end();
}

async function runCycle() {
  const startedAt = new Date().toISOString();
  const sources = await loadActiveSources();
  const existingUrls = await loadExistingUrls();
  const recentFailures = await loadRecentFailureHashes();
  const discoveries = await runWithConcurrency(sources, options.discoveryConcurrency, discoverSource);
  const candidates = interleaveCandidates(discoveries)
    .filter((candidate) => !existingUrls.has(candidate.normalizedUrl))
    .filter((candidate) => !recentFailures.has(candidate.urlHash));
  const stats = {
    activeSources: sources.length,
    sourcesWithCandidates: discoveries.filter((item) => item.candidates.length > 0).length,
    discoveredCandidates: discoveries.reduce((total, item) => total + item.candidates.length, 0),
    targets: candidates.length,
    attempted: 0,
    stored: 0,
    existing: 0,
    notFound: 0,
    robotsDisallowed: 0,
    robotsUnavailable: 0,
    failed: 0,
    retried: 0,
  };
  let cursor = 0;

  console.log(JSON.stringify({ phase: "cycle_start", stats, startedAt }));
  const workers = Array.from({ length: Math.min(options.concurrency, candidates.length) }, async () => {
    while (!stopRequested) {
      const candidate = candidates[cursor];
      cursor += 1;
      if (!candidate) return;
      if (existingUrls.has(candidate.normalizedUrl)) {
        stats.existing += 1;
        continue;
      }
      stats.attempted += 1;
      try {
        const article = await extractWithRetries(candidate, stats);
        if (!article) continue;
        const articleBody = cleanArticleBody(candidate.domain, article.textContent);
        if (articleBody.length < 80) throw new Error("article_body_not_found_after_cleanup");
        await storeArticle(candidate, article, articleBody);
        await saveUrlAttempt(candidate, "stored", null, 1);
        await recordPublisherSuccess(candidate, article.siteName, article.robots.checkedAt);
        existingUrls.add(candidate.normalizedUrl);
        stats.stored += 1;
      } catch (error) {
        const message = safeError(error);
        if (message === "article_robots_disallowed") stats.robotsDisallowed += 1;
        else if (message === "article_robots_unavailable") stats.robotsUnavailable += 1;
        else stats.failed += 1;
        await saveUrlAttempt(candidate, failureStatus(message), message, options.retries);
        await recordPublisherFailure(candidate, message);
        console.error(JSON.stringify({
          phase: "article_failed",
          domain: candidate.domain,
          url: candidate.normalizedUrl,
          error: message,
          at: new Date().toISOString(),
        }));
      }

      const completed = stats.stored + stats.existing + stats.notFound
        + stats.robotsDisallowed + stats.robotsUnavailable + stats.failed;
      if (completed > 0 && completed % options.progressEvery === 0) {
        console.log(JSON.stringify({ phase: "progress", stats, at: new Date().toISOString() }));
      }
    }
  });
  await Promise.all(workers);

  console.log(JSON.stringify({
    phase: stopRequested ? "cycle_stopped" : "cycle_complete",
    stats,
    database: await databaseStats(),
    sitemapStates: await sitemapStateStats(),
    startedAt,
    finishedAt: new Date().toISOString(),
  }));
}

async function loadActiveSources() {
  const [rows] = await pool.execute(`
    SELECT publisher_domain, display_name, sample_url
    FROM news_publisher_sources
    WHERE status = 'active' AND success_count > 0
    ORDER BY success_count DESC, publisher_domain
  `);
  return rows.flatMap((row) => {
    const domain = normalizeDomain(row.publisher_domain);
    if (!domain || FIXED_CRAWLER_DOMAINS.has(domain)) return [];
    if (options.domains && !options.domains.has(domain)) return [];
    try {
      const sampleUrl = normalizeArticleUrl(row.sample_url);
      return [{ domain, displayName: row.display_name || domain, sampleUrl, origin: new URL(sampleUrl).origin }];
    } catch {
      return [];
    }
  });
}

async function discoverSource(source) {
  const checkedAt = new Date().toISOString();
  const errors = [];
  const candidates = new Map();
  let sitemapDocuments = 0;
  try {
    const robotsUrl = new URL("/robots.txt", source.origin).toString();
    const robotsText = await fetchText(robotsUrl, source.domain, 1_000_000);
    const declared = robotsSitemaps(robotsText)
      .filter((value) => samePublisherDomain(value, source.domain));
    const roots = selectSitemapUrls(
      declared.length ? declared : conventionalSitemapUrls(source.origin),
      options.maxSitemapRoots,
    );
    const visited = new Set();
    const queue = roots.map((url) => ({ url, depth: 0 }));

    while (queue.length && sitemapDocuments < options.maxSitemapDocuments && !stopRequested) {
      const next = queue.shift();
      if (!next || visited.has(next.url)) continue;
      visited.add(next.url);
      try {
        const xml = await fetchText(next.url, source.domain, 5_000_000);
        sitemapDocuments += 1;
        const parsed = sitemapRecords(xml);
        if (parsed.isIndex && next.depth < options.maxSitemapDepth) {
          const children = selectSitemapUrls(
            parsed.records.map((record) => record.loc)
              .filter((value) => samePublisherDomain(value, source.domain)),
            options.maxSitemapChildren,
          );
          queue.push(...children.map((url) => ({ url, depth: next.depth + 1 })));
          continue;
        }
        for (const record of parsed.records) addCandidate(source, record, candidates);
      } catch (error) {
        errors.push({ url: next.url, error: safeError(error) });
      }
    }
  } catch (error) {
    errors.push({ url: source.origin, error: safeError(error) });
  }

  const output = [...candidates.values()]
    .sort((left, right) => Date.parse(right.lastModifiedIso) - Date.parse(left.lastModifiedIso)
      || right.numericRank - left.numericRank)
    .slice(0, options.limitPerDomain);
  await saveSitemapState(source, output.length ? "active" : "no_candidates", sitemapDocuments, output.length, errors);
  console.log(JSON.stringify({
    phase: "discovery",
    domain: source.domain,
    sitemapDocuments,
    candidates: output.length,
    errors: errors.length,
    at: checkedAt,
  }));
  return { source, candidates: output };
}

function addCandidate(source, record, candidates) {
  try {
    const normalizedUrl = normalizeArticleUrl(record.loc);
    const parsed = new URL(normalizedUrl);
    if (!samePublisherDomain(normalizedUrl, source.domain)) return;
    if (!isLikelyArticleUrl(parsed, source.sampleUrl)) return;
    const urlHash = createHash("sha256").update(normalizedUrl).digest("hex");
    candidates.set(normalizedUrl, {
      domain: source.domain,
      displayName: source.displayName,
      normalizedUrl,
      urlHash,
      lastModifiedIso: normalizedDate(record.lastmod),
      numericRank: numericUrlRank(normalizedUrl),
    });
  } catch {
    // Ignore malformed sitemap locations.
  }
}

async function fetchText(inputUrl, domain, maxBytes) {
  await waitForHost(inputUrl);
  const response = await fetch(inputUrl, {
    headers: {
      accept: "application/xml,text/xml,text/plain,text/html;q=0.8,*/*;q=0.1",
      "accept-language": "ko-KR,ko;q=0.9,en;q=0.5",
      "user-agent": USER_AGENT,
    },
    redirect: "follow",
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error(`discovery_fetch_http_${response.status}`);
  const finalDomain = normalizeDomain(new URL(response.url).hostname);
  if (finalDomain !== domain && !finalDomain.endsWith(`.${domain}`) && !domain.endsWith(`.${finalDomain}`)) {
    throw new Error("discovery_cross_domain_redirect");
  }
  const contentLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(contentLength) && contentLength > maxBytes) throw new Error("discovery_response_too_large");
  const text = await response.text();
  if (Buffer.byteLength(text, "utf8") > maxBytes) throw new Error("discovery_response_too_large");
  return text;
}

async function extractWithRetries(candidate, stats) {
  let lastError;
  for (let attempt = 1; attempt <= options.retries; attempt += 1) {
    await waitForHost(candidate.normalizedUrl);
    try {
      return await extractNewsArticle(candidate.normalizedUrl, {
        timeoutMs: 20_000,
        maxResponseBytes: 2_000_000,
        maxContentChars: 500_000,
        minContentChars: 80,
        maxRedirects: 4,
        userAgent: USER_AGENT,
      });
    } catch (error) {
      lastError = error;
      const message = safeError(error);
      if (isRobotsStop(message)) throw error;
      if (isPermanentNotFound(message)) {
        stats.notFound += 1;
        await saveUrlAttempt(candidate, "not_found", message, attempt);
        return null;
      }
      if (attempt >= options.retries) break;
      stats.retried += 1;
      const backoffMs = retryDelayMs(message, attempt);
      console.error(JSON.stringify({
        phase: "retry",
        domain: candidate.domain,
        url: candidate.normalizedUrl,
        attempt,
        backoffMs,
        error: message,
        at: new Date().toISOString(),
      }));
      await interruptibleDelay(backoffMs);
    }
  }
  throw lastError ?? new Error("article_extraction_failed");
}

async function storeArticle(candidate, article, articleBody) {
  const collectedAtIso = new Date().toISOString();
  const publishedAtIso = normalizedPublishedTime(article.publishedTime, candidate.lastModifiedIso);
  const scenarioId = classifyScenario(`${article.title}\n${articleBody}`);
  const articleId = `sitemap:${candidate.urlHash.slice(0, 32)}`;
  const canonicalUrl = normalizeArticleUrl(article.finalUrl);
  const extraction = {
    status: "extracted",
    requestedUrl: article.requestedUrl,
    finalUrl: article.finalUrl,
    method: article.method,
    charCount: articleBody.length,
    wordCount: countWords(articleBody),
    truncated: article.truncated,
    robots: article.robots,
  };
  const payload = {
    id: articleId,
    scenarioId: scenarioId === "industry" ? undefined : scenarioId,
    tag: scenarioTag(scenarioId),
    title: cleanTitle(article.title),
    source: article.siteName ?? candidate.displayName,
    sourceUrl: canonicalUrl,
    articleUrl: canonicalUrl,
    sourceType: "언론 보도",
    time: publishedAtIso.slice(0, 10),
    publishedAt: publishedAtIso.slice(0, 10),
    publishedAtIso,
    location: "대한민국",
    sentiment: "중립",
    confidence: 100,
    summary: cleanSummary(article.excerpt, articleBody),
    paragraphs: articleBody.split(/\n{2,}/).filter(Boolean).slice(0, 3),
    evidence: [],
    entities: [],
    relations: [],
    affected: [],
    discoveredVia: "active_publisher_sitemap",
  };

  await pool.execute(`
    INSERT INTO news_items (
      storage_key, article_id, scenario_id, source_url, published_at_iso,
      collected_at_iso, provider, article_no, article_url, article_body,
      article_extraction_json, article_extracted_at_iso, payload_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
    ON DUPLICATE KEY UPDATE
      scenario_id = VALUES(scenario_id),
      source_url = VALUES(source_url),
      published_at_iso = VALUES(published_at_iso),
      collected_at_iso = VALUES(collected_at_iso),
      provider = VALUES(provider),
      article_url = VALUES(article_url),
      article_body = VALUES(article_body),
      article_extraction_json = VALUES(article_extraction_json),
      article_extracted_at_iso = VALUES(article_extracted_at_iso),
      payload_json = VALUES(payload_json)
  `, [
    STORAGE_KEY,
    articleId,
    scenarioId,
    canonicalUrl,
    publishedAtIso,
    collectedAtIso,
    PROVIDER,
    canonicalUrl,
    articleBody,
    JSON.stringify(extraction),
    collectedAtIso,
    JSON.stringify(payload),
  ]);
}

async function ensureSchema() {
  await pool.execute(`
    CREATE TABLE IF NOT EXISTS news_publisher_sitemap_state (
      publisher_domain VARCHAR(191) NOT NULL,
      status VARCHAR(32) NOT NULL,
      sitemap_documents INT UNSIGNED NOT NULL DEFAULT 0,
      candidate_count INT UNSIGNED NOT NULL DEFAULT 0,
      last_error VARCHAR(191),
      checked_at_iso VARCHAR(40) NOT NULL,
      PRIMARY KEY (publisher_domain),
      INDEX idx_publisher_sitemap_state_status (status, publisher_domain)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  `);
  await pool.execute(`
    CREATE TABLE IF NOT EXISTS news_url_crawl_attempts (
      url_hash CHAR(64) NOT NULL,
      publisher_domain VARCHAR(191) NOT NULL,
      article_url TEXT NOT NULL,
      status VARCHAR(32) NOT NULL,
      attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
      last_error VARCHAR(191),
      updated_at_iso VARCHAR(40) NOT NULL,
      PRIMARY KEY (url_hash),
      INDEX idx_news_url_attempts_status (status, updated_at_iso),
      INDEX idx_news_url_attempts_domain (publisher_domain, updated_at_iso)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  `);
}

async function saveSitemapState(source, status, sitemapDocuments, candidateCount, errors) {
  const lastError = errors.at(-1)?.error ?? null;
  await pool.execute(`
    INSERT INTO news_publisher_sitemap_state (
      publisher_domain, status, sitemap_documents, candidate_count, last_error, checked_at_iso
    ) VALUES (?, ?, ?, ?, ?, ?)
    ON DUPLICATE KEY UPDATE
      status = VALUES(status),
      sitemap_documents = VALUES(sitemap_documents),
      candidate_count = VALUES(candidate_count),
      last_error = VALUES(last_error),
      checked_at_iso = VALUES(checked_at_iso)
  `, [source.domain, status, sitemapDocuments, candidateCount, lastError, new Date().toISOString()]);
}

async function recordPublisherSuccess(candidate, siteName, checkedAt) {
  const now = new Date().toISOString();
  await pool.execute(`
    UPDATE news_publisher_sources SET
      display_name = ?,
      status = 'active',
      robots_allowed = 1,
      attempt_count = attempt_count + 1,
      success_count = success_count + 1,
      last_error = NULL,
      last_checked_at_iso = ?,
      last_success_at_iso = ?
    WHERE publisher_domain = ?
  `, [(siteName || candidate.displayName).slice(0, 191), checkedAt || now, now, candidate.domain]);
}

async function recordPublisherFailure(candidate, message) {
  const now = new Date().toISOString();
  await pool.execute(`
    UPDATE news_publisher_sources SET
      attempt_count = attempt_count + 1,
      failure_count = failure_count + 1,
      last_error = ?,
      last_checked_at_iso = ?
    WHERE publisher_domain = ?
  `, [message, now, candidate.domain]);
}

async function saveUrlAttempt(candidate, status, lastError, attemptCount) {
  await pool.execute(`
    INSERT INTO news_url_crawl_attempts (
      url_hash, publisher_domain, article_url, status, attempt_count, last_error, updated_at_iso
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON DUPLICATE KEY UPDATE
      publisher_domain = VALUES(publisher_domain),
      article_url = VALUES(article_url),
      status = VALUES(status),
      attempt_count = attempt_count + VALUES(attempt_count),
      last_error = VALUES(last_error),
      updated_at_iso = VALUES(updated_at_iso)
  `, [
    candidate.urlHash,
    candidate.domain,
    candidate.normalizedUrl,
    status,
    attemptCount,
    lastError,
    new Date().toISOString(),
  ]);
}

async function loadExistingUrls() {
  const [rows] = await pool.execute(`
    SELECT source_url, article_url
    FROM news_items
    WHERE article_body IS NOT NULL AND CHAR_LENGTH(article_body) > 0
      AND JSON_UNQUOTE(JSON_EXTRACT(article_extraction_json, '$.robots.status')) = 'allowed'
  `);
  return new Set(rows.flatMap((row) => [row.article_url, row.source_url].flatMap((value) => {
    try {
      return value ? [normalizeArticleUrl(value)] : [];
    } catch {
      return [];
    }
  })));
}

async function loadRecentFailureHashes() {
  const cutoff = new Date(Date.now() - options.failureRetryHours * 60 * 60_000).toISOString();
  const [rows] = await pool.execute(`
    SELECT url_hash
    FROM news_url_crawl_attempts
    WHERE status <> 'stored' AND updated_at_iso >= ?
  `, [cutoff]);
  return new Set(rows.map((row) => row.url_hash));
}

async function databaseStats() {
  const [rows] = await pool.execute(`
    SELECT COUNT(*) AS rows_count,
      SUM(OCTET_LENGTH(article_body)) AS body_bytes,
      COUNT(DISTINCT LOWER(SUBSTRING_INDEX(SUBSTRING_INDEX(source_url, '://', -1), '/', 1))) AS domains
    FROM news_items
    WHERE storage_key = ?
  `, [STORAGE_KEY]);
  return rows[0];
}

async function sitemapStateStats() {
  const [rows] = await pool.execute(`
    SELECT status, COUNT(*) AS publishers, SUM(candidate_count) AS candidates
    FROM news_publisher_sitemap_state
    GROUP BY status
    ORDER BY status
  `);
  return rows;
}

function robotsSitemaps(value) {
  return [...value.matchAll(/^\s*Sitemap\s*:\s*(\S+)\s*$/gim)]
    .map((match) => decodeXml(match[1].trim()));
}

function conventionalSitemapUrls(origin) {
  return [
    new URL("/sitemap.xml", origin).toString(),
    new URL("/sitemap_news.xml", origin).toString(),
    new URL("/sitemap-news.xml", origin).toString(),
  ];
}

function selectSitemapUrls(values, limit) {
  return [...new Set(values)]
    .filter((value) => /^https?:\/\//i.test(value))
    .sort((left, right) => sitemapScore(right) - sitemapScore(left))
    .slice(0, limit);
}

function sitemapScore(value) {
  const text = value.toLowerCase();
  let score = 0;
  if (/latest|recent|google.*news|news.*google|news.?sitemap/.test(text)) score += 100;
  if (/daily|today|article/.test(text)) score += 50;
  if (/2026|2025/.test(text)) score += 20;
  if (/image|video|section|category|tag|author/.test(text)) score -= 100;
  return score;
}

function sitemapRecords(xml) {
  const isIndex = /<sitemapindex\b/i.test(xml);
  const tag = isIndex ? "sitemap" : "url";
  const blocks = [...xml.matchAll(new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)<\\/${tag}>`, "gi"))];
  const records = blocks.flatMap((match) => {
    const loc = elementText(match[1], "loc");
    return loc ? [{ loc, lastmod: elementText(match[1], "lastmod") }] : [];
  });
  if (records.length) return { isIndex, records };
  return {
    isIndex,
    records: [...xml.matchAll(/<loc>\s*([\s\S]*?)\s*<\/loc>/gi)]
      .map((match) => ({ loc: cleanXmlValue(match[1]), lastmod: "" })),
  };
}

function elementText(block, tag) {
  const match = block.match(new RegExp(`<${tag}\\b[^>]*>\\s*([\\s\\S]*?)\\s*<\\/${tag}>`, "i"));
  return match ? cleanXmlValue(match[1]) : "";
}

function cleanXmlValue(value) {
  return decodeXml(value.replace(/^\s*<!\[CDATA\[/, "").replace(/\]\]>\s*$/, "").trim());
}

function decodeXml(value) {
  return value
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");
}

function isLikelyArticleUrl(url, sampleUrl) {
  const text = `${url.pathname}${url.search}`.toLowerCase();
  if (/\/(?:sitemap|search|category|section|tag|author|login|member|admin)(?:\/|$)/.test(text)) return false;
  for (const key of url.searchParams.keys()) {
    if (COMMON_ARTICLE_QUERY_KEYS.has(key.toLowerCase()) && /\d/.test(url.searchParams.get(key) ?? "")) return true;
  }
  if (/\d{6,}/.test(url.pathname)) return true;
  if (/[A-Z]{3,}\d{6,}/i.test(url.pathname)) return true;
  try {
    const sample = new URL(sampleUrl);
    const samplePrefix = staticPathPrefix(sample.pathname);
    return samplePrefix.length >= 6 && url.pathname.startsWith(samplePrefix)
      && url.pathname !== sample.pathname;
  } catch {
    return false;
  }
}

function staticPathPrefix(pathname) {
  const segments = pathname.split("/").filter(Boolean);
  const stable = [];
  for (const segment of segments) {
    if (/\d|[A-Z]{3,}\d/i.test(segment)) break;
    stable.push(segment);
  }
  return stable.length ? `/${stable.join("/")}/` : "";
}

function interleaveCandidates(discoveries) {
  const output = [];
  const maxLength = Math.max(0, ...discoveries.map((item) => item.candidates.length));
  for (let index = 0; index < maxLength; index += 1) {
    for (const discovery of discoveries) {
      const candidate = discovery.candidates[index];
      if (candidate) output.push(candidate);
    }
  }
  return output;
}

async function runWithConcurrency(items, concurrency, operation) {
  const results = new Array(items.length);
  let cursor = 0;
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (!stopRequested) {
      const index = cursor;
      cursor += 1;
      if (index >= items.length) return;
      results[index] = await operation(items[index]);
    }
  });
  await Promise.all(workers);
  return results.filter(Boolean);
}

function waitForHost(inputUrl) {
  const host = new URL(inputUrl).host.toLowerCase();
  const previous = hostQueues.get(host) ?? Promise.resolve();
  const scheduled = previous.then(async () => {
    const lastRequestAt = hostLastRequestAt.get(host) ?? 0;
    const remaining = options.hostDelayMs - (Date.now() - lastRequestAt);
    if (remaining > 0) await interruptibleDelay(remaining);
    hostLastRequestAt.set(host, Date.now());
  });
  hostQueues.set(host, scheduled.catch(() => {}));
  return scheduled;
}

function normalizeArticleUrl(value) {
  const url = new URL(value);
  url.hash = "";
  for (const key of [...url.searchParams.keys()]) {
    if (TRACKING_PARAMETERS.has(key.toLowerCase())) url.searchParams.delete(key);
  }
  return url.toString().replace(/\/$/, "");
}

function normalizeDomain(value) {
  return String(value ?? "").trim().toLowerCase().replace(/^www\./, "");
}

function samePublisherDomain(value, domain) {
  try {
    const candidate = normalizeDomain(new URL(value).hostname);
    return candidate === domain || candidate.endsWith(`.${domain}`) || domain.endsWith(`.${candidate}`);
  } catch {
    return false;
  }
}

function numericUrlRank(value) {
  const matches = value.match(/\d+/g) ?? [];
  const last = matches.at(-1) ?? "0";
  return Number(last.slice(-15)) || 0;
}

function normalizedDate(value) {
  const parsed = new Date(value || 0);
  return Number.isNaN(parsed.getTime()) ? new Date(0).toISOString() : parsed.toISOString();
}

function cleanArticleBody(domain, value) {
  let output = value.replace(/\r/g, "").replace(/[ \t]+\n/g, "\n").trim();
  if (domain === "sedaily.com") {
    output = output.replace(/^뉴스 듣기\s+글자 크기[\s\S]{0,300}?다음 채널구독\s*/u, "");
  }
  return output.replace(/\n{3,}/g, "\n\n").trim();
}

function cleanTitle(value) {
  return String(value ?? "").replace(/^\[(?:헬로티 HelloT|서울경제)\]\s*/i, "").trim();
}

function cleanSummary(value, body) {
  return (String(value ?? "").trim() || body.slice(0, 300)).slice(0, 1_000);
}

function normalizedPublishedTime(value, fallback) {
  const parsed = new Date(value || fallback);
  return Number.isNaN(parsed.getTime()) ? new Date().toISOString() : parsed.toISOString();
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

function countWords(value) {
  return value.trim() ? value.trim().split(/\s+/).length : 0;
}

function failureStatus(message) {
  if (message === "article_robots_disallowed") return "robots_disallowed";
  if (message === "article_robots_unavailable") return "robots_unavailable";
  if (/article_fetch_http_(?:403|406|429)/.test(message)) return "http_blocked";
  if (isPermanentNotFound(message)) return "not_found";
  return "extract_failed";
}

function retryDelayMs(message, attempt) {
  if (/article_fetch_http_(?:403|406|429)/.test(message)) {
    return Math.min(30 * 60_000, 60_000 * (2 ** (attempt - 1)));
  }
  return Math.min(60_000, 2_000 * (2 ** (attempt - 1)));
}

function isPermanentNotFound(message) {
  return message === "article_not_found" || /article_fetch_http_(?:404|410)/.test(message);
}

function isRobotsStop(message) {
  return message === "article_robots_disallowed" || message === "article_robots_unavailable";
}

function readOptions(args) {
  const values = new Map();
  const flags = new Set();
  for (let index = 0; index < args.length; index += 1) {
    const token = args[index];
    if (!token.startsWith("--")) throw new Error(`unexpected argument: ${token}`);
    const name = token.slice(2);
    if (name === "watch") {
      flags.add(name);
      continue;
    }
    const value = args[index + 1];
    if (value === undefined || value.startsWith("--")) throw new Error(`missing value for --${name}`);
    values.set(name, value);
    index += 1;
  }
  const domains = values.has("domains")
    ? new Set(values.get("domains").split(",").map(normalizeDomain).filter(Boolean))
    : null;
  return {
    watch: flags.has("watch"),
    domains,
    intervalMinutes: integerOption(values.get("interval-minutes"), 360, 30, 1_440),
    concurrency: integerOption(values.get("concurrency"), 8, 1, 20),
    discoveryConcurrency: integerOption(values.get("discovery-concurrency"), 8, 1, 20),
    retries: integerOption(values.get("retries"), 2, 1, 10),
    hostDelayMs: integerOption(values.get("host-delay-ms"), 1_500, 1_000, 60_000),
    failureRetryHours: integerOption(values.get("failure-retry-hours"), 12, 1, 168),
    limitPerDomain: integerOption(values.get("limit-per-domain"), 300, 1, 5_000),
    maxSitemapRoots: integerOption(values.get("max-sitemap-roots"), 3, 1, 20),
    maxSitemapDocuments: integerOption(values.get("max-sitemap-documents"), 5, 1, 50),
    maxSitemapChildren: integerOption(values.get("max-sitemap-children"), 3, 1, 20),
    maxSitemapDepth: integerOption(values.get("max-sitemap-depth"), 2, 0, 5),
    progressEvery: integerOption(values.get("progress-every"), 50, 1, 10_000),
  };
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
    connectionLimit: Math.max(4, options.concurrency + 1),
    queueLimit: 0,
    enableKeepAlive: true,
    keepAliveInitialDelay: 0,
    ssl,
  };
}

async function mysqlConnectionOptionsFromEnvironment() {
  const mysqlUrl = process.env.MYSQL_URL?.trim();
  if (mysqlUrl) return mysqlConnectionOptions(mysqlUrl);

  const host = requiredEnvironment("MYSQL_HOST");
  const caPath = process.env.MYSQL_SSL_CA_PATH?.trim();
  const ssl = caPath
    ? { ca: await readFile(caPath, "utf8"), rejectUnauthorized: true, servername: host }
    : undefined;
  return {
    host,
    port: Number(process.env.MYSQL_PORT?.trim() || 3306),
    user: requiredEnvironment("MYSQL_USER"),
    password: requiredEnvironment("MYSQL_PASSWORD"),
    database: requiredEnvironment("MYSQL_DATABASE"),
    charset: "utf8mb4",
    waitForConnections: true,
    connectionLimit: Math.max(4, options.concurrency + 1),
    queueLimit: 0,
    enableKeepAlive: true,
    keepAliveInitialDelay: 0,
    ssl,
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
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function requestStop() {
  stopRequested = true;
  console.log(JSON.stringify({ phase: "stop_requested", at: new Date().toISOString() }));
}

async function interruptibleDelay(milliseconds) {
  const deadline = Date.now() + milliseconds;
  while (!stopRequested && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, Math.min(5_000, deadline - Date.now())));
  }
}

function safeError(error) {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/mysql:\/\/[^\s@]+@/gi, "mysql://***@").slice(0, 191);
}
