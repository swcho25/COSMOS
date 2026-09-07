import { createHash } from "node:crypto";
import { gunzipSync } from "node:zlib";
import { readFile } from "node:fs/promises";
import { createPool } from "mysql2/promise";
import { parseHTML } from "linkedom";
import { extractNewsArticle } from "../lib/news-article-extractor.ts";
import { assertRobotsAllowed } from "../lib/robots-policy.ts";

const USER_AGENT = "NexusRiskArticleExtractor/1.0 (+news intelligence; source-linked)";
const STORAGE_KEY = "production:publisher-10y-backfill";
const DISCOVERY_BODY_TIMEOUT_MS = 45_000;
const COMMON_ARTICLE_QUERY_KEYS = new Set([
  "no", "idxno", "article_no", "articleno", "newsno", "seq", "aid", "id",
]);
const PAGE_QUERY_KEYS = new Set([
  "page", "pagenum", "page_num", "page_no", "pageno", "pg", "p",
]);
const TRACKING_PARAMETERS = new Set([
  "ref", "inflow", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
]);
const ARCHIVE_LINK_PATTERN = /(?:전체\s*기사|기사\s*목록|뉴스\s*목록|지난\s*기사|과거\s*기사|all\s*news|news\s*list|archive)/iu;
const NEXT_LINK_PATTERN = /^(?:다음|next|next\s*page|›|»|>|다음\s*페이지)$/iu;
const DATE_PATTERN = /\b(20\d{2})[.\-/년\s]+(0?[1-9]|1[0-2])[.\-/월\s]+(0?[1-9]|[12]\d|3[01])(?:일)?\b/u;

loadLocalEnvironment();

const options = readOptions(process.argv.slice(2));
const profileConfig = await loadProfileConfig(options.profileFile);
const pool = createPool(await mysqlConnectionOptionsFromEnvironment());
const hostQueues = new Map();
const hostLastRequestAt = new Map();
let stopRequested = false;

process.on("SIGINT", requestStop);
process.on("SIGTERM", requestStop);

try {
  await ensureSchema();
  const sources = await loadActiveSources();
  await seedProfiles(sources);
  if (options.retryFailures) await resetRetryableFailures(sources);

  let round = 0;
  do {
    round += 1;
    const startedAt = new Date().toISOString();
    const existingUrls = await loadExistingUrls();
    // A previous run may already have discovered a large article backlog. Drain one
    // batch before doing more discovery so a slow or broken sitemap cannot prevent
    // the existing queue from making forward progress after a restart.
    const resumedArticleStats = options.discoverOnly
      ? emptyArticleStats()
      : await processPendingArticles(sources, existingUrls);
    await runWithConcurrency(sources, options.discoveryConcurrency, (source) => seedDiscovery(source));
    const discoveryStats = await runWithConcurrency(
      sources,
      options.discoveryConcurrency,
      (source) => processSourceDiscovery(source, existingUrls),
    );
    const discoveredArticleStats = options.discoverOnly
      ? emptyArticleStats()
      : await processPendingArticles(sources, existingUrls);
    const articleStats = mergeArticleStats(resumedArticleStats, discoveredArticleStats);
    await refreshBackfillStates(sources);
    const pending = await pendingStats(sources);

    console.log(JSON.stringify({
      phase: stopRequested ? "backfill_stopped" : "backfill_round_complete",
      round,
      range: { since: options.sinceIso, until: options.untilIso },
      publishers: sources.length,
      discovery: sumDiscoveryStats(discoveryStats),
      articles: articleStats,
      pending,
      database: await databaseStats(),
      startedAt,
      finishedAt: new Date().toISOString(),
    }));

    if (!options.continuous || options.discoverOnly || stopRequested) break;
    if (Number(pending.documents) === 0 && Number(pending.articles) === 0) break;
    await interruptibleDelay(options.roundPauseSeconds * 1_000);
  } while (!stopRequested);
} catch (error) {
  console.error(JSON.stringify({ phase: "fatal", error: safeError(error), at: new Date().toISOString() }));
  process.exitCode = 1;
} finally {
  await pool.end();
}

async function loadActiveSources() {
  const [rows] = await pool.execute(`
    SELECT s.publisher_domain, s.display_name, s.sample_url,
      p.publisher_domain AS profile_domain,
      p.enabled AS backfill_enabled,
      p.strategy AS backfill_strategy,
      p.sitemap_urls_json,
      p.yearly_sitemap_templates_json,
      p.monthly_sitemap_templates_json,
      p.archive_urls_json,
      p.archive_templates_json
    FROM news_publisher_sources s
    LEFT JOIN news_publisher_backfill_profiles p
      ON p.publisher_domain = s.publisher_domain
    WHERE s.status = 'active' AND s.success_count > 0
    ORDER BY s.success_count DESC, s.publisher_domain
  `);
  return rows.flatMap((row) => {
    const domain = normalizeDomain(row.publisher_domain);
    if (!domain || (options.domains && !options.domains.has(domain))) return [];
    const config = resolvedProfile(domain, row);
    if (!config.enabled) return [];
    try {
      const sampleUrl = normalizeArticleUrl(row.sample_url);
      const parsed = new URL(sampleUrl);
      return [{
        domain,
        displayName: String(row.display_name || domain).slice(0, 191),
        sampleUrl,
        origin: parsed.origin,
        profile: config,
      }];
    } catch {
      return [];
    }
  });
}

async function seedProfiles(sources) {
  for (const source of sources) {
    await pool.execute(`
      INSERT INTO news_publisher_backfill_profiles (
        publisher_domain, display_name, sample_url, enabled, strategy,
        sitemap_urls_json, yearly_sitemap_templates_json, monthly_sitemap_templates_json,
        archive_urls_json, archive_templates_json, updated_at_iso
      ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
      ON DUPLICATE KEY UPDATE
        display_name = VALUES(display_name),
        sample_url = VALUES(sample_url),
        updated_at_iso = VALUES(updated_at_iso)
    `, [
      source.domain,
      source.displayName,
      source.sampleUrl,
      source.profile.strategy,
      JSON.stringify(source.profile.sitemapUrls),
      JSON.stringify(source.profile.yearlySitemapTemplates),
      JSON.stringify(source.profile.monthlySitemapTemplates),
      JSON.stringify(source.profile.archiveUrls),
      JSON.stringify(source.profile.archiveTemplates),
      new Date().toISOString(),
    ]);
  }
}

