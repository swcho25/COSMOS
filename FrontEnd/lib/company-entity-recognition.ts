import { foreignCompanyAliases } from "./foreign-company-aliases";
import { krxListedCompanies } from "./krx-company-registry.generated";
import { secListedCompanies } from "./sec-company-registry.generated";

export type RecognizedCompanyEntity = {
  label: string;
  nodeId: string;
  code?: string;
  industry?: string;
  market?: string;
  matchedAlias: string;
  start: number;
  end: number;
};

type CompanyCandidate = Omit<RecognizedCompanyEntity, "matchedAlias" | "start" | "end"> & { aliases: string[] };
type AliasCandidate = { company: CompanyCandidate; alias: string };

const koreanParticles = ["으로부터", "에게서", "에서는", "으로는", "이라는", "이라고", "에서", "에게", "부터", "까지", "보다", "처럼", "으로", "라고", "이라", "은", "는", "이", "가", "을", "를", "의", "과", "와", "에", "로", "도", "만"];
const corporateSuffixes = ["코리아", "그룹", "홀딩스", "테크놀로지", "테크놀로지스", "전자", "반도체", "모터스", "측"];
const ambiguousEnglishAliases = new Set(["apple", "amazon", "alphabet", "meta", "target", "block", "gap", "visa", "shell", "oracle", "zoom", "snap", "unity", "affirm", "ford", "lucid"]);
const ambiguousKoreanAliases = new Set(["태양", "대상", "미래", "우리", "동방", "동양", "동일", "선진", "메타", "애플"]);
const strongCompanyAction = /(?:주가|주식|종목|시가총액|시총|매출|영업이익|실적|공시|상장|지분|인수|합병|계약|수주|공급|생산|출시|투자유치|증자|감자|배당|대표이사|CEO|stock|shares?|revenue|earnings?|acquisition|merger|contract|announc(?:e|ed|es)|launch(?:ed|es)?|unveil(?:ed|s)?)/i;

const domesticCandidates: CompanyCandidate[] = krxListedCompanies.map((company) => ({
  label: company.name,
  nodeId: `krx-${company.code}`,
  code: company.code,
  industry: company.industry,
  market: company.market,
  aliases: [company.name],
}));

const secByTicker = new Map<string, (typeof secListedCompanies)[number]>();
secListedCompanies.forEach((company) => company.tickers.forEach((ticker) => secByTicker.set(ticker.toUpperCase(), company)));

const curatedByCik = new Map<string, (typeof foreignCompanyAliases)[number][]>();
const curatedStandalone: CompanyCandidate[] = [];
foreignCompanyAliases.forEach((entry) => {
  const sec = entry.ticker ? secByTicker.get(entry.ticker.toUpperCase()) : undefined;
  if (!sec) {
    curatedStandalone.push({
      label: entry.label,
      nodeId: `foreign-${slugId(entry.label)}`,
      code: entry.ticker,
      industry: entry.industry,
      market: entry.market,
      aliases: entry.aliases,
    });
    return;
  }
  curatedByCik.set(sec.cik, [...(curatedByCik.get(sec.cik) ?? []), entry]);
});

const foreignCandidates: CompanyCandidate[] = [
  ...secListedCompanies.map((company) => {
    const curated = curatedByCik.get(company.cik) ?? [];
    const preferred = curated[0];
    return {
      label: preferred?.label ?? company.aliases.at(-1) ?? company.name,
      nodeId: `sec-${company.cik}`,
      code: preferred?.ticker ?? company.tickers[0],
      industry: preferred?.industry ?? "해외 상장사",
      market: preferred?.market ?? company.exchanges[0] ?? "SEC filer",
      aliases: uniqueStrings([...company.aliases, ...curated.flatMap((entry) => entry.aliases)]),
    };
  }),
  ...curatedStandalone,
];

const allCandidates = dedupeCompanies([...domesticCandidates, ...foreignCandidates]);
const curatedAliasKeys = new Set(foreignCompanyAliases.flatMap((company) => company.aliases.map(normalizeEntityName)));
const aliasCandidates: AliasCandidate[] = allCandidates
  .flatMap((company) => company.aliases.map((alias) => ({ company, alias: alias.trim() })).filter(({ alias }) => isUsefulAlias(alias)))
  .sort((a, b) => normalizeEntityName(b.alias).length - normalizeEntityName(a.alias).length || a.alias.localeCompare(b.alias, "ko"));

const entityByExactName = new Map<string, CompanyCandidate>();
allCandidates.forEach((company) => {
  [company.label, company.code, ...company.aliases].filter(Boolean).forEach((value) => {
    const key = normalizeEntityName(String(value));
    if (key && !entityByExactName.has(key)) entityByExactName.set(key, company);
  });
});

export const companyRegistryStats = {
  domesticCount: domesticCandidates.length,
  foreignCount: foreignCandidates.length,
  foreignTickerAssociations: secListedCompanies.reduce((total, company) => total + company.tickers.length, 0),
};

