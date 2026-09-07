import { NextResponse } from "next/server";
import { crawlNewsBodies, shouldCrawlNewsBodies } from "../../../lib/news-body-crawler";
import {
  classifyNewsScenario,
  createSearchCompanyProfile,
  googleCompanyNewsFeedUrl,
  googleNewsFeedUrl,
  isLiveNewsRequest,
  liveNewsProfiles,
  naverCompanyNewsApiUrl,
  naverNewsApiUrl,
  parseNaverNewsApi,
  parseNewsRss,
  resolveCompanyProfile,
  toNewsSignal,
  type CompanyProfile,
  type FeedItem,
  type LiveNewsScenario,
} from "../../../lib/news-crawler";
import { companyRegistryStats, hasCompanyNameMention } from "../../../lib/company-entity-recognition";
import { krxListedCompanies, krxRegistryAsOf } from "../../../lib/krx-company-registry.generated";
import { secRegistryAsOf } from "../../../lib/sec-company-registry.generated";
import {
  mergeNewsHistory,
  newsStorageKey,
  type NewsPersistenceState,
} from "../../../db/news-repository";
import { newsStoreFromEnv, type NewsStore } from "../../../db/news-store";

type ProviderResult = {
  provider: "NAVER API HUB" | "Google News RSS";
  items: Array<{ item: FeedItem; scenario: LiveNewsScenario }>;
  fallbackReason?: string;
  companyQuery?: string;
  searchedCompany?: CompanyProfile;
};