async function seedDiscovery(source) {
  const documents = [];
  try {
    const robotsText = await fetchRobotsText(source.origin);
    for (const sitemapUrl of robotsSitemaps(robotsText)) {
      if (samePublisherDomain(sitemapUrl, source.domain)) {
        documents.push(documentRecord(source, sitemapUrl, "sitemap", 0, "robots.txt"));
      }
    }
  } catch (error) {
    console.error(JSON.stringify({
      phase: "robots_seed_warning",
      domain: source.domain,
      error: safeError(error),
      at: new Date().toISOString(),
    }));
  }

  for (const value of source.profile.sitemapUrls) {
    documents.push(documentRecord(source, resolveProfileUrl(value, source.origin), "sitemap", 0, "profile"));
  }
  if (options.probeYearlySitemaps) {
    for (const year of targetYears()) {
      for (const template of source.profile.yearlySitemapTemplates) {
        const value = expandTemplate(template, { year, month: "01", page: 1 });
        documents.push(documentRecord(source, resolveProfileUrl(value, source.origin), "sitemap", 0, "year_template"));
      }
    }
  }
  if (options.probeMonthlySitemaps) {
    for (const year of targetYears()) {
      for (let month = 1; month <= 12; month += 1) {
        if (!monthIntersectsRange(year, month)) continue;
        for (const template of source.profile.monthlySitemapTemplates) {
          const value = expandTemplate(template, { year, month: String(month).padStart(2, "0"), page: 1 });
          documents.push(documentRecord(source, resolveProfileUrl(value, source.origin), "sitemap", 0, "month_template"));
        }
      }
    }
  }
  if (options.archiveDiscovery) {
    documents.push(documentRecord(source, source.origin, "archive_seed", 0, "homepage"));
    for (const value of source.profile.archiveUrls) {
      documents.push(documentRecord(source, resolveProfileUrl(value, source.origin), "archive", 0, "profile"));
    }
    for (const template of source.profile.archiveTemplates) {
      documents.push(...archiveTemplateDocuments(source, template));
    }
  }

  await insertDiscoveryDocuments(documents);
  await pool.execute(`
    INSERT INTO news_publisher_backfill_state (
      publisher_domain, since_iso, until_iso, status, started_at_iso, updated_at_iso
    ) VALUES (?, ?, ?, 'discovering', ?, ?)
    ON DUPLICATE KEY UPDATE
      since_iso = VALUES(since_iso),
      until_iso = VALUES(until_iso),
      status = IF(status = 'complete', status, VALUES(status)),
      updated_at_iso = VALUES(updated_at_iso)
  `, [source.domain, options.sinceIso, options.untilIso, new Date().toISOString(), new Date().toISOString()]);
}

async function processSourceDiscovery(source, existingUrls) {
  const stats = {
    domain: source.domain,
    sitemapDocuments: 0,
    archiveSeeds: 0,
    archivePages: 0,
    candidates: 0,
    existing: 0,
    notFound: 0,
    robotsDisallowed: 0,
    robotsUnavailable: 0,
    failed: 0,
  };
  let processed = 0;
  let archivePages = 0;

  while (!stopRequested && processed < options.maxDocumentsPerDomain) {
    const document = await nextDiscoveryDocument(source.domain);
    if (!document) break;
    if (document.document_type === "archive" && archivePages >= options.maxArchivePagesPerDomain) break;
    processed += 1;
    if (document.document_type === "archive") archivePages += 1;

    try {
      if (document.document_type === "sitemap") {
        const result = await processSitemapDocument(source, document, existingUrls);
        stats.sitemapDocuments += 1;
        stats.candidates += result.candidates;
        stats.existing += result.existing;
      } else if (document.document_type === "archive_seed") {
        const result = await processArchiveSeed(source, document);
        stats.archiveSeeds += 1;
        stats.candidates += result.candidates;
      } else {
        const result = await processArchiveDocument(source, document, existingUrls);
        stats.archivePages += 1;
        stats.candidates += result.candidates;
        stats.existing += result.existing;
      }
      await markDiscoveryDocument(document.document_hash, "processed", null);
    } catch (error) {
      const message = safeError(error);
      const status = discoveryFailureStatus(message);
      if (status === "not_found") stats.notFound += 1;
      else if (status === "robots_disallowed") stats.robotsDisallowed += 1;
      else if (status === "robots_unavailable") stats.robotsUnavailable += 1;
      else stats.failed += 1;
      await markDiscoveryDocument(document.document_hash, status, message);
      if (status !== "not_found") {
        console.error(JSON.stringify({
          phase: "discovery_document_failed",
          domain: source.domain,
          type: document.document_type,
          url: document.document_url,
          error: message,
          at: new Date().toISOString(),
        }));
      }
    }
  }

  console.log(JSON.stringify({ phase: "publisher_discovery", ...stats, at: new Date().toISOString() }));
  return stats;
}

async function processSitemapDocument(source, document, existingUrls) {
  const fetched = await fetchDiscoveryDocument(document.document_url, source.domain, options.maxSitemapBytes);
  const parsed = sitemapRecords(fetched.text);
  if (parsed.isIndex) {
    const children = parsed.records
      .filter((record) => samePublisherDomain(record.loc, source.domain))
      .filter((record) => sitemapRecordIntersectsRange(record))
      .filter(() => document.depth < options.maxSitemapDepth)
      .map((record) => documentRecord(
        source,
        record.loc,
        "sitemap",
        document.depth + 1,
        document.document_url,
        normalizedOptionalDate(record.lastmod),
      ));
    await insertDiscoveryDocuments(children);
    return { candidates: 0, existing: 0 };
  }

  const candidates = parsed.records.flatMap((record) => candidateFromRecord(
    source,
    record.loc,
    record.lastmod,
    `sitemap:${document.document_url}`,
  ));
  return insertArticleCandidates(candidates, existingUrls);
}

async function processArchiveSeed(source, document) {
  const fetched = await fetchDiscoveryDocument(document.document_url, source.domain, options.maxArchiveBytes, "html");
  const { document: htmlDocument } = parseHTML(fetched.text);
  const archiveUrls = [...htmlDocument.querySelectorAll("a[href]")].flatMap((anchor) => {
    const href = anchor.getAttribute("href") ?? "";
    const text = cleanInline(anchor.textContent);
    let resolved;
    try {
      resolved = normalizeDiscoveryUrl(new URL(href, fetched.finalUrl).toString());
    } catch {
      return [];
    }
    const haystack = `${text} ${new URL(resolved).pathname} ${new URL(resolved).search}`;
    if (!ARCHIVE_LINK_PATTERN.test(haystack) || !samePublisherDomain(resolved, source.domain)) return [];
    return [documentRecord(source, resolved, "archive", 0, document.document_url)];
  });
  await insertDiscoveryDocuments(archiveUrls.slice(0, options.maxArchiveSeedsPerDomain));
  return { candidates: archiveUrls.length };
}

async function processArchiveDocument(source, document, existingUrls) {
  const fetched = await fetchDiscoveryDocument(document.document_url, source.domain, options.maxArchiveBytes, "html");
  const { document: htmlDocument } = parseHTML(fetched.text);
  const candidates = [];
  const pageDates = [];
  const pagination = [];

  for (const anchor of htmlDocument.querySelectorAll("a[href]")) {
    const href = anchor.getAttribute("href") ?? "";
    let resolved;
    try {
      resolved = normalizeArticleUrl(new URL(href, fetched.finalUrl).toString());
    } catch {
      continue;
    }
    if (!samePublisherDomain(resolved, source.domain)) continue;
    const text = cleanInline(anchor.textContent);
    const date = dateFromArchiveAnchor(anchor);
    if (date) pageDates.push(date);

    if (isLikelyArticleUrl(new URL(resolved), source.sampleUrl)) {
      if (!date || dateWithinRange(date)) {
        candidates.push(...candidateFromRecord(source, resolved, date, `archive:${document.document_url}`));
      }
      continue;
    }
    if (isPaginationLink(resolved, text, fetched.finalUrl)) {
      pagination.push(documentRecord(source, resolved, "archive", document.depth + 1, document.document_url));
    }
  }

  const result = await insertArticleCandidates(candidates, existingUrls);
  const oldest = pageDates.map((value) => Date.parse(value)).filter(Number.isFinite).sort((a, b) => a - b)[0];
  if (!oldest || oldest >= Date.parse(options.sinceIso)) {
    await insertDiscoveryDocuments(pagination.slice(0, options.maxPaginationLinksPerPage));
  }
  return result;
}

