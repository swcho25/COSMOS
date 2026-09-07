import type { NewsSignal } from "./news-data";
import {
  hasCompanyNameMention,
  recognizeCompanyEntities,
  resolveCompanyEntity,
} from "./company-entity-recognition";

export type LiveNewsScenario = "battery" | "semiconductor" | "governance";

export type FeedItem = {
  title: string;
  link: string;
  articleUrl?: string;
  pubDate: string;
  source: string;
  description: string;
  channel?: "naver" | "rss";
};

export type CompanyProfile = { keyword: string; label: string; nodeId: string; code?: string; industry?: string; market?: string };
type ScenarioProfile = {
  query: string;
  tag: string;
  eventLabel: string;
  relationIds: string[];
  companies: CompanyProfile[];
};

export const liveNewsProfiles: Record<LiveNewsScenario, ScenarioProfile> = {
  battery: {
    query: "(LG에너지솔루션 OR 포스코퓨처엠 OR 에코프로비엠 OR 삼성SDI) (배터리 OR 공급망 OR 계약) when:7d",
    tag: "배터리",
    eventLabel: "배터리 공급망 뉴스",
    relationIds: ["contract-lg", "posco-lg", "lg-hyundai"],
    companies: [
      { keyword: "LG에너지솔루션", label: "LG에너지솔루션", nodeId: "lgensol" },
      { keyword: "포스코퓨처엠", label: "포스코퓨처엠", nodeId: "posco" },
      { keyword: "포스코홀딩스", label: "포스코홀딩스", nodeId: "poscoholdings" },
      { keyword: "현대차", label: "현대자동차", nodeId: "hyundai" },
      { keyword: "기아", label: "기아", nodeId: "kia" },
      { keyword: "삼성SDI", label: "삼성SDI", nodeId: "samsung-sdi" },
      { keyword: "에코프로비엠", label: "에코프로비엠", nodeId: "ecopro-bm" },
      { keyword: "SK온", label: "SK온", nodeId: "sk-on" },
      { keyword: "엘앤에프", label: "엘앤에프", nodeId: "lf" },
      { keyword: "CATL", label: "CATL", nodeId: "catl" },
      { keyword: "테슬라", label: "Tesla", nodeId: "tesla" },
      { keyword: "Tesla", label: "Tesla", nodeId: "tesla" },
      { keyword: "GM", label: "GM", nodeId: "gm" },
      { keyword: "Ford", label: "Ford", nodeId: "ford" },
      { keyword: "포드", label: "Ford", nodeId: "ford" },
    ],
  },
  semiconductor: {
    query: "(SK하이닉스 OR 삼성전자 OR 한미반도체 OR ASML) (HBM OR 반도체 OR 메모리) when:7d",
    tag: "반도체",
    eventLabel: "AI 메모리 뉴스",
    relationIds: ["demand-hynix", "demand-samsung", "asml-hynix"],
    companies: [
      { keyword: "SK하이닉스", label: "SK하이닉스", nodeId: "skhynix" },
      { keyword: "삼성전자", label: "삼성전자", nodeId: "samsung" },
      { keyword: "한미반도체", label: "한미반도체", nodeId: "hanmi" },
      { keyword: "ASML", label: "ASML", nodeId: "asml" },
      { keyword: "네이버", label: "NAVER", nodeId: "naver" },
      { keyword: "NAVER", label: "NAVER", nodeId: "naver" },
      { keyword: "엔비디아", label: "NVIDIA", nodeId: "nvidia" },
      { keyword: "NVIDIA", label: "NVIDIA", nodeId: "nvidia" },
      { keyword: "마이크론", label: "Micron", nodeId: "micron" },
      { keyword: "Micron", label: "Micron", nodeId: "micron" },
      { keyword: "TSMC", label: "TSMC", nodeId: "tsmc" },
      { keyword: "브로드컴", label: "Broadcom", nodeId: "broadcom" },
      { keyword: "SK스퀘어", label: "SK스퀘어", nodeId: "sksquare" },
    ],
  },
  governance: {
    query: "(현대모비스 OR 현대자동차 OR 기아 OR 현대제철 OR 현대글로비스) (지분 OR 주주 OR 지배구조 OR 자사주) when:30d",
    tag: "지배구조",
    eventLabel: "지배구조 뉴스",
    relationIds: ["mobis-hyundai", "hyundai-kia", "kia-mobis"],
    companies: [
      { keyword: "현대모비스", label: "현대모비스", nodeId: "mobis-g" },
      { keyword: "현대자동차", label: "현대자동차", nodeId: "hyundai-g" },
      { keyword: "현대차", label: "현대자동차", nodeId: "hyundai-g" },
      { keyword: "기아", label: "기아", nodeId: "kia-g" },
      { keyword: "현대제철", label: "현대제철", nodeId: "steel-g" },
      { keyword: "현대글로비스", label: "현대글로비스", nodeId: "glovis-g" },
      { keyword: "현대위아", label: "현대위아", nodeId: "hyundai-wia" },
      { keyword: "현대오토에버", label: "현대오토에버", nodeId: "hyundai-autoever" },
      { keyword: "현대건설", label: "현대건설", nodeId: "hyundai-ec" },
      { keyword: "현대차증권", label: "현대차증권", nodeId: "hyundai-securities" },
      { keyword: "국민연금", label: "국민연금", nodeId: "nps" },
    ],
  },
};