export async function GET(request: Request) {
  const url = new URL(request.url);
  const requested = url.searchParams.get("scenario") ?? "all";
  if (!isLiveNewsRequest(requested)) {
    return NextResponse.json({ error: "unknown_scenario", available: ["all", "battery", "semiconductor", "governance"] }, { status: 400 });
  }
  const companyInput = url.searchParams.get("company");
  const companyQuery = companyInput?.trim().replace(/\s+/g, " ") ?? "";
  if (companyInput !== null && (companyQuery.length < 2 || companyQuery.length > 40 || !/^[a-zA-Z0-9가-힣&().,+\-\s]+$/.test(companyQuery))) {
    return NextResponse.json({ error: "invalid_company_query", message: "기업명은 2~40자의 한글·영문·숫자로 입력해 주세요." }, { status: 400 });
  }
  const includeBody = url.searchParams.get("includeBody") === "1";
  const bodyLimit = readBodyLimit(url.searchParams.get("bodyLimit"));
  if (bodyLimit === null) {
    return NextResponse.json({ error: "invalid_body_limit", message: "bodyLimit은 1~10 사이의 정수여야 합니다." }, { status: 400 });
  }
  const backfill = url.searchParams.get("backfill") === "1";
  const backfillStart = readBoundedInteger(url.searchParams.get("start"), 1, 1, 1_000);
  const backfillDisplay = readBoundedInteger(url.searchParams.get("display"), 100, 1, 100);
  if (backfill && (requested === "all" || companyQuery || url.searchParams.get("history") === "1")) {
    return NextResponse.json({ error: "invalid_backfill_scope", message: "과거 수집은 개별 scenario에서만 사용할 수 있습니다." }, { status: 400 });
  }
  if (backfill && (backfillStart === null || backfillDisplay === null)) {
    return NextResponse.json({ error: "invalid_backfill_pagination", message: "start는 1~1000, display는 1~100 사이의 정수여야 합니다." }, { status: 400 });
  }

  const collectedAt = new Date();
  const runtimeEnv = await getRuntimeEnv();
  const clientId = runtimeString(runtimeEnv, "NAVER_API_HUB_CLIENT_ID");
  const clientSecret = runtimeString(runtimeEnv, "NAVER_API_HUB_CLIENT_SECRET");
  const apiKeyConfigured = Boolean(clientId && clientSecret);
  if (backfill && !apiKeyConfigured) {
    return NextResponse.json({ error: "naver_api_credentials_required" }, { status: 503, headers: { "cache-control": "no-store" } });
  }
  const db = await newsStoreFromEnv(runtimeEnv);
  const crawlBodies = !backfill && shouldCrawlNewsBodies(includeBody, Boolean(db));
  const storageKey = newsStorageKey(requested, companyQuery);
  let persistence: NewsPersistenceState = {
    enabled: Boolean(db),
    status: db ? "stored" : "database_unavailable",
    historyCount: 0,
    database: db?.kind,
  };
  if (url.searchParams.get("history") === "1") {
    if (!db) {
      return NextResponse.json({ error: "news_database_unavailable", items: [], persistence }, {
        status: 503,
        headers: { "cache-control": "no-store" },
      });
    }
    try {
      const storedItems = await db.loadHistory(storageKey, collectedAt, companyQuery ? 40 : 60);
      const crawled = crawlBodies
        ? await crawlNewsBodies(storedItems, { limit: bodyLimit ?? undefined })
        : null;
      const bodyStored = crawled
        ? await persistNewsBodies(db, storageKey, crawled.bodyResults, collectedAt.toISOString())
        : true;
      return NextResponse.json({
        requested,
        provider: `${db.label} 저장 뉴스`,
        apiKeyConfigured,
        companyQuery: companyQuery || null,
        collectedAt: collectedAt.toISOString(),
        refreshAfterSeconds: 300,
        persistence: { enabled: true, status: bodyStored ? "stored" : "database_error", historyCount: storedItems.length, database: db.kind },
        bodyCrawl: crawled?.stats ?? { enabled: false },
        items: newsResponseItems(crawled?.items ?? storedItems, includeBody),
      }, {
        status: storedItems.length ? 200 : 404,
        headers: { "cache-control": "no-store", "x-news-source": `${db.kind}-stored-news` },
      });
    } catch (error) {
      logNewsDatabaseError("history_request", error);
      return NextResponse.json({
        error: "news_database_error",
        items: [],
        persistence: { enabled: true, status: "database_error", historyCount: 0, database: db.kind },
      }, { status: 503, headers: { "cache-control": "no-store" } });
    }
  }
  try {
    const result = apiKeyConfigured
      ? await (companyQuery
          ? fetchCompanyFromNaver(companyQuery, clientId!, clientSecret!)
          : fetchFromNaverApi(requested, clientId!, clientSecret!, backfill ? {
              start: backfillStart ?? 1,
              display: backfillDisplay ?? 100,
            } : undefined)).catch(async (error: unknown) => {
          if (backfill) throw error;
          const fallback = companyQuery ? await fetchCompanyFromGoogleRss(companyQuery) : await fetchFromGoogleRss(requested);
          return { ...fallback, fallbackReason: error instanceof Error ? error.message : "naver_api_failed" };
        })
      : companyQuery ? await fetchCompanyFromGoogleRss(companyQuery) : await fetchFromGoogleRss(requested);

    const normalized = result.items
      .filter(({ item }) => !result.searchedCompany || mentionsCompany(item, result.searchedCompany, companyQuery))
      .map(({ item, scenario }) => toNewsSignal(item, scenario, collectedAt, {
        searchedCompany: result.searchedCompany,
      }))
      .sort((a, b) => Date.parse(b.publishedAtIso ?? "") - Date.parse(a.publishedAtIso ?? ""))
      .filter((item, index, all) => all.findIndex((candidate) => candidate.sourceUrl === item.sourceUrl) === index);
    const resultLimit = backfill ? (backfillDisplay ?? 100) : 16;
    const categoryBalanced = !backfill && requested === "all" && !companyQuery
      ? (Object.keys(liveNewsProfiles) as LiveNewsScenario[]).flatMap((scenario) => normalized.filter((item) => item.scenarioId === scenario).slice(0, 5))
      : normalized.slice(0, resultLimit);
    const balancedIds = new Set(categoryBalanced.map((item) => item.id));
    const currentItems = [...categoryBalanced, ...normalized.filter((item) => !balancedIds.has(item.id)).slice(0, Math.max(0, resultLimit - categoryBalanced.length))]
      .sort((a, b) => Date.parse(b.publishedAtIso ?? "") - Date.parse(a.publishedAtIso ?? ""))
      .slice(0, resultLimit);
    if (!currentItems.length) throw new Error("empty_feed");

    let items = currentItems;
    if (db) {
      let storedItems = [] as typeof currentItems;
      let databaseError = false;
      if (!backfill) {
        try {
          storedItems = await db.loadHistory(storageKey, collectedAt, companyQuery ? 40 : 60);
        } catch {
          databaseError = true;
        }
        items = mergeNewsHistory(currentItems, storedItems, companyQuery ? 40 : 60);
      }
      try {
        await db.saveHistory(storageKey, currentItems, result.provider, collectedAt.toISOString());
      } catch {
        databaseError = true;
      }
      persistence = {
        enabled: true,
        status: databaseError ? "database_error" : "stored",
        historyCount: items.length,
        database: db.kind,
      };
    }

    const crawled = crawlBodies
      ? await crawlNewsBodies(items, { limit: bodyLimit ?? undefined })
      : null;
    if (db && crawled) {
      const bodyStored = await persistNewsBodies(db, storageKey, crawled.bodyResults, collectedAt.toISOString());
      if (!bodyStored) persistence = { ...persistence, status: "database_error" };
    }

    return NextResponse.json({
      requested,
      provider: result.provider,
      apiKeyConfigured,
      fallbackReason: result.fallbackReason ?? null,
      companyQuery: result.companyQuery ?? null,
      companyRegistry: {
        provider: "KRX KIND + SEC EDGAR",
        count: companyRegistryStats.domesticCount + companyRegistryStats.foreignCount,
        domesticCount: krxListedCompanies.length,
        foreignCount: companyRegistryStats.foreignCount,
        foreignTickerAssociations: companyRegistryStats.foreignTickerAssociations,
        asOf: krxRegistryAsOf > secRegistryAsOf ? krxRegistryAsOf : secRegistryAsOf,
      },
      collectedAt: collectedAt.toISOString(),
      pagination: backfill ? { start: backfillStart, display: backfillDisplay } : null,
      refreshAfterSeconds: 300,
      persistence,
      bodyCrawl: crawled?.stats ?? { enabled: false },
      items: newsResponseItems(crawled?.items ?? items, includeBody),
    }, {
      headers: {
        "cache-control": backfill ? "no-store" : "public, max-age=0, s-maxage=300, stale-while-revalidate=600",
        "x-news-source": result.provider.toLowerCase().replaceAll(" ", "-"),
      },
    });
  } catch (error) {
    if (db) {
      try {
        const storedItems = await db.loadHistory(storageKey, collectedAt, companyQuery ? 40 : 60);
        if (storedItems.length) {
          const crawled = crawlBodies
            ? await crawlNewsBodies(storedItems, { limit: bodyLimit ?? undefined })
            : null;
          const bodyStored = crawled
            ? await persistNewsBodies(db, storageKey, crawled.bodyResults, collectedAt.toISOString())
            : true;
          return NextResponse.json({
            requested,
            provider: `${db.label} 저장 뉴스`,
            apiKeyConfigured,
            fallbackReason: error instanceof Error ? error.message : "news_provider_failed",
            companyQuery: companyQuery || null,
            collectedAt: collectedAt.toISOString(),
            refreshAfterSeconds: 300,
            persistence: { enabled: true, status: bodyStored ? "stored" : "database_error", historyCount: storedItems.length, database: db.kind },
            bodyCrawl: crawled?.stats ?? { enabled: false },
            items: newsResponseItems(crawled?.items ?? storedItems, includeBody),
          }, {
            headers: {
              "cache-control": "public, max-age=0, s-maxage=60, stale-while-revalidate=300",
              "x-news-source": `${db.kind}-stored-news`,
            },
          });
        }
      } catch {
        persistence = { enabled: true, status: "database_error", historyCount: 0, database: db.kind };
      }
    }
    return NextResponse.json({
      requested,
      error: "news_fetch_failed",
      message: error instanceof Error ? error.message : "unknown_error",
      apiKeyConfigured,
      collectedAt: collectedAt.toISOString(),
      persistence,
      items: [],
    }, { status: 502, headers: { "cache-control": "no-store" } });
  }
}