async function processPendingArticles(sources, existingUrls) {
  const sourceByDomain = new Map(sources.map((source) => [source.domain, source]));
  const rows = await loadPendingArticles(sources);
  const stats = emptyArticleStats();
  stats.targets = rows.length;
  let cursor = 0;
  const workers = Array.from({ length: Math.min(options.concurrency, rows.length) }, async () => {
    while (!stopRequested) {
      const candidate = rows[cursor];
      cursor += 1;
      if (!candidate) return;
      const source = sourceByDomain.get(candidate.publisher_domain);
      if (!source) continue;
      stats.attempted += 1;
      if (existingUrls.has(candidate.article_url)) {
        stats.existing += 1;
        await markArticleCandidate(candidate.url_hash, "existing", null);
        continue;
      }

      try {
        const article = await extractWithRetries(candidate, stats);
        if (!article) continue;
        const finalUrl = normalizeArticleUrl(article.finalUrl);
        if (existingUrls.has(finalUrl)) {
          stats.existing += 1;
          await markArticleCandidate(candidate.url_hash, "existing", null);
          continue;
        }
        const published = resolveArticleDate(article.publishedTime, candidate.last_modified_iso, finalUrl);
        if (!published) {
          stats.dateUnknown += 1;
          await markArticleCandidate(candidate.url_hash, "date_unknown", "article_published_date_unknown");
          continue;
        }
        if (!dateWithinRange(published.iso)) {
          stats.outOfRange += 1;
          await markArticleCandidate(candidate.url_hash, "out_of_range", null);
          continue;
        }
        const articleBody = cleanArticleBody(source.domain, article.textContent);
        if (articleBody.length < 80) throw new Error("article_body_not_found_after_cleanup");
        await storeArticle(source, candidate, article, articleBody, published);
        existingUrls.add(finalUrl);
        existingUrls.add(candidate.article_url);
        stats.stored += 1;
        await markArticleCandidate(candidate.url_hash, "stored", null);
      } catch (error) {
        const message = safeError(error);
        const status = articleFailureStatus(message);
        if (status === "not_found") stats.notFound += 1;
        else if (status === "robots_disallowed") stats.robotsDisallowed += 1;
        else if (status === "robots_unavailable") stats.robotsUnavailable += 1;
        else stats.failed += 1;
        await markArticleCandidate(candidate.url_hash, status, message);
        console.error(JSON.stringify({
          phase: "historical_article_failed",
          domain: source.domain,
          url: candidate.article_url,
          error: message,
          at: new Date().toISOString(),
        }));
      }

      const completed = stats.stored + stats.existing + stats.outOfRange + stats.dateUnknown
        + stats.notFound + stats.robotsDisallowed + stats.robotsUnavailable + stats.failed;
      if (completed > 0 && completed % options.progressEvery === 0) {
        console.log(JSON.stringify({ phase: "historical_progress", stats, at: new Date().toISOString() }));
      }
    }
  });
  await Promise.all(workers);
  return stats;
}

async function extractWithRetries(candidate, stats) {
  let lastError;
  for (let attempt = 1; attempt <= options.retries; attempt += 1) {
    await waitForHost(candidate.article_url);
    try {
      return await extractNewsArticle(candidate.article_url, {
        timeoutMs: 25_000,
        maxResponseBytes: 2_000_000,
        maxContentChars: 500_000,
        minContentChars: 80,
        maxRedirects: 4,
        userAgent: USER_AGENT,
      });
    } catch (error) {
      lastError = error;
      const message = safeError(error);
      if (isRobotsStop(message) || isPermanentNotFound(message)) break;
      if (attempt >= options.retries) break;
      stats.retried += 1;
      await interruptibleDelay(retryDelayMs(message, attempt));
    }
  }
  throw lastError ?? new Error("article_extraction_failed");
}

async function storeArticle(source, candidate, article, articleBody, published) {
  const collectedAtIso = new Date().toISOString();
  const scenarioId = classifyScenario(`${article.title}\n${articleBody}`);
  const canonicalUrl = normalizeArticleUrl(article.finalUrl);
  const articleId = `history:${candidate.url_hash.slice(0, 32)}`;
  const provider = String(article.siteName || source.displayName || source.domain).slice(0, 64);
  const extraction = {
    status: "extracted",
    requestedUrl: article.requestedUrl,
    finalUrl: article.finalUrl,
    method: article.method,
    charCount: articleBody.length,
    wordCount: countWords(articleBody),
    truncated: article.truncated,
    robots: article.robots,
    publishedDateSource: published.source,
  };
  const payload = {
    id: articleId,
    scenarioId: scenarioId === "industry" ? undefined : scenarioId,
    tag: scenarioTag(scenarioId),
    title: cleanTitle(article.title),
    source: provider,
    sourceUrl: canonicalUrl,
    articleUrl: canonicalUrl,
    sourceType: "언론 보도",
    time: published.iso.slice(0, 10),
    publishedAt: published.iso.slice(0, 10),
    publishedAtIso: published.iso,
    location: "대한민국",
    sentiment: "중립",
    confidence: 100,
    summary: cleanSummary(article.excerpt, articleBody),
    paragraphs: articleBody.split(/\n{2,}/).filter(Boolean).slice(0, 3),
    evidence: [],
    entities: [],
    relations: [],
    affected: [],
    discoveredVia: candidate.discovered_via,
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
    published.iso,
    collectedAtIso,
    provider,
    canonicalUrl,
    articleBody,
    JSON.stringify(extraction),
    collectedAtIso,
    JSON.stringify(payload),
  ]);
}