export function isLiveNewsScenario(value: string | null): value is LiveNewsScenario {
  return value === "battery" || value === "semiconductor" || value === "governance";
}

export function isLiveNewsRequest(value: string | null): value is LiveNewsScenario | "all" {
  return value === "all" || isLiveNewsScenario(value);
}

export function googleNewsFeedUrl(scenario: LiveNewsScenario): string {
  const query = encodeURIComponent(liveNewsProfiles[scenario].query);
  return `https://news.google.com/rss/search?q=${query}&hl=ko&gl=KR&ceid=KR:ko`;
}

export function naverNewsApiUrl(
  scenario: LiveNewsScenario,
  options: { display?: number; start?: number } = {},
): string {
  const query: Record<LiveNewsScenario, string> = {
    battery: "배터리 공급망",
    semiconductor: "HBM 반도체",
    governance: "현대차 지배구조",
  };
  const display = boundedSearchInteger(options.display, 30, 1, 100);
  const start = boundedSearchInteger(options.start, 1, 1, 1_000);
  const params = new URLSearchParams({ query: query[scenario], display: String(display), start: String(start), sort: "date" });
  return `https://naverapihub.apigw.ntruss.com/search/v1/news?${params.toString()}`;
}

export function naverCompanyNewsApiUrl(company: string): string {
  const params = new URLSearchParams({ query: company, display: "30", start: "1", sort: "date" });
  return `https://naverapihub.apigw.ntruss.com/search/v1/news?${params.toString()}`;
}

export function googleCompanyNewsFeedUrl(company: string): string {
  const query = encodeURIComponent(`"${company}" when:7d`);
  return `https://news.google.com/rss/search?q=${query}&hl=ko&gl=KR&ceid=KR:ko`;
}

export function resolveCompanyProfile(value: string): { scenario: LiveNewsScenario; company: CompanyProfile } | null {
  const query = normalizeSearchValue(value);
  if (!query) return null;
  const matches = (Object.entries(liveNewsProfiles) as Array<[LiveNewsScenario, ScenarioProfile]>).flatMap(([scenario, profile]) =>
    profile.companies
      .filter((company) => {
        const keyword = normalizeSearchValue(company.keyword);
        const label = normalizeSearchValue(company.label);
        return keyword === query || label === query || keyword.includes(query) || label.includes(query);
      })
      .map((company) => ({ scenario, company })),
  );
  const unique = matches.filter((match, index) => matches.findIndex((candidate) => candidate.company.nodeId === match.company.nodeId) === index);
  if (unique.length === 1) return { scenario: unique[0].scenario, company: enrichCompanyProfile(unique[0].company) };
  const entity = resolveCompanyEntity(value);
  return entity ? { scenario: scenarioForIndustry(entity.industry), company: entityToProfile(entity) } : null;
}

export function createSearchCompanyProfile(value: string): CompanyProfile {
  const label = value.trim().replace(/\s+/g, " ");
  const entity = resolveCompanyEntity(label);
  if (entity) return entityToProfile(entity);
  return { keyword: label, label, nodeId: `search-${stableHash(normalizeSearchValue(label))}` };
}

export function parseNaverNewsApi(payload: unknown): FeedItem[] {
  if (!payload || typeof payload !== "object" || !("items" in payload) || !Array.isArray(payload.items)) return [];
  return payload.items.flatMap((article): FeedItem[] => {
    if (!article || typeof article !== "object") return [];
    const record = article as Record<string, unknown>;
    const title = typeof record.title === "string" ? cleanText(record.title) : "";
    const originalLink = typeof record.originallink === "string" ? record.originallink : "";
    const naverLink = typeof record.link === "string" ? record.link : "";
    const link = originalLink || naverLink;
    if (!title || !link) return [];
    return [{
      title,
      link,
      articleUrl: originalLink || naverLink,
      pubDate: typeof record.pubDate === "string" ? record.pubDate : "",
      source: sourceFromUrl(link),
      description: typeof record.description === "string" ? cleanText(record.description) : "",
      channel: "naver",
    }];
  });
}