async function fetchCompanyFromNaver(companyQuery: string, clientId: string, clientSecret: string): Promise<ProviderResult> {
  const resolved = resolveCompanyProfile(companyQuery);
  const searchedCompany = resolved?.company ?? createSearchCompanyProfile(companyQuery);
  const canonicalQuery = resolved?.company.label ?? companyQuery;
  const response = await fetch(naverCompanyNewsApiUrl(canonicalQuery), {
    headers: {
      accept: "application/json",
      "X-NCP-APIGW-API-KEY-ID": clientId,
      "X-NCP-APIGW-API-KEY": clientSecret,
    },
  });
  const payload = await response.json() as { errorCode?: string; errorMessage?: string };
  if (!response.ok) throw new Error(payload.errorCode ?? `naver_api_${response.status}`);
  const items = parseNaverNewsApi(payload).map((item) => ({
    item,
    scenario: resolved?.scenario ?? classifyNewsScenario(item) ?? "governance" as LiveNewsScenario,
  }));
  if (!items.length) throw new Error("naver_company_news_empty");
  return { provider: "NAVER API HUB", items, companyQuery: canonicalQuery, searchedCompany };
}

async function getRuntimeEnv(): Promise<Record<string, unknown>> {
  try {
    const workerRuntime = await import("cloudflare:workers");
    return workerRuntime.env as Record<string, unknown>;
  } catch {
    return process.env as Record<string, unknown>;
  }
}

function runtimeString(runtimeEnv: Record<string, unknown>, key: string): string | undefined {
  const value = runtimeEnv[key];
  return typeof value === "string" ? value.trim() || undefined : undefined;
}

