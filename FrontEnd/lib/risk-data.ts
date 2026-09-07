import { newsSignals, type NewsSignal } from "./news-data";

export type NodeKind = "company" | "shareholder" | "industry" | "material" | "event";
export type RelationKind =
  | "SUPPLIES_TO"
  | "OWNS_STAKE"
  | "USES_MATERIAL"
  | "BELONGS_TO"
  | "SHORTAGE_RISK"
  | "NEWS_MENTIONS"
  | "EXTRACTED_RELATION"
  | "MODEL_EXPOSURE";
export type DataBasis = "disclosed" | "reported" | "modeled";

export type RiskNode = {
  id: string;
  label: string;
  kind: NodeKind;
  code?: string;
  industry?: string;
  marketCap?: string;
  rating?: string;
  per?: number;
  pbr?: number;
  country?: string;
  description: string;
  sourceUrl?: string;
  sourceLabel?: string;
  dataAsOf?: string;
  x: number;
  y: number;
};

export type RiskEdge = {
  id: string;
  source: string;
  target: string;
  relation: RelationKind;
  weight: number;
  label: string;
  evidence: string;
  basis: DataBasis;
  sourceUrl?: string;
  sourceLabel?: string;
  dataAsOf?: string;
};

export type Scenario = {
  id: string;
  shortLabel: string;
  title: string;
  subtitle: string;
  severity: "CRITICAL" | "HIGH" | "ELEVATED";
  severityScore: number;
  shockSource: string;
  nodes: RiskNode[];
  edges: RiskEdge[];
  estimatedMarketImpact: number;
  exposedValue: string;
  maxDepth: number;
  dataAsOf: string;
  news: NewsSignal[];
};

const company = (
  id: string,
  label: string,
  code: string,
  industry: string,
  x: number,
  y: number,
  description: string,
  sourceUrl?: string,
  sourceLabel?: string,
  dataAsOf?: string,
): RiskNode => ({ id, label, code, industry, x, y, description, sourceUrl, sourceLabel, dataAsOf, marketCap: "미수집", rating: "미수집", kind: "company" });

const modeled = (id: string, source: string, target: string, relation: RelationKind, weight: number, label: string): RiskEdge => ({
  id, source, target, relation, weight, label, basis: "modeled", evidence: "산업 연결을 바탕으로 한 프로토타입 전이 가중치 · 공시값 아님",
});