export function recognizeCompanyEntities(text: string): RecognizedCompanyEntity[] {
  const occupied: Array<{ start: number; end: number }> = [];
  const results: RecognizedCompanyEntity[] = [];

  aliasCandidates.forEach(({ company, alias }) => {
    const mention = findAliasMention(text, alias);
    if (!mention || occupied.some((range) => mention.start < range.end && mention.end > range.start)) return;
    occupied.push(mention);
    results.push({ ...company, matchedAlias: alias, ...mention });
  });

  return dedupeRecognized(results.sort((a, b) => a.start - b.start));
}

export function resolveCompanyEntity(value: string): CompanyCandidate | null {
  return entityByExactName.get(normalizeEntityName(value)) ?? null;
}

export function hasCompanyNameMention(text: string, values: string[]): boolean {
  return values.some((value) => Boolean(value && findAliasMention(text, value)));
}

function findAliasMention(text: string, alias: string): { start: number; end: number } | null {
  const haystack = text.toLocaleLowerCase("ko-KR");
  const needle = alias.toLocaleLowerCase("ko-KR");
  let from = 0;
  while (from <= haystack.length - needle.length) {
    const start = haystack.indexOf(needle, from);
    if (start < 0) return null;
    const end = start + needle.length;
    if (hasValidBoundaries(text, start, end, alias) && hasRequiredContext(text, start, end, alias)) return { start, end };
    from = start + 1;
  }
  return null;
}

function hasValidBoundaries(text: string, start: number, end: number, alias: string): boolean {
  const before = text[start - 1] ?? "";
  const after = text[end] ?? "";
  const containsKorean = /[가-힣]/.test(alias);
  if (containsKorean) {
    if (/[가-힣a-z0-9]/i.test(before)) return false;
    if (!after || !/[가-힣a-z0-9]/i.test(after)) return true;
    const tail = text.slice(end);
    return [...koreanParticles, ...corporateSuffixes].some((suffix) => {
      if (!tail.startsWith(suffix)) return false;
      const next = tail[suffix.length] ?? "";
      return !next || !/[가-힣a-z0-9]/i.test(next);
    });
  }
  return !/[a-z0-9]/i.test(before) && !/[a-z0-9]/i.test(after);
}

function hasRequiredContext(text: string, start: number, end: number, alias: string): boolean {
  const normalized = normalizeEntityName(alias);
  const ambiguous = ambiguousKoreanAliases.has(alias) || ambiguousEnglishAliases.has(normalized) || (/^[a-z]+$/i.test(alias) && normalized.length <= 5);
  if (!ambiguous) return true;
  const before = text.slice(Math.max(0, start - 36), start);
  const after = text.slice(end, Math.min(text.length, end + 56));
  const window = `${before}${alias}${after}`;
  const explicitCompanyForm = /(?:\(주\)|㈜|상장사|상장기업|종목|법인)\s*$/i.test(before)
    || /^(?:은|는|이|가|의|측|그룹|홀딩스|코리아|,|\s){0,8}(?:대표이사|대표|회장|CEO|측)/i.test(after);
  const tickerForm = /(?:\$[A-Z]{1,6}|(?:NASDAQ|NYSE|나스닥|뉴욕증시)\s*[:：]?\s*[A-Z]{1,6}|\([A-Z]{1,6}\))/i.test(window);
  const actionAfterName = /^(?:은|는|이|가|을|를|의|과|와|에|에서|으로|로|도|만|측|,|\s){0,12}.{0,38}/.exec(after)?.[0] ?? "";
  return explicitCompanyForm || tickerForm || strongCompanyAction.test(actionAfterName);
}

function isUsefulAlias(alias: string): boolean {
  const normalized = normalizeEntityName(alias);
  if (/[가-힣]/.test(alias)) return normalized.length >= 2;
  if (normalized.length <= 3 && !curatedAliasKeys.has(normalized)) return false;
  if (/^[a-z]{1,3}$/i.test(alias) && alias !== alias.toUpperCase()) return false;
  return true;
}

function dedupeCompanies(companies: CompanyCandidate[]): CompanyCandidate[] {
  const byId = new Map<string, CompanyCandidate>();
  companies.forEach((company) => {
    const existing = byId.get(company.nodeId);
    byId.set(company.nodeId, existing ? { ...existing, aliases: uniqueStrings([...existing.aliases, ...company.aliases]) } : { ...company, aliases: uniqueStrings(company.aliases) });
  });
  return [...byId.values()];
}

function dedupeRecognized(companies: RecognizedCompanyEntity[]): RecognizedCompanyEntity[] {
  return companies.filter((company, index) => companies.findIndex((candidate) =>
    candidate.nodeId === company.nodeId || Boolean(company.code && candidate.code === company.code),
  ) === index);
}

function uniqueStrings(values: string[]): string[] {
  return values.filter((value, index) => value && values.findIndex((candidate) => normalizeEntityName(candidate) === normalizeEntityName(value)) === index);
}

function normalizeEntityName(value: string): string {
  return value.toLocaleLowerCase("ko-KR").replace(/[^a-z0-9가-힣]/g, "");
}

function slugId(value: string): string {
  const normalized = normalizeEntityName(value);
  let hash = 2166136261;
  for (let index = 0; index < normalized.length; index += 1) {
    hash ^= normalized.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}