async function fetchFromNaverApi(
  requested: LiveNewsScenario | "all",
  clientId: string,
  clientSecret: string,
  pagination?: { start: number; display: number },
): Promise<ProviderResult> {
  const scenarios = requested === "all" ? Object.keys(liveNewsProfiles) as LiveNewsScenario[] : [requested];
  const settled = await Promise.allSettled(scenarios.map(async (scenario) => {
    const response = await fetch(naverNewsApiUrl(scenario, pagination), {
      headers: {
        accept: "application/json",
        "X-NCP-APIGW-API-KEY-ID": clientId,
        "X-NCP-APIGW-API-KEY": clientSecret,
      },
    });
    const payload = await response.json() as { errorCode?: string; errorMessage?: string };
    if (!response.ok) throw new Error(payload.errorCode ?? `naver_api_${response.status}`);
    return parseNaverNewsApi(payload).map((item) => ({ item, scenario }));
  }));
  const items = settled.flatMap((result) => result.status === "fulfilled" ? result.value : []);
  if (!items.length) throw new Error("naver_api_empty");
  return { provider: "NAVER API HUB", items };
}

async function fetchFromGoogleRss(requested: LiveNewsScenario | "all"): Promise<ProviderResult> {
  const scenarios = requested === "all" ? Object.keys(liveNewsProfiles) as LiveNewsScenario[] : [requested];
  const settled = await Promise.allSettled(scenarios.map(async (scenario) => {
    const response = await fetch(googleNewsFeedUrl(scenario), {
      headers: {
        accept: "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
        "user-agent": "CosmosRiskBot/1.0 (+source-linked financial news monitor)",
      },
    });
    if (!response.ok) throw new Error(`rss_${response.status}`);
    return parseNewsRss(await response.text()).slice(0, 10).map((item) => ({ item, scenario }));
  }));
  const items = settled.flatMap((result) => result.status === "fulfilled" ? result.value : []);
  if (!items.length) throw new Error("rss_empty");
  return { provider: "Google News RSS", items };
}

async function fetchCompanyFromGoogleRss(companyQuery: string): Promise<ProviderResult> {
  const resolved = resolveCompanyProfile(companyQuery);
  const searchedCompany = resolved?.company ?? createSearchCompanyProfile(companyQuery);
  const canonicalQuery = resolved?.company.label ?? companyQuery;
  const response = await fetch(googleCompanyNewsFeedUrl(canonicalQuery), {
    headers: {
      accept: "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
      "user-agent": "CosmosRiskBot/1.0 (+source-linked financial news monitor)",
    },
  });
  if (!response.ok) throw new Error(`rss_${response.status}`);
  const items = parseNewsRss(await response.text()).slice(0, 16).map((item) => ({
    item,
    scenario: resolved?.scenario ?? classifyNewsScenario(item) ?? "governance" as LiveNewsScenario,
  }));
  if (!items.length) throw new Error("rss_company_news_empty");
  return { provider: "Google News RSS", items, companyQuery: canonicalQuery, searchedCompany };
}

function mentionsCompany(item: FeedItem, company: CompanyProfile, rawQuery: string): boolean {
  return hasCompanyNameMention(`${item.title} ${item.description}`, [company.keyword, company.label, rawQuery]);
}

function readBodyLimit(value: string | null): number | null | undefined {
  if (value === null || value === "") return undefined;
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 1 && parsed <= 10 ? parsed : null;
}

function readBoundedInteger(value: string | null, fallback: number, min: number, max: number): number | null {
  if (value === null || value === "") return fallback;
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= min && parsed <= max ? parsed : null;
}

async function persistNewsBodies(
  db: NewsStore,
  storageKey: string,
  items: import("../../../lib/news-data").NewsSignal[],
  extractedAtIso: string,
): Promise<boolean> {
  try {
    await db.saveBodies(storageKey, items, extractedAtIso);
    return true;
  } catch {
    return false;
  }
}

function newsResponseItems(
  items: import("../../../lib/news-data").NewsSignal[],
  includeBody: boolean,
): import("../../../lib/news-data").NewsSignal[] {
  if (includeBody) return items;
  return items.map((item) => {
    const responseItem = { ...item };
    delete responseItem.articleBody;
    delete responseItem.articleExtraction;
    return responseItem;
  });
}

function logNewsDatabaseError(operation: string, error: unknown): void {
  const details = error instanceof Error
    ? { name: error.name, message: error.message, code: "code" in error ? String(error.code) : undefined }
    : { message: String(error) };
  console.error(`[news-database:${operation}]`, details);
}