export function classifyNewsScenario(item: FeedItem): LiveNewsScenario | null {
  const text = `${item.title} ${item.description}`.toLowerCase();
  const scores = (Object.keys(liveNewsProfiles) as LiveNewsScenario[]).map((scenario) => ({
    scenario,
    score: liveNewsProfiles[scenario].companies.reduce((total, company) => total + (text.includes(company.keyword.toLowerCase()) ? 2 : 0), 0)
      + keywordScore(text, scenario),
  })).sort((a, b) => b.score - a.score);
  return scores[0]?.score > 0 ? scores[0].scenario : null;
}

export function parseNewsRss(xml: string): FeedItem[] {
  return [...xml.matchAll(/<item>([\s\S]*?)<\/item>/gi)].map((match) => {
    const item = match[1];
    return {
      title: cleanText(readTag(item, "title")),
      link: cleanText(readTag(item, "link")),
      articleUrl: cleanText(readTag(item, "link")),
      pubDate: cleanText(readTag(item, "pubDate")),
      source: cleanText(readTag(item, "source")) || "Google News",
      description: cleanText(readTag(item, "description")),
      channel: "rss" as const,
    };
  }).filter((item) => Boolean(item.title && item.link));
}

export function toNewsSignal(
  item: FeedItem,
  scenario: LiveNewsScenario,
  collectedAt: Date,
  options: { searchedCompany?: CompanyProfile } = {},
): NewsSignal {
  const profile = liveNewsProfiles[scenario];
  const searchable = `${item.title} ${item.description}`.toLowerCase();
  const profiledCompanies = profile.companies.map(enrichCompanyProfile);
  const searchedCompany = options.searchedCompany ? enrichCompanyProfile(options.searchedCompany) : undefined;
  const companyPool = searchedCompany && !profiledCompanies.some((company) => company.nodeId === searchedCompany.nodeId)
    ? [searchedCompany, ...profiledCompanies]
    : profiledCompanies;
  const profiledMatches = companyPool.filter((company) => hasCompanyNameMention(searchable, [company.keyword, company.label]));
  const registryMatches = recognizeCompanyEntities(searchable).map(entityToProfile);
  const matched = uniqueCompanies([...profiledMatches, ...registryMatches]);
  const published = validDate(item.pubDate) ?? collectedAt;
  const sentiment = inferSentiment(searchable);
  const confidence = Math.min(96, 84 + matched.length * 4);
  const summary = normalizeSummary(item.description, item.title, item.source);
  const linkedCompanies = matched;
  const affected = linkedCompanies.map((company, index) => {
    return {
    nodeId: company.nodeId,
    label: company.label,
    score: Math.max(48, confidence - 7 - index * 8),
    reason: "기사 제목·제공 요약에서 국내·해외 종목 마스터의 기업명 직접 인식",
    code: company.code,
    industry: company.industry,
    market: company.market,
  };
  });
  const eventName = compactTitle(item.title);
  const coMentionRelations = affected.slice(1).map((company) => ({
    source: affected[0].label,
    relation: "CO_MENTIONED",
    target: company.label,
    confidence: Math.min(94, Math.round((affected[0].score + company.score) / 2)),
    basis: "reported" as const,
  }));

  return {
    id: `live-${scenario}-${stableHash(`${item.link}-${item.pubDate}`)}`,
    scenarioId: scenario,
    tag: profile.tag,
    title: item.title,
    source: item.source,
    sourceUrl: item.link,
    articleUrl: item.articleUrl ?? item.link,
    sourceType: item.channel === "naver" ? "NAVER API" : "RSS 뉴스",
    time: relativeTime(published, collectedAt),
    publishedAt: formatKoreanDate(published),
    publishedAtIso: published.toISOString(),
    location: item.channel === "naver" ? "NAVER API HUB" : "실시간 뉴스 피드",
    sentiment,
    confidence,
    summary,
    paragraphs: [
      summary,
      `이 항목은 ${formatKoreanDate(collectedAt)}에 ${item.channel === "naver" ? "NAVER API HUB" : "RSS"}에서 자동 수집했습니다. 기사 본문은 저장하지 않으며 원문 링크와 제목·요약만 분석합니다.`,
    ],
    evidence: [{
      prefix: `${item.channel === "naver" ? "NAVER API HUB" : "RSS"}에서 확인한 최신 헤드라인: `,
      highlight: compactTitle(item.title),
      suffix: ` · 발행처 ${item.source}`,
      relationIds: profile.relationIds,
    }],
    entities: [
      ...matched.map((company) => ({ name: company.label, type: "COMPANY" as const })),
      { name: profile.eventLabel, type: "EVENT" as const },
    ],
    relations: [...affected.map((company) => ({
      source: eventName,
      relation: "NEWS_MENTIONS",
      target: company.label,
      confidence: company.score,
      basis: "reported" as const,
    })), ...coMentionRelations],
    affected,
  };
}