async function ensureSchema() {
  await pool.execute(`
    CREATE TABLE IF NOT EXISTS news_publisher_backfill_profiles (
      publisher_domain VARCHAR(191) NOT NULL,
      display_name VARCHAR(191) NOT NULL,
      sample_url TEXT NOT NULL,
      enabled TINYINT(1) NOT NULL DEFAULT 1,
      strategy VARCHAR(32) NOT NULL DEFAULT 'auto',
      sitemap_urls_json LONGTEXT NOT NULL,
      yearly_sitemap_templates_json LONGTEXT NOT NULL,
      monthly_sitemap_templates_json LONGTEXT NOT NULL,
      archive_urls_json LONGTEXT NOT NULL,
      archive_templates_json LONGTEXT NOT NULL,
      updated_at_iso VARCHAR(40) NOT NULL,
      PRIMARY KEY (publisher_domain),
      INDEX idx_backfill_profiles_enabled (enabled, publisher_domain)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  `);
  await pool.execute(`
    CREATE TABLE IF NOT EXISTS news_publisher_backfill_documents (
      document_hash CHAR(64) NOT NULL,
      publisher_domain VARCHAR(191) NOT NULL,
      document_url TEXT NOT NULL,
      document_type VARCHAR(24) NOT NULL,
      depth INT UNSIGNED NOT NULL DEFAULT 0,
      discovered_via VARCHAR(191) NOT NULL,
      last_modified_iso VARCHAR(40),
      status VARCHAR(32) NOT NULL DEFAULT 'pending',
      attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
      last_error VARCHAR(191),
      created_at_iso VARCHAR(40) NOT NULL,
      updated_at_iso VARCHAR(40) NOT NULL,
      PRIMARY KEY (document_hash),
      INDEX idx_backfill_documents_domain_status (publisher_domain, status, document_type),
      INDEX idx_backfill_documents_status (status, updated_at_iso)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  `);
  await pool.execute(`
    CREATE TABLE IF NOT EXISTS news_publisher_backfill_queue (
      url_hash CHAR(64) NOT NULL,
      publisher_domain VARCHAR(191) NOT NULL,
      article_url TEXT NOT NULL,
      last_modified_iso VARCHAR(40),
      discovered_via VARCHAR(191) NOT NULL,
      status VARCHAR(32) NOT NULL DEFAULT 'pending',
      attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
      last_error VARCHAR(191),
      created_at_iso VARCHAR(40) NOT NULL,
      updated_at_iso VARCHAR(40) NOT NULL,
      PRIMARY KEY (url_hash),
      INDEX idx_backfill_queue_domain_status (publisher_domain, status, last_modified_iso),
      INDEX idx_backfill_queue_status (status, updated_at_iso)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  `);
  await pool.execute(`
    CREATE TABLE IF NOT EXISTS news_publisher_backfill_state (
      publisher_domain VARCHAR(191) NOT NULL,
      since_iso VARCHAR(40) NOT NULL,
      until_iso VARCHAR(40) NOT NULL,
      status VARCHAR(32) NOT NULL,
      sitemap_documents INT UNSIGNED NOT NULL DEFAULT 0,
      archive_pages INT UNSIGNED NOT NULL DEFAULT 0,
      discovered_candidates BIGINT UNSIGNED NOT NULL DEFAULT 0,
      pending_candidates BIGINT UNSIGNED NOT NULL DEFAULT 0,
      stored_articles BIGINT UNSIGNED NOT NULL DEFAULT 0,
      existing_articles BIGINT UNSIGNED NOT NULL DEFAULT 0,
      out_of_range_articles BIGINT UNSIGNED NOT NULL DEFAULT 0,
      robots_disallowed BIGINT UNSIGNED NOT NULL DEFAULT 0,
      failed_articles BIGINT UNSIGNED NOT NULL DEFAULT 0,
      oldest_candidate_iso VARCHAR(40),
      newest_candidate_iso VARCHAR(40),
      last_error VARCHAR(191),
      started_at_iso VARCHAR(40) NOT NULL,
      updated_at_iso VARCHAR(40) NOT NULL,
      completed_at_iso VARCHAR(40),
      PRIMARY KEY (publisher_domain),
      INDEX idx_backfill_state_status (status, publisher_domain)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  `);
}

async function insertDiscoveryDocuments(documents) {
  const unique = uniqueBy(documents.filter(Boolean), (item) => item.document_hash);
  for (const chunk of chunks(unique, 250)) {
    const placeholders = chunk.map(() => "(?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)").join(",");
    const values = chunk.flatMap((item) => [
      item.document_hash,
      item.publisher_domain,
      item.document_url,
      item.document_type,
      item.depth,
      item.discovered_via.slice(0, 191),
      item.last_modified_iso,
      item.created_at_iso,
      item.created_at_iso,
    ]);
    await pool.execute(`
      INSERT IGNORE INTO news_publisher_backfill_documents (
        document_hash, publisher_domain, document_url, document_type, depth,
        discovered_via, last_modified_iso, status, attempt_count, last_error,
        created_at_iso, updated_at_iso
      ) VALUES ${placeholders}
    `, values);
  }
}

async function insertArticleCandidates(candidates, existingUrls) {
  const unique = uniqueBy(candidates.filter(Boolean), (item) => item.url_hash);
  let existing = 0;
  for (const candidate of unique) {
    if (existingUrls.has(candidate.article_url)) {
      candidate.status = "existing";
      existing += 1;
    }
  }
  for (const chunk of chunks(unique, 250)) {
    const placeholders = chunk.map(() => "(?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)").join(",");
    const values = chunk.flatMap((item) => [
      item.url_hash,
      item.publisher_domain,
      item.article_url,
      item.last_modified_iso,
      item.discovered_via.slice(0, 191),
      item.status,
      item.created_at_iso,
      item.created_at_iso,
    ]);
    await pool.execute(`
      INSERT INTO news_publisher_backfill_queue (
        url_hash, publisher_domain, article_url, last_modified_iso, discovered_via,
        status, attempt_count, last_error, created_at_iso, updated_at_iso
      ) VALUES ${placeholders}
      ON DUPLICATE KEY UPDATE
        last_modified_iso = COALESCE(last_modified_iso, VALUES(last_modified_iso)),
        discovered_via = VALUES(discovered_via),
        updated_at_iso = VALUES(updated_at_iso)
    `, values);
  }
  return { candidates: unique.length, existing };
}

async function nextDiscoveryDocument(domain) {
  const [rows] = await pool.execute(`
    SELECT document_hash, publisher_domain, document_url, document_type, depth,
      discovered_via, last_modified_iso
    FROM news_publisher_backfill_documents
    WHERE publisher_domain = ? AND status = 'pending'
    ORDER BY FIELD(document_type, 'sitemap', 'archive_seed', 'archive'), depth, document_url
    LIMIT 1
  `, [domain]);
  return rows[0] ?? null;
}

async function markDiscoveryDocument(hash, status, error) {
  await pool.execute(`
    UPDATE news_publisher_backfill_documents SET
      status = ?, attempt_count = attempt_count + 1, last_error = ?, updated_at_iso = ?
    WHERE document_hash = ?
  `, [status, error, new Date().toISOString(), hash]);
}

async function loadPendingArticles(sources) {
  if (!sources.length) return [];
  const domains = sources.map((source) => source.domain);
  // The queue can contain more than a million rows. A window function over the
  // entire table forces MySQL to sort all pending URLs before returning a small
  // batch. Query each publisher through the existing
  // (publisher_domain, status, last_modified_iso) index instead, then merge the
  // bounded per-publisher results in memory.
  const batches = await runWithConcurrency(
    domains,
    Math.min(options.concurrency, domains.length),
    async (domain) => {
      const [dated] = await pool.execute(`
        SELECT url_hash, publisher_domain, article_url, last_modified_iso, discovered_via
        FROM news_publisher_backfill_queue
        WHERE publisher_domain = ? AND status = 'pending' AND last_modified_iso IS NOT NULL
        ORDER BY last_modified_iso, url_hash
        LIMIT ?
      `, [domain, options.maxArticlesPerDomainRun]);
      if (dated.length >= options.maxArticlesPerDomainRun) return dated;

      const [undated] = await pool.execute(`
        SELECT url_hash, publisher_domain, article_url, last_modified_iso, discovered_via
        FROM news_publisher_backfill_queue
        WHERE publisher_domain = ? AND status = 'pending' AND last_modified_iso IS NULL
        ORDER BY url_hash
        LIMIT ?
      `, [domain, options.maxArticlesPerDomainRun - dated.length]);
      return [...dated, ...undated];
    },
  );

  return batches
    .flat()
    .sort((left, right) => {
      const leftDate = left.last_modified_iso ?? "9999-12-31T00:00:00.000Z";
      const rightDate = right.last_modified_iso ?? "9999-12-31T00:00:00.000Z";
      return leftDate.localeCompare(rightDate)
        || left.publisher_domain.localeCompare(right.publisher_domain)
        || left.article_url.localeCompare(right.article_url);
    })
    .slice(0, options.maxArticlesPerRun);
}

