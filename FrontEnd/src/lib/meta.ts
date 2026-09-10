import type { ImpactDirection, Market, RelationshipType, Sentiment } from "@/api/types";
import type { CurrencyCode } from "@/lib/format";

/* ------------------------------ 산업 팔레트 ------------------------------ */
export const INDUSTRY_COLORS: Record<string, string> = {
  반도체: "#5FB8FF",
  "2차전지": "#4FE8C0",
  자동차: "#FFB457",
  "인터넷·플랫폼": "#B98CFF",
  "바이오·헬스케어": "#FF7FA4",
  "에너지·중공업": "#FFE27A",
  "소프트웨어·AI": "#5CE1E6",
  "금융·지주": "#9DB0FF",
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
/**
 * 지분 관계로 취급할 유형 코드. relationship_type 시드가 아직 없어 코드가 바뀔 수 있으므로
 * 화면(투자·지분 트리)이 문자열을 직접 비교하지 않고 이 배열만 참조하게 한다.
 */
export const OWNERSHIP_TYPES: RelationshipType[] = ["INVEST"];

/** name 은 서버가 relationship_type.name 을 응답에 실어 주면 하드코딩 라벨 대신 쓰기 위한 자리다. */
export function relationshipMeta(code: RelationshipType, name?: string | null): RelationshipMeta {
  const base =
    RELATIONSHIP_META[code] ??
    ({
      code,
      label: String(code),
      color: "#AAB3C2",
      directed: false,
      description: "정의되지 않은 관계 유형",
    } satisfies RelationshipMeta);
  return name ? { ...base, label: name } : base;
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
/** 시장별 거래 통화. 미국·일본·중국 상장 종목을 원화로 표기하지 않도록 시장 코드를 전부 적는다. */
const MARKET_CURRENCY: Record<string, CurrencyCode> = {
  KOSPI: "KRW",
  KOSDAQ: "KRW",
  NASDAQ: "USD",
  NYSE: "USD",
  AMEX: "USD",
  TSE: "JPY",
  SSE: "CNY",
  SZSE: "CNY",
};
export function marketCurrency(market: Market): CurrencyCode {
  return MARKET_CURRENCY[market] ?? "KRW";
}

/** 점수(0~100)를 라벨로 */
export function scoreBand(score: number) {
  if (score >= 80) return "매우 강함";
  if (score >= 60) return "강함";
  if (score >= 40) return "보통";
  if (score >= 20) return "약함";
  return "미약";
}
