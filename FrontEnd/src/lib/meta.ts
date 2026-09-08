import type { ImpactDirection, Market, RelationshipType, Sentiment } from "@/api/types";

/* ------------------------------ 산업 팔레트 ------------------------------ */
export const INDUSTRY_COLORS: Record<string, string> = {
  반도체: "#5FB8FF",
  "2차전지": "#4FE8C0",
  자동차: "#FFB457",
  "인터넷·플랫폼": "#B98CFF",
  "바이오·헬스케어": "#FF7FA4",
  "에너지·중공업": "#FFE27A",
  "소프트웨어·AI": "#5CE1E6",
  금융: "#9DB0FF",
  "소비재·유통": "#B6F26B",
  "통신·미디어": "#E58CFF",
  "철강·화학·소재": "#FF7A6B",
};
const FALLBACK_COLORS = ["#7FD3FF", "#FFD27F", "#8CFFC9", "#FF9FCF", "#C0C8FF"];

export function industryColor(name: string | undefined | null) {
  if (!name) return "#AAB3C2";
  if (INDUSTRY_COLORS[name]) return INDUSTRY_COLORS[name];
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return FALLBACK_COLORS[h % FALLBACK_COLORS.length];
}

/* ------------------------------ 관계 유형 ------------------------------ */
export interface RelationshipMeta {
  code: RelationshipType;
  label: string;
  color: string;
  directed: boolean;
  description: string;
}
export const RELATIONSHIP_META: Record<string, RelationshipMeta> = {
  SUPPLY: { code: "SUPPLY", label: "공급", color: "#63D7FF", directed: true, description: "부품·장비·원자재를 공급하는 관계. 화살표 방향으로 충격이 전이됩니다." },
  INVEST: { code: "INVEST", label: "투자·지분", color: "#C6A5FF", directed: true, description: "지분 보유·출자 관계. 지배 방향으로 표시합니다." },
  PARTNER: { code: "PARTNER", label: "협력", color: "#4FE8C0", directed: false, description: "합작·제휴·공동 개발처럼 방향이 없는 협력 관계." },
  COMPETE: { code: "COMPETE", label: "경쟁", color: "#FF8A7A", directed: false, description: "같은 시장에서 점유율·가격을 다투는 관계." },
};
export const RELATIONSHIP_ORDER: RelationshipType[] = ["SUPPLY", "INVEST", "PARTNER", "COMPETE"];

export function relationshipMeta(code: RelationshipType): RelationshipMeta {
  return (
    RELATIONSHIP_META[code] ?? {
      code,
      label: String(code),
      color: "#AAB3C2",
      directed: false,
      description: "정의되지 않은 관계 유형",
    }
  );
}

/* ------------------------------ 감성 / 영향 방향 ------------------------------ */
export interface ToneMeta {
  label: string;
  color: string;
  fg: string;
  bg: string;
}
export const SENTIMENT_META: Record<Sentiment, ToneMeta> = {
  POSITIVE: { label: "긍정", color: "#2AC769", fg: "#5BE08C", bg: "rgba(42,199,105,.13)" },
  NEGATIVE: { label: "부정", color: "#FF5C7A", fg: "#FF8FA3", bg: "rgba(255,92,122,.14)" },
  NEUTRAL: { label: "중립", color: "#8B95A1", fg: "#AAB3C2", bg: "rgba(255,255,255,.06)" },
};
export const IMPACT_META: Record<ImpactDirection, ToneMeta> = {
  POSITIVE: { label: "긍정 전이", color: "#2AC769", fg: "#5BE08C", bg: "rgba(42,199,105,.13)" },
  NEGATIVE: { label: "부정 전이", color: "#FF5C7A", fg: "#FF8FA3", bg: "rgba(255,92,122,.14)" },
  NEUTRAL: { label: "중립", color: "#8B95A1", fg: "#AAB3C2", bg: "rgba(255,255,255,.06)" },
};
export const SENTIMENTS: Sentiment[] = ["POSITIVE", "NEGATIVE", "NEUTRAL"];

export function marketLabel(market: Market) {
  if (market === "KOSPI" || market === "KOSDAQ") return "KRX";
  if (market === "NASDAQ") return "NASDAQ";
  return market;
}
export function marketCurrency(market: Market) {
  return market === "NASDAQ" ? "USD" : "KRW";
}

/** 점수(0~100)를 라벨로 */
export function scoreBand(score: number) {
  if (score >= 80) return "매우 강함";
  if (score >= 60) return "강함";
  if (score >= 40) return "보통";
  if (score >= 20) return "약함";
  return "미약";
}