async function markArticleCandidate(hash, status, error) {
  await pool.execute(`
    UPDATE news_publisher_backfill_queue SET
      status = ?, attempt_count = attempt_count + 1, last_error = ?, updated_at_iso = ?
    WHERE url_hash = ?
  `, [status, error, new Date().toISOString(), hash]);
}

async function resetRetryableFailures(sources) {
  if (!sources.length) return;
  const domains = sources.map((source) => source.domain);
  const placeholders = domains.map(() => "?").join(",");
  const cutoff = new Date(Date.now() - options.failureRetryHours * 60 * 60_000).toISOString();
  await pool.execute(`
    UPDATE news_publisher_backfill_documents
    SET status = 'pending', last_error = NULL, updated_at_iso = ?
    WHERE publisher_domain IN (${placeholders})
      AND status IN ('fetch_failed', 'robots_unavailable') AND updated_at_iso < ?
  `, [new Date().toISOString(), ...domains, cutoff]);
  await pool.execute(`
    UPDATE news_publisher_backfill_queue
    SET status = 'pending', last_error = NULL, updated_at_iso = ?
    WHERE publisher_domain IN (${placeholders})
      AND status IN ('extract_failed', 'http_blocked', 'robots_unavailable') AND updated_at_iso < ?
  `, [new Date().toISOString(), ...domains, cutoff]);
}

async function refreshBackfillStates(sources) {
  for (const source of sources) {
    const [documentRows] = await pool.execute(`
      SELECT
        SUM(document_type = 'sitemap' AND status = 'processed') AS sitemap_documents,
        SUM(document_type = 'archive' AND status = 'processed') AS archive_pages,
        SUM(status = 'pending') AS pending_documents,
        MAX(last_error) AS last_error
      FROM news_publisher_backfill_documents
      WHERE publisher_domain = ?
    `, [source.domain]);
    const [queueRows] = await pool.execute(`
      SELECT COUNT(*) AS discovered_candidates,
        SUM(status = 'pending') AS pending_candidates,
        SUM(status = 'stored') AS stored_articles,
        SUM(status = 'existing') AS existing_articles,
        SUM(status = 'out_of_range') AS out_of_range_articles,
        SUM(status = 'robots_disallowed') AS robots_disallowed,
        SUM(status IN ('extract_failed', 'http_blocked', 'robots_unavailable', 'date_unknown')) AS failed_articles,
        MIN(last_modified_iso) AS oldest_candidate_iso,
        MAX(last_modified_iso) AS newest_candidate_iso,
        MAX(last_error) AS last_error
      FROM news_publisher_backfill_queue
      WHERE publisher_domain = ?
    `, [source.domain]);
    const documents = documentRows[0];
    const queue = queueRows[0];
    const pending = Number(documents.pending_documents || 0) + Number(queue.pending_candidates || 0);
    const status = pending > 0 ? "in_progress" : Number(queue.discovered_candidates || 0) > 0 ? "complete" : "no_candidates";
    const now = new Date().toISOString();
    await pool.execute(`
      INSERT INTO news_publisher_backfill_state (
        publisher_domain, since_iso, until_iso, status, sitemap_documents, archive_pages,
        discovered_candidates, pending_candidates, stored_articles, existing_articles,
        out_of_range_articles, robots_disallowed, failed_articles,
        oldest_candidate_iso, newest_candidate_iso, last_error,
        started_at_iso, updated_at_iso, completed_at_iso
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON DUPLICATE KEY UPDATE
        since_iso = VALUES(since_iso), until_iso = VALUES(until_iso), status = VALUES(status),
        sitemap_documents = VALUES(sitemap_documents), archive_pages = VALUES(archive_pages),
        discovered_candidates = VALUES(discovered_candidates), pending_candidates = VALUES(pending_candidates),
        stored_articles = VALUES(stored_articles), existing_articles = VALUES(existing_articles),
        out_of_range_articles = VALUES(out_of_range_articles), robots_disallowed = VALUES(robots_disallowed),
        failed_articles = VALUES(failed_articles), oldest_candidate_iso = VALUES(oldest_candidate_iso),
        newest_candidate_iso = VALUES(newest_candidate_iso), last_error = VALUES(last_error),
        updated_at_iso = VALUES(updated_at_iso), completed_at_iso = VALUES(completed_at_iso)
    `, [
      source.domain, options.sinceIso, options.untilIso, status,
      Number(documents.sitemap_documents || 0), Number(documents.archive_pages || 0),
      Number(queue.discovered_candidates || 0), Number(queue.pending_candidates || 0),
      Number(queue.stored_articles || 0), Number(queue.existing_articles || 0),
      Number(queue.out_of_range_articles || 0), Number(queue.robots_disallowed || 0),
      Number(queue.failed_articles || 0), queue.oldest_candidate_iso, queue.newest_candidate_iso,
      queue.last_error || documents.last_error, now, now, status === "complete" ? now : null,
    ]);
  }
}

async function pendingStats(sources) {
  if (!sources.length) return { documents: 0, articles: 0 };
  const domains = sources.map((source) => source.domain);
  const placeholders = domains.map(() => "?").join(",");
  const [documentRows] = await pool.execute(`
    SELECT COUNT(*) AS count FROM news_publisher_backfill_documents
    WHERE status = 'pending' AND publisher_domain IN (${placeholders})
  `, domains);
  const [articleRows] = await pool.execute(`
    SELECT COUNT(*) AS count FROM news_publisher_backfill_queue
    WHERE status = 'pending' AND publisher_domain IN (${placeholders})
  `, domains);
  return { documents: Number(documentRows[0].count), articles: Number(articleRows[0].count) };
}