export const scenarios: Scenario[] = [
  {
    id: "battery",
    shortLabel: "배터리 계약 충격",
    title: "배터리 수요·계약 충격 시나리오",
    subtitle: "LG에너지솔루션 대형 공급계약 종료 보도 · 실제 지분 구조와 모델 전이 결합",
    severity: "HIGH",
    severityScore: 88,
    shockSource: "contract-shock",
    estimatedMarketImpact: -3.9,
    exposedValue: "모델 ₩15.2조",
    maxDepth: 3,
    dataAsOf: "뉴스 2025.12.26 · 지분 2025 사업보고서/공식 발표 기준",
    nodes: [
      { id: "contract-shock", label: "EV 계약 취소", kind: "event", country: "KR/US/DE", description: "Ford·Freudenberg 배터리 계약 종료를 묶은 관측 이벤트", sourceUrl: newsSignals.lgesFordCancellation.sourceUrl, sourceLabel: "Reuters 보도", dataAsOf: "2025.12.26", x: .07, y: .46 },
      company("lgensol", "LG에너지솔루션", "373220", "배터리 셀", .31, .46, "대형 EV 배터리 공급계약 취소의 직접 당사자", "https://www.lgensol.com/kr/investors/data-business-report", "LG에너지솔루션 2025 사업보고서", "2025.12.31"),
      company("ford", "Ford", "F", "완성차", .55, .16, "2025년 12월 LG에너지솔루션 공급계약 종료 당사자", newsSignals.lgesFordCancellation.sourceUrl, "Reuters 보도", "2025.12.17"),
      company("freudenberg", "Freudenberg", "비상장", "산업재·배터리", .55, .76, "2025년 12월 LG에너지솔루션 주문 취소 당사자", newsSignals.lgesFreudenbergCancellation.sourceUrl, "Reuters 보도", "2025.12.26"),
      company("posco", "포스코퓨처엠", "003670", "배터리 소재", .58, .46, "양극재·음극재 생산사, 셀 수요 변화의 모델상 간접 노출", newsSignals.poscoRightsIssue.sourceUrl, "POSCO FUTURE M 공식 발표", "2025.05.28"),
      company("poscoholdings", "포스코홀딩스", "005490", "지주·철강", .82, .18, "포스코퓨처엠 최대주주, 59.7% 지분에 배정된 유상증자 참여", newsSignals.poscoRightsIssue.sourceUrl, "POSCO FUTURE M 공식 발표", "2025.05.28"),
      company("lgchem", "LG화학", "051910", "화학·배터리 소재", .10, .77, "LG에너지솔루션 최대주주", "https://www.lgensol.com/upload/file/download/2025_LGES_Business_Report.pdf", "LG에너지솔루션 2025 사업보고서", "2025.12.31"),
      company("hyundai", "현대자동차", "005380", "완성차", .82, .53, "배터리 셀 수급 변화의 모델상 전방 노출 기업"),
      company("kia", "기아", "000270", "완성차", .85, .80, "배터리 셀 수급 변화의 모델상 전방 노출 기업"),
    ],
    edges: [
      { id: "contract-lg", source: "contract-shock", target: "lgensol", relation: "SHORTAGE_RISK", weight: .94, label: "모델 충격 0.94", evidence: "Ford·Freudenberg 계약 종료 보도에서 생성한 이벤트 엣지", basis: "modeled", sourceUrl: newsSignals.lgesFordCancellation.sourceUrl, sourceLabel: "Reuters", dataAsOf: "2025.12.26" },
      { id: "contract-ford", source: "contract-shock", target: "ford", relation: "SHORTAGE_RISK", weight: .72, label: "모델 충격 0.72", evidence: "계약 종료 당사자 관계 · 강도는 모델 추정", basis: "modeled", sourceUrl: newsSignals.lgesFordCancellation.sourceUrl, sourceLabel: "Reuters", dataAsOf: "2025.12.17" },
      { id: "contract-freudenberg", source: "contract-shock", target: "freudenberg", relation: "SHORTAGE_RISK", weight: .64, label: "모델 충격 0.64", evidence: "주문 취소 당사자 관계 · 강도는 모델 추정", basis: "modeled", sourceUrl: newsSignals.lgesFreudenbergCancellation.sourceUrl, sourceLabel: "Reuters", dataAsOf: "2025.12.26" },
      { id: "lg-ford", source: "lgensol", target: "ford", relation: "SUPPLIES_TO", weight: .78, label: "계약 관계 · 종료", evidence: "실제 공급계약 관계, 전이 가중치는 모델 추정", basis: "reported", sourceUrl: newsSignals.lgesFordCancellation.sourceUrl, sourceLabel: "Reuters", dataAsOf: "2025.12.17" },
      { id: "lg-freudenberg", source: "lgensol", target: "freudenberg", relation: "SUPPLIES_TO", weight: .63, label: "계약 관계 · 취소", evidence: "실제 공급계약 관계, 전이 가중치는 모델 추정", basis: "reported", sourceUrl: newsSignals.lgesFreudenbergCancellation.sourceUrl, sourceLabel: "Reuters", dataAsOf: "2025.12.26" },
      { id: "lgchem-lg", source: "lgchem", target: "lgensol", relation: "OWNS_STAKE", weight: .8184, label: "지분 81.84%", evidence: "LG에너지솔루션 사업보고서 주주현황", basis: "disclosed", sourceUrl: "https://www.lgensol.com/upload/file/download/2025_LGES_Business_Report.pdf", sourceLabel: "2025 사업보고서", dataAsOf: "2025.12.31" },
      { id: "holdings-posco", source: "poscoholdings", target: "posco", relation: "OWNS_STAKE", weight: .597, label: "지분 59.7%", evidence: "포스코퓨처엠 공식 유상증자 발표", basis: "disclosed", sourceUrl: newsSignals.poscoRightsIssue.sourceUrl, sourceLabel: "POSCO FUTURE M", dataAsOf: "2025.05.28" },
      modeled("posco-lg", "posco", "lgensol", "SUPPLIES_TO", .62, "모델 가중치 0.62"),
      modeled("lg-hyundai", "lgensol", "hyundai", "SUPPLIES_TO", .49, "모델 가중치 0.49"),
      modeled("lg-kia", "lgensol", "kia", "SUPPLIES_TO", .42, "모델 가중치 0.42"),
    ],
    news: [newsSignals.lgesFordCancellation, newsSignals.lgesFreudenbergCancellation, newsSignals.poscoRightsIssue],
  },
  {
    id: "semiconductor",
    shortLabel: "AI 메모리 집중",
    title: "AI 메모리 공급 집중 시나리오",
    subtitle: "HBM4 양산·대규모 수요 확대 · 실제 기업 발표와 주요주주 데이터 기반",
    severity: "HIGH",
    severityScore: 84,
    shockSource: "ai-demand",
    estimatedMarketImpact: -2.7,
    exposedValue: "모델 ₩11.8조",
    maxDepth: 3,
    dataAsOf: "뉴스 2026.01.29 · 지분 2026.05.06 기준",
    nodes: [
      { id: "ai-demand", label: "AI 메모리 수요", kind: "event", country: "GLOBAL", description: "HBM4 양산과 2026년 고객 수요 확대를 결합한 관측 이벤트", sourceUrl: newsSignals.skHynixEarnings.sourceUrl, sourceLabel: "SK hynix Newsroom", dataAsOf: "2026.01.29", x: .07, y: .46 },
      company("skhynix", "SK하이닉스", "000660", "메모리 반도체", .30, .27, "HBM4 개발 완료와 양산 준비를 발표한 AI 메모리 공급사", newsSignals.skHynixHbm4.sourceUrl, "SK hynix Newsroom", "2025.09.12"),
      company("samsung", "삼성전자", "005930", "반도체·전자", .30, .70, "2026년 1분기 HBM4 공급 계획을 발표한 메모리 공급사", newsSignals.samsungResults.sourceUrl, "Samsung Global Newsroom", "2026.01.29"),
      company("asml", "ASML", "ASML", "반도체 장비", .54, .12, "첨단 DRAM·HBM 생산에 필요한 EUV 장비 공급사", "https://www.tomshardware.com/tech-industry/semiconductors/sk-hynix-places-record-8-billion-order-for-asml-euv-lithography-machines", "Reuters 인용 보도", "2026.03.24"),
      company("sksquare", "SK스퀘어", "402340", "투자", .54, .37, "SK하이닉스 주요주주 20.5%", "https://m.skhynix.com/ir/UI-FR-IR04/", "SK hynix Ownership Structure", "2026.05.06"),
      company("hanmi", "한미반도체", "042700", "반도체 장비", .55, .72, "HBM 후공정 투자 사이클의 모델상 노출 기업"),
      company("naver", "NAVER", "035420", "인터넷·AI", .82, .28, "AI 인프라 조달을 통한 모델상 전방 노출 기업"),
      company("mobis2", "현대모비스", "012330", "자동차부품", .82, .72, "차량용 반도체 조달을 통한 모델상 전방 노출 기업"),
    ],
    edges: [
      modeled("demand-hynix", "ai-demand", "skhynix", "SHORTAGE_RISK", .92, "수요 집중 0.92"),
      modeled("demand-samsung", "ai-demand", "samsung", "SHORTAGE_RISK", .79, "수요 집중 0.79"),
      modeled("demand-asml", "ai-demand", "asml", "SHORTAGE_RISK", .66, "장비 수요 0.66"),
      { id: "sksquare-hynix", source: "sksquare", target: "skhynix", relation: "OWNS_STAKE", weight: .205, label: "지분 20.5%", evidence: "SK하이닉스 공식 Ownership Structure", basis: "disclosed", sourceUrl: "https://m.skhynix.com/ir/UI-FR-IR04/", sourceLabel: "SK hynix IR", dataAsOf: "2026.05.06" },
      { id: "asml-hynix", source: "asml", target: "skhynix", relation: "SUPPLIES_TO", weight: .71, label: "EUV 주문 · 모델 0.71", evidence: "2026년 3월 공개된 ASML EUV 장비 주문 보도 · 전이 강도는 모델 추정", basis: "reported", sourceUrl: "https://www.tomshardware.com/tech-industry/semiconductors/sk-hynix-places-record-8-billion-order-for-asml-euv-lithography-machines", sourceLabel: "Reuters 인용 보도", dataAsOf: "2026.03.24" },
      modeled("hynix-ai", "skhynix", "naver", "SUPPLIES_TO", .58, "모델 가중치 0.58"),
      modeled("samsung-ai", "samsung", "naver", "SUPPLIES_TO", .47, "모델 가중치 0.47"),
      modeled("hynix-hanmi", "skhynix", "hanmi", "SUPPLIES_TO", .44, "CAPEX 모델 0.44"),
      modeled("samsung-mobis", "samsung", "mobis2", "SUPPLIES_TO", .39, "모델 가중치 0.39"),
    ],
    news: [newsSignals.skHynixHbm4, newsSignals.skHynixEarnings, newsSignals.samsungResults],
  },
  {
    id: "governance",
    shortLabel: "현대차그룹 지분",
    title: "현대차그룹 순환 지분 구조",
    subtitle: "현대모비스 → 현대자동차 → 기아 → 현대모비스 · 공식 IR 기준 지분율",
    severity: "ELEVATED",
    severityScore: 72,
    shockSource: "mobis-g",
    estimatedMarketImpact: -2.1,
    exposedValue: "모델 ₩6.4조",
    maxDepth: 4,
    dataAsOf: "현대모비스 2025.12.31 · 현대자동차 2025.03.31 · 기아 최신 IR",
    nodes: [
      company("mobis-g", "현대모비스", "012330", "자동차부품", .13, .48, "현대자동차 21.86%를 보유하고 기아 등 특수관계인이 32.70%를 보유하는 연결축", "https://www.mobis.com/en/ir/irinfo.do", "현대모비스 IR", "2025.12.31"),
      company("hyundai-g", "현대자동차", "005380", "완성차", .38, .22, "현대모비스가 21.86%를 보유한 그룹 핵심 완성차 회사", "https://www.hyundai.com/content/dam/hyundai/ww/en/images/company/investor-relations/financial-Information/report-en/2025/2025-q1-consolidated-audit-report-en.pdf", "현대자동차 1Q25 감사보고서", "2025.03.31"),
      company("kia-g", "기아", "000270", "완성차", .64, .46, "현대자동차가 35.17%를 보유하고 현대모비스 18.10%를 보유", "https://worldwide.kia.com/en/company/sustainability/governance/shareholders", "Kia Shareholders", "최신 IR"),
      company("steel-g", "현대제철", "004020", "철강", .38, .76, "현대모비스 지분 6.07% 보유", "https://www.mobis.com/en/ir/irinfo.do", "현대모비스 IR", "2025.12.31"),
      company("glovis-g", "현대글로비스", "086280", "물류", .68, .78, "현대모비스 지분 0.72% 보유", "https://www.mobis.com/en/ir/irinfo.do", "현대모비스 IR", "2025.12.31"),
      { id: "cmk", label: "정몽구", kind: "shareholder", country: "KR", description: "현대모비스 7.47%, 현대자동차 5.44% 보유", sourceUrl: "https://www.mobis.com/en/ir/irinfo.do", sourceLabel: "현대모비스 IR", dataAsOf: "2025.12.31", x: .88, y: .17 },
      { id: "ec", label: "정의선", kind: "shareholder", country: "KR", description: "현대모비스 0.33% 보유", sourceUrl: "https://www.mobis.com/en/ir/irinfo.do", sourceLabel: "현대모비스 IR", dataAsOf: "2025.12.31", x: .89, y: .48 },
      { id: "treasury", label: "자기주식", kind: "shareholder", country: "KR", description: "현대모비스 자기주식 1,555,800주", sourceUrl: "https://www.mobis.com/en/ir/irinfo.do", sourceLabel: "현대모비스 IR", dataAsOf: "2025.12.31", x: .88, y: .80 },
    ],
    edges: [
      { id: "mobis-hyundai", source: "mobis-g", target: "hyundai-g", relation: "OWNS_STAKE", weight: .2186, label: "지분 21.86%", evidence: "현대자동차 2025년 1분기 연결감사보고서", basis: "disclosed", sourceUrl: "https://www.hyundai.com/content/dam/hyundai/ww/en/images/company/investor-relations/financial-Information/report-en/2025/2025-q1-consolidated-audit-report-en.pdf", sourceLabel: "Hyundai Motor Audit Report", dataAsOf: "2025.03.31" },
      { id: "hyundai-kia", source: "hyundai-g", target: "kia-g", relation: "OWNS_STAKE", weight: .3517, label: "지분 35.17%", evidence: "기아 공식 주주현황", basis: "disclosed", sourceUrl: "https://worldwide.kia.com/en/company/sustainability/governance/shareholders", sourceLabel: "Kia Shareholders", dataAsOf: "최신 IR" },
      { id: "kia-mobis", source: "kia-g", target: "mobis-g", relation: "OWNS_STAKE", weight: .181, label: "지분 18.10%", evidence: "현대모비스 공식 주주현황", basis: "disclosed", sourceUrl: "https://www.mobis.com/en/ir/irinfo.do", sourceLabel: "Hyundai Mobis IR", dataAsOf: "2025.12.31" },
      { id: "steel-mobis", source: "steel-g", target: "mobis-g", relation: "OWNS_STAKE", weight: .0607, label: "지분 6.07%", evidence: "현대모비스 공식 주주현황", basis: "disclosed", sourceUrl: "https://www.mobis.com/en/ir/irinfo.do", sourceLabel: "Hyundai Mobis IR", dataAsOf: "2025.12.31" },
      { id: "glovis-mobis", source: "glovis-g", target: "mobis-g", relation: "OWNS_STAKE", weight: .0072, label: "지분 0.72%", evidence: "현대모비스 공식 주주현황", basis: "disclosed", sourceUrl: "https://www.mobis.com/en/ir/irinfo.do", sourceLabel: "Hyundai Mobis IR", dataAsOf: "2025.12.31" },
      { id: "cmk-mobis", source: "cmk", target: "mobis-g", relation: "OWNS_STAKE", weight: .0747, label: "지분 7.47%", evidence: "현대모비스 공식 주주현황", basis: "disclosed", sourceUrl: "https://www.mobis.com/en/ir/irinfo.do", sourceLabel: "Hyundai Mobis IR", dataAsOf: "2025.12.31" },
      { id: "ec-mobis", source: "ec", target: "mobis-g", relation: "OWNS_STAKE", weight: .0033, label: "지분 0.33%", evidence: "현대모비스 공식 주주현황", basis: "disclosed", sourceUrl: "https://www.mobis.com/en/ir/irinfo.do", sourceLabel: "Hyundai Mobis IR", dataAsOf: "2025.12.31" },
      { id: "cmk-hyundai", source: "cmk", target: "hyundai-g", relation: "OWNS_STAKE", weight: .0544, label: "지분 5.44%", evidence: "현대자동차 2025년 1분기 연결감사보고서", basis: "disclosed", sourceUrl: "https://www.hyundai.com/content/dam/hyundai/ww/en/images/company/investor-relations/financial-Information/report-en/2025/2025-q1-consolidated-audit-report-en.pdf", sourceLabel: "Hyundai Motor Audit Report", dataAsOf: "2025.03.31" },
    ],
    news: [newsSignals.mobisOwnership, newsSignals.hyundaiResults, newsSignals.kiaOwnership],
  },
];

export const relationLabels: Record<RelationKind, string> = {
  SUPPLIES_TO: "공급",
  OWNS_STAKE: "지분",
  USES_MATERIAL: "원자재",
  BELONGS_TO: "산업",
  SHORTAGE_RISK: "이벤트",
};
