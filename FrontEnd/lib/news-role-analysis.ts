import type { NewsSignal } from "./news-data";

export const NEWS_ROLE_MODEL_ID = "onnx-community/Qwen3-0.6B-Instruct-ONNX";

export type CompanyNewsRole = "affected" | "source" | "mentioned";
export type CompanyRole = {
  name: string;
  role: CompanyNewsRole;
  confidence: number;
  reason?: string;
};
export type CompanyRoleAnalysis = {
  provider: "local-qwen3-webgpu" | "target-price-rule";
  modelId: string;
  companies: CompanyRole[];
};

function normalizeCompanyName(value: string) {
  return value.toLocaleLowerCase("ko-KR").replace(/[^a-z0-9가-힣]/g, "");
}

export function buildCompanyRolePrompt(input: Pick<NewsSignal, "title" | "summary"> & { candidates: string[] }) {
  return `/no_think
금융 뉴스에서 각 후보 기업의 문맥상 역할을 판별하세요.

역할 정의:
- affected: 뉴스의 실제 사건·실적·계약·가격·공급망·위험이 발생하거나 영향을 받는 대상 기업
- source: 목표주가, 투자의견, 전망, 리포트, 조사 결과를 제시하거나 인용된 증권사·연구기관·평가기관. 단지 의견을 냈을 뿐이면 영향 기업이 아님
- mentioned: 비교, 배경 설명, 예시로만 등장한 기업

중요 규칙:
1. "A증권, B 목표주가 상향"이면 A증권은 source, B는 affected입니다.
2. "A증권 실적 악화"처럼 증권사 자체가 사건 대상이면 A증권은 affected입니다.
3. 계약·합작·인수의 실제 당사자는 모두 affected일 수 있습니다.
4. 후보를 빠뜨리지 말고 반드시 한 번씩만 분류하세요.
5. confidence에는 0.00~1.00 사이의 실제 확신도를 추정해 넣으세요.

기사 제목: ${input.title}
기사 요약: ${input.summary}
후보 기업: ${JSON.stringify(input.candidates)}

JSON만 출력하세요:
{"companies":[{"name":"후보 기업명 그대로","role":"affected|source|mentioned","confidence":0.95,"reason":"짧은 한국어 근거"}]}`;
}

function parseRole(value: unknown): CompanyNewsRole | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLocaleLowerCase("en-US");
  if (["affected", "target", "impact", "영향대상", "대상"].includes(normalized)) return "affected";
  if (["source", "opinion_source", "analyst", "출처", "분석출처"].includes(normalized)) return "source";
  if (["mentioned", "mention", "background", "단순언급", "배경"].includes(normalized)) return "mentioned";
  return null;
}

function extractGeneratedText(value: unknown): string {
  if (typeof value === "string") return value;
  if (!Array.isArray(value) || !value.length) return "";
  const first = value[0] as { generated_text?: unknown };
  const generated = first?.generated_text;
  if (typeof generated === "string") return generated;
  if (Array.isArray(generated)) {
    const last = generated.at(-1) as { content?: unknown } | undefined;
    return typeof last?.content === "string" ? last.content : "";
  }
  return "";
}

export function parseCompanyRoleOutput(output: unknown, candidates: string[]): CompanyRole[] | null {
  const text = extractGeneratedText(output)
    .replace(/<think>[\s\S]*?<\/think>/gi, "")
    .replace(/```(?:json)?/gi, "")
    .replace(/```/g, "")
    .trim();
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start < 0 || end <= start) return null;

  try {
    const parsed = JSON.parse(text.slice(start, end + 1)) as { companies?: unknown };
    if (!Array.isArray(parsed.companies)) return null;
    const candidateByName = new Map(candidates.map((name) => [normalizeCompanyName(name), name]));
    const roles = parsed.companies.flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const record = item as { name?: unknown; role?: unknown; confidence?: unknown; reason?: unknown };
      if (typeof record.name !== "string") return [];
      const canonicalName = candidateByName.get(normalizeCompanyName(record.name));
      const role = parseRole(record.role);
      if (!canonicalName || !role) return [];
      const rawConfidence = typeof record.confidence === "number" ? record.confidence : Number(record.confidence);
      const confidence = Number.isFinite(rawConfidence) ? Math.min(1, Math.max(0, rawConfidence > 1 ? rawConfidence / 100 : rawConfidence)) : .5;
      return [{ name: canonicalName, role, confidence, reason: typeof record.reason === "string" ? record.reason.slice(0, 100) : undefined }];
    });
    const unique = roles.filter((role, index, all) => all.findIndex((candidate) => normalizeCompanyName(candidate.name) === normalizeCompanyName(role.name)) === index);
    return candidates.every((candidate) => unique.some((role) => normalizeCompanyName(role.name) === normalizeCompanyName(candidate))) ? unique : null;
  } catch {
    return null;
  }
}

const targetPriceLanguage = /(목표\s*주가|목표가|투자\s*의견|매수\s*의견|리포트|애널리스트|연구원|전망치)/i;
const researchCompanyName = /(증권|투자증권|자산운용|리서치|securities|capital markets|research)/i;

export function inferTargetPriceRoles(news: NewsSignal): CompanyRoleAnalysis | null {
  const text = `${news.title} ${news.summary}`;
  if (!targetPriceLanguage.test(text) || news.affected.length < 2) return null;
  const sources = news.affected.filter((company) => researchCompanyName.test(company.label) && text.includes(company.label));
  if (!sources.length || sources.length === news.affected.length) return null;
  const sourceIds = new Set(sources.map((company) => company.nodeId));
  return {
    provider: "target-price-rule",
    modelId: "high-precision-target-price-context-rule",
    companies: news.affected.map((company) => ({
      name: company.label,
      role: sourceIds.has(company.nodeId) ? "source" : "affected",
      confidence: sourceIds.has(company.nodeId) ? .99 : .9,
      reason: sourceIds.has(company.nodeId) ? "목표주가·투자의견을 제시한 분석 출처" : "리포트의 실제 분석 대상 기업",
    })),
  };
}

export function applyCompanyRolesToNews(news: NewsSignal, analysis: CompanyRoleAnalysis | null): NewsSignal {
  if (!analysis) return news;
  const roleByName = new Map(analysis.companies.map((company) => [normalizeCompanyName(company.name), company]));
  if (!news.affected.every((company) => roleByName.has(normalizeCompanyName(company.label)))) return news;

  const affected = news.affected.filter((company) => roleByName.get(normalizeCompanyName(company.label))?.role === "affected");
  if (!affected.length) return news;
  const excludedNames = new Set(analysis.companies.filter((company) => company.role !== "affected").map((company) => normalizeCompanyName(company.name)));
  const relations = news.relations.filter((relation) => !excludedNames.has(normalizeCompanyName(relation.source)) && !excludedNames.has(normalizeCompanyName(relation.target)));

  return {
    ...news,
    affected,
    relations,
    companyRoleAnalysis: analysis,
  };
}

export function companyRoleFor(news: NewsSignal, name: string) {
  return news.companyRoleAnalysis?.companies.find((company) => normalizeCompanyName(company.name) === normalizeCompanyName(name));
}