async function databaseStats() {
  const [rows] = await pool.execute(`
    SELECT COUNT(*) AS rows_count,
      COALESCE(SUM(OCTET_LENGTH(article_body)), 0) AS body_bytes,
      COUNT(DISTINCT provider) AS publishers,
      MIN(published_at_iso) AS oldest_article,
      MAX(published_at_iso) AS newest_article
    FROM news_items
    WHERE storage_key = ?
  `, [STORAGE_KEY]);
  return rows[0];
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

async function fetchRobotsText(origin) {
  const robotsUrl = new URL("/robots.txt", origin).toString();
  await waitForHost(robotsUrl);
  const response = await fetch(robotsUrl, {
    headers: { accept: "text/plain,*/*;q=0.1", "user-agent": USER_AGENT },
    redirect: "follow",
    signal: AbortSignal.timeout(20_000),
  });
  if (response.status === 404 || response.status === 410) return "";
  if (!response.ok) throw new Error(`robots_fetch_http_${response.status}`);
  const text = await response.text();
  if (Buffer.byteLength(text, "utf8") > 1_000_000) throw new Error("robots_response_too_large");
  return text;
}

async function fetchDiscoveryDocument(inputUrl, domain, maxBytes, expected = "xml") {
  let currentUrl = normalizeDiscoveryUrl(inputUrl);
  for (let redirectCount = 0; redirectCount <= 4; redirectCount += 1) {
    await waitForHost(currentUrl);
    await assertRobotsAllowed(currentUrl, { userAgent: USER_AGENT, signal: AbortSignal.timeout(20_000) });
    const response = await fetch(currentUrl, {
      headers: {
        accept: expected === "html"
          ? "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"
          : "application/xml,text/xml,text/plain,application/gzip;q=0.9,*/*;q=0.1",
        "accept-language": "ko-KR,ko;q=0.9,en;q=0.5",
        "user-agent": USER_AGENT,
      },
      redirect: "manual",
      signal: AbortSignal.timeout(30_000),
    });
    if (isRedirect(response.status)) {
      const location = response.headers.get("location");
      await response.body?.cancel();
      if (!location || redirectCount === 4) throw new Error("discovery_redirect_error");
      currentUrl = normalizeDiscoveryUrl(new URL(location, currentUrl).toString());
      if (!samePublisherDomain(currentUrl, domain)) throw new Error("discovery_cross_domain_redirect");
      continue;
    }
    if (response.status === 404 || response.status === 410) {
      await response.body?.cancel();
      throw new Error(`discovery_fetch_http_${response.status}`);
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new Error(`discovery_fetch_http_${response.status}`);
    }
    const bytes = await readLimitedBytes(response, maxBytes);
    let decodedBytes = bytes;
    if (bytes[0] === 0x1f && bytes[1] === 0x8b) {
      decodedBytes = gunzipSync(bytes);
      if (decodedBytes.byteLength > maxBytes * 5) throw new Error("discovery_uncompressed_response_too_large");
    }
    return {
      text: new TextDecoder("utf-8", { fatal: false }).decode(decodedBytes),
      finalUrl: response.url || currentUrl,
    };
  }
  throw new Error("discovery_redirect_error");
}

async function readLimitedBytes(response, maxBytes) {
  const contentLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(contentLength) && contentLength > maxBytes) throw new Error("discovery_response_too_large");
  if (!response.body) return new Uint8Array();
  const reader = response.body.getReader();
  const parts = [];
  let total = 0;
  const deadline = Date.now() + DISCOVERY_BODY_TIMEOUT_MS;
  try {
    while (total <= maxBytes) {
      const remainingMs = deadline - Date.now();
      if (remainingMs <= 0) throw new Error("discovery_body_timeout");
      const { done, value } = await readReaderChunk(reader, remainingMs);
      if (done) break;
      if (total + value.byteLength > maxBytes) {
        await reader.cancel();
        throw new Error("discovery_response_too_large");
      }
      parts.push(value);
      total += value.byteLength;
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  }
  const output = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    output.set(part, offset);
    offset += part.byteLength;
  }
  return output;
}