function keywordScore(text: string, scenario: LiveNewsScenario): number {
  const keywords: Record<LiveNewsScenario, string[]> = {
    battery: ["battery", "배터리", "cathode", "양극재", "전기차"],
    semiconductor: ["semiconductor", "memory", "hbm", "반도체", "메모리"],
    governance: ["shareholder", "governance", "stake", "지분", "주주", "자사주"],
  };
  return keywords[scenario].reduce((total, keyword) => total + (text.includes(keyword) ? 1 : 0), 0);
}

function readTag(xml: string, tag: string): string {
  const match = xml.match(new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)<\\/${tag}>`, "i"));
  return match?.[1] ?? "";
}

function cleanText(value: string): string {
  return decodeEntities(value.replace(/^<!\[CDATA\[|\]\]>$/g, ""))
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function decodeEntities(value: string): string {
  const named: Record<string, string> = { amp: "&", apos: "'", quot: '"', lt: "<", gt: ">", nbsp: " " };
  return value
    .replace(/&([a-z]+);/gi, (match, name) => named[name.toLowerCase()] ?? match)
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/&#x([\da-f]+);/gi, (_, code) => String.fromCodePoint(Number.parseInt(code, 16)));
}

function normalizeSummary(description: string, title: string, source: string): string {
  const candidate = description.replace(title, "").replace(source, "").trim();
  const text = candidate.length >= 35 ? candidate : `${source}가 배포한 최신 기사입니다. 헤드라인과 발행 정보를 실시간 수집해 관련 기업과 그래프 관계를 자동 연결했습니다.`;
  return text.length > 320 ? `${text.slice(0, 317)}…` : text;
}

function uniqueCompanies(companies: CompanyProfile[]): CompanyProfile[] {
  return companies.filter((company, index) => companies.findIndex((candidate) =>
    candidate.nodeId === company.nodeId
      || Boolean(company.code && candidate.code === company.code)
      || normalizeSearchValue(candidate.label) === normalizeSearchValue(company.label),
  ) === index);
}

function enrichCompanyProfile(company: CompanyProfile): CompanyProfile {
  const entity = resolveCompanyEntity(company.label) ?? resolveCompanyEntity(company.keyword);
  return entity ? { ...company, code: entity.code, industry: entity.industry, market: entity.market } : company;
}

function entityToProfile(entity: { label: string; nodeId: string; code?: string; industry?: string; market?: string }): CompanyProfile {
  return { keyword: entity.label, label: entity.label, nodeId: entity.nodeId, code: entity.code, industry: entity.industry, market: entity.market };
}

function scenarioForIndustry(industry?: string): LiveNewsScenario {
  if (industry?.match(/반도체|메모리|서버|클라우드|소프트웨어|IT|인터넷/)) return "semiconductor";
  if (industry?.match(/배터리|자동차|전기차|전자/)) return "battery";
  return "governance";
}

function inferSentiment(text: string): NewsSignal["sentiment"] {
  if (/(중단|취소|하락|적자|리콜|제재|위기|급락|차질|소송)/.test(text)) return "부정";
  if (/(증가|성장|수주|최대|개선|흑자|확대|돌파|개발 완료)/.test(text)) return "긍정";
  if (/(우려|검토|변동|전망|경고|불확실)/.test(text)) return "주의";
  return "중립";
}

function validDate(value: string): Date | null {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatKoreanDate(date: Date): string {
  return new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function relativeTime(date: Date, now: Date): string {
  const minutes = Math.max(0, Math.floor((now.getTime() - date.getTime()) / 60_000));
  if (minutes < 1) return "방금 전";
  if (minutes < 60) return `${minutes}분 전`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}시간 전`;
  return `${Math.floor(hours / 24)}일 전`;
}

function compactTitle(value: string): string {
  return value.length > 80 ? `${value.slice(0, 77)}…` : value;
}

function stableHash(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function normalizeSearchValue(value: string): string {
  return value.toLocaleLowerCase("ko-KR").replace(/[^a-z0-9가-힣]/g, "");
}

function boundedSearchInteger(value: number | undefined, fallback: number, min: number, max: number): number {
  if (!Number.isInteger(value)) return fallback;
  return Math.min(max, Math.max(min, value!));
}

function sourceFromUrl(value: string): string {
  try {
    return new URL(value).hostname.replace(/^www\./, "");
  } catch {
    return "NAVER News";
  }
}