async function readReaderChunk(reader, timeoutMs) {
  let timeout;
  try {
    return await Promise.race([
      reader.read(),
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error("discovery_body_timeout")), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

function documentRecord(source, value, type, depth, via, lastModifiedIso = null) {
  try {
    const url = normalizeDiscoveryUrl(value);
    if (!samePublisherDomain(url, source.domain)) return null;
    const hash = createHash("sha256").update(`${type}:${url}`).digest("hex");
    const now = new Date().toISOString();
    return {
      document_hash: hash,
      publisher_domain: source.domain,
      document_url: url,
      document_type: type,
      depth,
      discovered_via: via,
      last_modified_iso: lastModifiedIso,
      created_at_iso: now,
    };
  } catch {
    return null;
  }
}

function candidateFromRecord(source, value, lastmod, via) {
  try {
    const articleUrl = normalizeArticleUrl(value);
    if (!samePublisherDomain(articleUrl, source.domain)) return [];
    if (!isLikelyArticleUrl(new URL(articleUrl), source.sampleUrl)) return [];
    const lastModifiedIso = normalizedOptionalDate(lastmod) || dateFromUrl(articleUrl);
    if (lastModifiedIso && !dateWithinRange(lastModifiedIso)) return [];
    const now = new Date().toISOString();
    return [{
      url_hash: createHash("sha256").update(articleUrl).digest("hex"),
      publisher_domain: source.domain,
      article_url: articleUrl,
      last_modified_iso: lastModifiedIso,
      discovered_via: via,
      status: "pending",
      created_at_iso: now,
    }];
  } catch {
    return [];
  }
}

function archiveTemplateDocuments(source, template) {
  const definition = typeof template === "string" ? { url: template } : template;
  if (!definition?.url) return [];
  const values = [];
  const years = definition.url.includes("{year}") ? targetYears() : [new Date(options.untilIso).getUTCFullYear()];
  const months = definition.url.includes("{month}") ? Array.from({ length: 12 }, (_, index) => index + 1) : [1];
  for (const year of years) {
    for (const month of months) {
      if (definition.url.includes("{month}") && !monthIntersectsRange(year, month)) continue;
      const page = Number(definition.startPage || 1);
      const url = expandTemplate(definition.url, { year, month: String(month).padStart(2, "0"), page });
      values.push(documentRecord(source, resolveProfileUrl(url, source.origin), "archive", 0, "archive_template"));
    }
  }
  return values;
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

function sitemapRecordIntersectsRange(record) {
  const urlDate = dateFromUrl(record.loc);
  if (urlDate) return dateWithinRange(urlDate);
  const modified = normalizedOptionalDate(record.lastmod);
  if (!modified) return true;
  return Date.parse(modified) >= Date.parse(options.sinceIso) - 366 * 24 * 60 * 60_000;
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
    return samplePrefix.length >= 2 && url.pathname.startsWith(samplePrefix) && url.pathname !== sample.pathname;
  } catch {
    return false;
  }
}

function isPaginationLink(value, text, currentUrl) {
  let candidate;
  let current;
  try {
    candidate = new URL(value);
    current = new URL(currentUrl);
  } catch {
    return false;
  }
  if (candidate.pathname !== current.pathname && !/\/page\/\d+\/?$/i.test(candidate.pathname)) return false;
  if (NEXT_LINK_PATTERN.test(text.trim())) return true;
  for (const key of candidate.searchParams.keys()) {
    const candidateValue = candidate.searchParams.get(key) ?? "";
    if (PAGE_QUERY_KEYS.has(key.toLowerCase()) && /^\d+$/.test(candidateValue)) return true;
  }
  return /\/page\/\d+\/?$/i.test(candidate.pathname);
}

function dateFromArchiveAnchor(anchor) {
  let node = anchor;
  for (let depth = 0; depth < 4 && node; depth += 1) {
    const match = cleanInline(node.textContent).match(DATE_PATTERN);
    if (match) return normalizedOptionalDate(`${match[1]}-${String(match[2]).padStart(2, "0")}-${String(match[3]).padStart(2, "0")}`);
    node = node.parentElement;
  }
  return null;
}

function dateFromUrl(value) {
  const text = String(value);
  const match = text.match(/(?:^|\D)(20\d{2})[/_-]?(0[1-9]|1[0-2])[/_-]?([0-2]\d|3[01])(?:\D|$)/);
  if (match) return normalizedOptionalDate(`${match[1]}-${match[2]}-${match[3]}`);
  const yearMonth = text.match(/(?:^|\D)(20\d{2})[/_-](0[1-9]|1[0-2])(?:\D|$)/);
  return yearMonth ? normalizedOptionalDate(`${yearMonth[1]}-${yearMonth[2]}-01`) : null;
}

function resolveArticleDate(articleValue, sitemapValue, url) {
  const values = [
    { value: articleValue, source: "article" },
    { value: sitemapValue, source: "sitemap_or_archive" },
    { value: dateFromUrl(url), source: "url" },
  ];
  for (const item of values) {
    const iso = normalizedOptionalDate(item.value);
    if (iso) return { iso, source: item.source };
  }
  return null;
}

function dateWithinRange(value) {
  const time = Date.parse(value);
  return Number.isFinite(time) && time >= Date.parse(options.sinceIso) && time <= Date.parse(options.untilIso);
}

function targetYears() {
  const first = new Date(options.sinceIso).getUTCFullYear();
  const last = new Date(options.untilIso).getUTCFullYear();
  return Array.from({ length: last - first + 1 }, (_, index) => first + index);
}

function monthIntersectsRange(year, month) {
  const first = Date.UTC(year, month - 1, 1);
  const last = Date.UTC(year, month, 1) - 1;
  return last >= Date.parse(options.sinceIso) && first <= Date.parse(options.untilIso);
}

function expandTemplate(template, values) {
  return String(template)
    .replaceAll("{year}", String(values.year))
    .replaceAll("{month}", String(values.month))
    .replaceAll("{page}", String(values.page));
}

function resolveProfileUrl(value, origin) {
  return new URL(value, origin).toString();
}

function mergedProfile(domain) {
  const defaults = profileConfig.defaults ?? {};
  const specific = profileConfig.publishers?.[domain] ?? {};
  return {
    enabled: specific.enabled ?? defaults.enabled ?? true,
    strategy: specific.strategy ?? defaults.strategy ?? "auto",
    sitemapUrls: arrayValue(specific.sitemapUrls ?? defaults.sitemapUrls),
    yearlySitemapTemplates: arrayValue(specific.yearlySitemapTemplates ?? defaults.yearlySitemapTemplates),
    monthlySitemapTemplates: arrayValue(specific.monthlySitemapTemplates ?? defaults.monthlySitemapTemplates),
    archiveUrls: arrayValue(specific.archiveUrls ?? defaults.archiveUrls),
    archiveTemplates: arrayValue(specific.archiveTemplates ?? defaults.archiveTemplates),
  };
}

function resolvedProfile(domain, row) {
  const base = row.profile_domain ? databaseProfile(row) : mergedProfile(domain);
  const specific = profileConfig.publishers?.[domain];
  if (!specific) return base;
  return {
    enabled: specific.enabled ?? base.enabled,
    strategy: specific.strategy ?? base.strategy,
    sitemapUrls: specific.sitemapUrls ? arrayValue(specific.sitemapUrls) : base.sitemapUrls,
    yearlySitemapTemplates: specific.yearlySitemapTemplates
      ? arrayValue(specific.yearlySitemapTemplates)
      : base.yearlySitemapTemplates,
    monthlySitemapTemplates: specific.monthlySitemapTemplates
      ? arrayValue(specific.monthlySitemapTemplates)
      : base.monthlySitemapTemplates,
    archiveUrls: specific.archiveUrls ? arrayValue(specific.archiveUrls) : base.archiveUrls,
    archiveTemplates: specific.archiveTemplates ? arrayValue(specific.archiveTemplates) : base.archiveTemplates,
  };
}

function databaseProfile(row) {
  return {
    enabled: Boolean(row.backfill_enabled),
    strategy: row.backfill_strategy || "auto",
    sitemapUrls: parsedArray(row.sitemap_urls_json),
    yearlySitemapTemplates: parsedArray(row.yearly_sitemap_templates_json),
    monthlySitemapTemplates: parsedArray(row.monthly_sitemap_templates_json),
    archiveUrls: parsedArray(row.archive_urls_json),
    archiveTemplates: parsedArray(row.archive_templates_json),
  };
}

function parsedArray(value) {
  try {
    const parsed = JSON.parse(value || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

async function loadProfileConfig(path) {
  const content = await readFile(path, "utf8");
  return JSON.parse(content);
}

function robotsSitemaps(value) {
  return [...value.matchAll(/^\s*Sitemap\s*:\s*(\S+)\s*$/gim)]
    .map((match) => decodeXml(match[1].trim()));
}

function elementText(block, tag) {
  const match = block.match(new RegExp(`<${tag}\\b[^>]*>\\s*([\\s\\S]*?)\\s*<\\/${tag}>`, "i"));
  return match ? cleanXmlValue(match[1]) : "";
}

function cleanXmlValue(value) {
  return decodeXml(value.replace(/^\s*<!\[CDATA\[/, "").replace(/\]\]>\s*$/, "").trim());
}

function decodeXml(value) {
  return value.replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&apos;/g, "'")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">");
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

function normalizeArticleUrl(value) {
  const url = new URL(value);
  if (!/^https?:$/.test(url.protocol)) throw new Error("unsupported_url_protocol");
  url.hash = "";
  for (const key of [...url.searchParams.keys()]) {
    if (TRACKING_PARAMETERS.has(key.toLowerCase())) url.searchParams.delete(key);
  }
  return url.toString().replace(/\/$/, "");
}

function normalizeDiscoveryUrl(value) {
  const url = new URL(value);
  if (!/^https?:$/.test(url.protocol)) throw new Error("unsupported_url_protocol");
  url.hash = "";
  return url.toString();
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

function normalizedOptionalDate(value) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function cleanArticleBody(domain, value) {
  let output = String(value ?? "").replace(/\r/g, "").replace(/[ \t]+\n/g, "\n").trim();
  if (domain === "sedaily.com") {
    output = output.replace(/^뉴스 듣기\s+글자 크기[\s\S]{0,300}?다음 채널구독\s*/u, "");
  }
  return output.replace(/\n{3,}/g, "\n\n").trim();
}

function cleanInline(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function cleanTitle(value) {
  return cleanInline(value).replace(/^\[(?:헬로티 HelloT|서울경제)\]\s*/i, "");
}

function cleanSummary(value, body) {
  return (cleanInline(value) || body.slice(0, 300)).slice(0, 1_000);
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

function discoveryFailureStatus(message) {
  if (message === "article_robots_disallowed") return "robots_disallowed";
  if (message === "article_robots_unavailable") return "robots_unavailable";
  if (/discovery_fetch_http_(?:404|410)/.test(message)) return "not_found";
  if (/discovery_fetch_http_(?:403|406|429)/.test(message)) return "http_blocked";
  return "fetch_failed";
}

function articleFailureStatus(message) {
  if (message === "article_robots_disallowed") return "robots_disallowed";
  if (message === "article_robots_unavailable") return "robots_unavailable";
  if (isPermanentNotFound(message)) return "not_found";
  if (/article_fetch_http_(?:403|406|429)/.test(message)) return "http_blocked";
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

function isRedirect(status) {
  return status === 301 || status === 302 || status === 303 || status === 307 || status === 308;
}

function emptyArticleStats() {
  return {
    targets: 0, attempted: 0, stored: 0, existing: 0, outOfRange: 0, dateUnknown: 0,
    notFound: 0, robotsDisallowed: 0, robotsUnavailable: 0, failed: 0, retried: 0,
  };
}

function mergeArticleStats(...values) {
  const total = emptyArticleStats();
  for (const value of values) {
    for (const key of Object.keys(total)) total[key] += Number(value?.[key] ?? 0);
  }
  return total;
}

function sumDiscoveryStats(values) {
  return values.reduce((total, value) => {
    for (const key of [
      "sitemapDocuments", "archiveSeeds", "archivePages", "candidates", "existing",
      "notFound", "robotsDisallowed", "robotsUnavailable", "failed",
    ]) total[key] = (total[key] ?? 0) + Number(value[key] ?? 0);
    return total;
  }, {});
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

function readOptions(args) {
  const values = new Map();
  const flags = new Set();
  const booleanFlags = new Set([
    "discover-only", "continuous", "probe-monthly-sitemaps", "no-yearly-probes",
    "no-archive", "retry-failures",
  ]);
  for (let index = 0; index < args.length; index += 1) {
    const token = args[index];
    if (!token.startsWith("--")) throw new Error(`unexpected argument: ${token}`);
    const name = token.slice(2);
    if (booleanFlags.has(name)) {
      flags.add(name);
      continue;
    }
    const value = args[index + 1];
    if (value === undefined || value.startsWith("--")) throw new Error(`missing value for --${name}`);
    values.set(name, value);
    index += 1;
  }
  const until = parseDateOption(values.get("until"), new Date());
  const years = integerOption(values.get("years"), 10, 1, 50);
  const sinceDefault = new Date(until);
  sinceDefault.setUTCFullYear(sinceDefault.getUTCFullYear() - years);
  const since = parseDateOption(values.get("since"), sinceDefault);
  if (since >= until) throw new Error("--since must be earlier than --until");
  const domains = values.has("domains")
    ? new Set(values.get("domains").split(",").map(normalizeDomain).filter(Boolean))
    : null;
  return {
    discoverOnly: flags.has("discover-only"),
    continuous: flags.has("continuous"),
    retryFailures: flags.has("retry-failures"),
    probeYearlySitemaps: !flags.has("no-yearly-probes"),
    probeMonthlySitemaps: flags.has("probe-monthly-sitemaps"),
    archiveDiscovery: !flags.has("no-archive"),
    domains,
    sinceIso: since.toISOString(),
    untilIso: until.toISOString(),
    profileFile: values.get("profile-file") ?? "config/news-publisher-backfill-profiles.json",
    concurrency: integerOption(values.get("concurrency"), 6, 1, 20),
    discoveryConcurrency: integerOption(values.get("discovery-concurrency"), 6, 1, 20),
    retries: integerOption(values.get("retries"), 2, 1, 10),
    hostDelayMs: integerOption(values.get("host-delay-ms"), 2_000, 1_000, 60_000),
    maxDocumentsPerDomain: integerOption(values.get("max-documents-per-domain"), 250, 1, 10_000),
    maxArchivePagesPerDomain: integerOption(values.get("max-archive-pages-per-domain"), 500, 0, 100_000),
    maxArchiveSeedsPerDomain: integerOption(values.get("max-archive-seeds-per-domain"), 8, 1, 100),
    maxPaginationLinksPerPage: integerOption(values.get("max-pagination-links-per-page"), 20, 1, 200),
    maxSitemapDepth: integerOption(values.get("max-sitemap-depth"), 6, 0, 20),
    maxSitemapBytes: integerOption(values.get("max-sitemap-bytes"), 20_000_000, 100_000, 100_000_000),
    maxArchiveBytes: integerOption(values.get("max-archive-bytes"), 3_000_000, 100_000, 20_000_000),
    maxArticlesPerRun: integerOption(values.get("max-articles-per-run"), 20_000, 1, 1_000_000),
    maxArticlesPerDomainRun: integerOption(values.get("max-articles-per-domain-run"), 5_000, 1, 100_000),
    progressEvery: integerOption(values.get("progress-every"), 100, 1, 100_000),
    roundPauseSeconds: integerOption(values.get("round-pause-seconds"), 30, 1, 3_600),
    failureRetryHours: integerOption(values.get("failure-retry-hours"), 24, 1, 720),
  };
}

async function mysqlConnectionOptions(value) {
  const url = new URL(value);
  if (url.protocol !== "mysql:") throw new Error("MYSQL_URL must use mysql://");
  const caPath = process.env.MYSQL_SSL_CA_PATH?.trim();
  const ssl = caPath ? { ca: await readFile(caPath, "utf8"), rejectUnauthorized: true, servername: url.hostname } : undefined;
  return {
    host: url.hostname,
    port: Number(url.port || 3306),
    user: decodeURIComponent(url.username),
    password: decodeURIComponent(url.password),
    database: decodeURIComponent(url.pathname.slice(1)),
    charset: "utf8mb4",
    waitForConnections: true,
    connectionLimit: Math.max(4, options.concurrency + options.discoveryConcurrency + 1),
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
    port: Number(process.env.MYSQL_PORT || 3306),
    user: requiredEnvironment("MYSQL_USER"),
    password: requiredEnvironment("MYSQL_PASSWORD"),
    database: requiredEnvironment("MYSQL_DATABASE"),
    charset: "utf8mb4",
    waitForConnections: true,
    connectionLimit: Math.max(4, options.concurrency + options.discoveryConcurrency + 1),
    queueLimit: 0,
    enableKeepAlive: true,
    keepAliveInitialDelay: 0,
    ssl,
  };
}

function parseDateOption(value, fallback) {
  const parsed = value ? new Date(value) : new Date(fallback);
  if (Number.isNaN(parsed.getTime())) throw new Error(`invalid date: ${value}`);
  return parsed;
}

function integerOption(value, fallback, min, max) {
  if (value === undefined || value === "") return fallback;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`integer option must be between ${min} and ${max}`);
  }
  return parsed;
}

function arrayValue(value) {
  return Array.isArray(value) ? value : [];
}

function chunks(values, size) {
  const output = [];
  for (let index = 0; index < values.length; index += size) output.push(values.slice(index, index + size));
  return output;
}

function uniqueBy(values, key) {
  const map = new Map();
  for (const value of values) if (value) map.set(key(value), value);
  return [...map.values()];
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
