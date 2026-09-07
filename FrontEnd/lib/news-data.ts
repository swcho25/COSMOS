export type NewsSignal = {
  id: string;
  scenarioId?: "battery" | "semiconductor" | "governance";
  tag: string;
  title: string;
  source: string;
  sourceUrl: string;
  articleUrl?: string;
  sourceType: "언론 보도" | "기업 발표" | "공식 IR" | "RSS 뉴스" | "NAVER API";
  time: string;
  publishedAt: string;
  publishedAtIso?: string;
  location: string;
  sentiment: "매우 부정" | "부정" | "중립" | "주의" | "긍정";
  confidence: number;
  summary: string;
  paragraphs: string[];
  articleBody?: string;
  articleExtraction?: {
    status: "extracted" | "failed";
    requestedUrl: string;
    finalUrl?: string;
    method?: "json-ld" | "readability" | "naver-selector" | "article-selector";
    charCount?: number;
    wordCount?: number;
    truncated?: boolean;
    error?: string;
    checkedAt?: string;
    robots?: {
      status: "allowed";
      checkedAt: string;
      policyUrl: string;
      userAgent: string;
    };
  };
  evidence: Array<{
    prefix: string;
    highlight: string;
    suffix: string;
    relationIds: string[];
  }>;
  entities: Array<{ name: string; type: "COMPANY" | "MATERIAL" | "LOCATION" | "EVENT" | "SHAREHOLDER" }>;
  relations: Array<{ source: string; relation: string; target: string; confidence: number; basis?: "reported" | "modeled" }>;
  affected: Array<{ nodeId: string; label: string; score: number; reason: string; code?: string; industry?: string; market?: string }>;
  companyRoleAnalysis?: {
    provider: "local-qwen3-webgpu" | "target-price-rule";
    modelId: string;
    companies: Array<{ name: string; role: "affected" | "source" | "mentioned"; confidence: number; reason?: string }>;
  };
};

export function mergeNewsSignalsNewestFirst(
  incoming: NewsSignal[],
  previous: NewsSignal[],
  limit = 60,
): NewsSignal[] {
  const merged: NewsSignal[] = [];
  const indexById = new Map<string, number>();
  const indexByUrl = new Map<string, number>();

  for (const item of [...incoming, ...previous]) {
    const normalizedUrl = normalizeNewsUrl(item.sourceUrl);
    const existingIndex = indexById.get(item.id) ?? (normalizedUrl ? indexByUrl.get(normalizedUrl) : undefined);
    if (existingIndex !== undefined) {
      const existing = merged[existingIndex];
      merged[existingIndex] = {
        ...item,
        ...existing,
        articleUrl: existing.articleUrl ?? item.articleUrl,
        articleBody: existing.articleBody ?? item.articleBody,
        articleExtraction: existing.articleExtraction ?? item.articleExtraction,
      };
      continue;
    }
    const index = merged.push(item) - 1;
    indexById.set(item.id, index);
    if (normalizedUrl) indexByUrl.set(normalizedUrl, index);
  }

  return merged
    .sort((a, b) => newsTimestamp(b) - newsTimestamp(a))
    .slice(0, limit);
}

function normalizeNewsUrl(value: string): string {
  try {
    const url = new URL(value);
    ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"].forEach((key) => url.searchParams.delete(key));
    url.hash = "";
    return url.toString().replace(/\/$/, "");
  } catch {
    return value.trim().replace(/\/$/, "");
  }
}

function newsTimestamp(item: NewsSignal): number {
  const timestamp = Date.parse(item.publishedAtIso ?? item.publishedAt ?? "");
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

export const newsSignals: Record<string, NewsSignal> = {
  lgesFordCancellation: {
    id: "news-lges-ford-cancellation",
    tag: "계약",
    title: "LG에너지솔루션, 포드와 9.6조원 배터리 공급계약 종료",
    source: "Reuters · Investing.com",
    sourceUrl: "https://www.investing.com/news/stock-market-news/south-koreas-lg-energy-solution-ends-65-billion-ev-batterysupply-deal-with-ford-4411948",
    sourceType: "언론 보도",
    time: "2025.12.17",
    publishedAt: "2025.12.17",
    location: "대한민국 · 미국",
    sentiment: "매우 부정",
    confidence: 97,
    summary: "포드가 전기차 수요 둔화와 사업전략 변경을 이유로 LG에너지솔루션과의 대규모 배터리 공급계약을 종료한 실제 보도입니다.",
    paragraphs: [
      "Reuters 보도에 따르면 종료 대상 계약 규모는 약 9조6천억원(65억달러)입니다. 계약 취소는 유럽 전기차 배터리 수요 전망과 LG에너지솔루션의 생산능력 운영 계획을 함께 재평가하게 하는 신호입니다.",
      "이 프로토타입은 기사에 확인된 계약 당사자와 금액을 실제 사실로 저장하고, 그래프상의 후속 고객·소재기업 전이 점수는 별도의 모델 추정치로 계산합니다.",
    ],
    evidence: [
      { prefix: "기사에서 확인된 핵심 사실: ", highlight: "포드가 약 9조6천억원 규모의 배터리 공급계약을 종료", suffix: "했으며 LG에너지솔루션이 이를 공시했습니다.", relationIds: ["contract-lg", "lg-ford"] },
      { prefix: "계약 종료 배경으로 ", highlight: "전기차 수요 둔화와 포드의 전기차 전략 조정", suffix: "이 제시됐습니다.", relationIds: ["contract-ford"] },
    ],
    entities: [
      { name: "LG에너지솔루션", type: "COMPANY" }, { name: "Ford", type: "COMPANY" }, { name: "공급계약 종료", type: "EVENT" },
    ],
    relations: [
      { source: "LG에너지솔루션", relation: "SUPPLIES_TO", target: "Ford", confidence: 98 },
      { source: "Ford", relation: "TERMINATES_CONTRACT", target: "LG에너지솔루션", confidence: 97 },
    ],
    affected: [
      { nodeId: "lgensol", label: "LG에너지솔루션", score: 88, reason: "대규모 장기 공급계약 종료" },
      { nodeId: "ford", label: "Ford", score: 61, reason: "전기차 공급전략 재편" },
      { nodeId: "posco", label: "포스코퓨처엠", score: 49, reason: "셀 고객사 수요 변화의 모델상 2차 노출" },
    ],
  },
  lgesFreudenbergCancellation: {
    id: "news-lges-freudenberg-cancellation",
    tag: "계약",
    title: "LG에너지솔루션, 프로이덴베르크 3.9조원 배터리 주문 취소",
    source: "Reuters · MarketScreener",
    sourceUrl: "https://www.marketscreener.com/news/lg-energy-solution-cancels-3-9-trillion-won-battery-order-with-freudenberg-ce7e59dad188f125",
    sourceType: "언론 보도",
    time: "2025.12.26",
    publishedAt: "2025.12.26",
    location: "대한민국 · 독일",
    sentiment: "부정",
    confidence: 96,
    summary: "LG에너지솔루션이 프로이덴베르크와의 약 3조9천억원 규모 배터리 공급계약 취소를 알린 실제 Reuters 보도입니다.",
    paragraphs: [
      "이 계약 취소는 포드 계약 종료 직후 공개돼 전기차용 배터리 수요 둔화가 복수 고객 관계에서 동시에 나타나는지를 관찰할 수 있는 이벤트입니다.",
      "금액과 당사자는 보도 사실로 사용하며 소재사·완성차사로 전이되는 강도는 계약 구조와 산업 연결을 바탕으로 계산한 분석 모델 값입니다.",
    ],
    evidence: [
      { prefix: "기사에 따르면 ", highlight: "취소된 주문 규모는 약 3조9천억원", suffix: "이며 상대방은 독일 프로이덴베르크입니다.", relationIds: ["contract-lg", "lg-freudenberg"] },
      { prefix: "시계열상 이 이벤트는 ", highlight: "포드 공급계약 종료 후 열흘 이내에 공개", suffix: "돼 계약 집중 리스크를 높이는 신호로 분류했습니다.", relationIds: ["contract-lg"] },
    ],
    entities: [
      { name: "LG에너지솔루션", type: "COMPANY" }, { name: "Freudenberg", type: "COMPANY" }, { name: "주문 취소", type: "EVENT" },
    ],
    relations: [
      { source: "LG에너지솔루션", relation: "SUPPLIES_TO", target: "Freudenberg", confidence: 96 },
      { source: "Freudenberg", relation: "CANCELS_ORDER", target: "LG에너지솔루션", confidence: 95 },
    ],
    affected: [
      { nodeId: "lgensol", label: "LG에너지솔루션", score: 79, reason: "고객 주문 취소의 누적 영향" },
      { nodeId: "freudenberg", label: "Freudenberg", score: 54, reason: "배터리 조달계획 변경" },
      { nodeId: "posco", label: "포스코퓨처엠", score: 43, reason: "배터리 소재 수요의 모델상 간접 노출" },
    ],
  },
  poscoRightsIssue: {
    id: "news-posco-rights-issue",
    tag: "지분",
    title: "포스코홀딩스, 포스코퓨처엠 유상증자에 5,256억원 참여",
    source: "POSCO FUTURE M Newsroom",
    sourceUrl: "https://www.poscofuturem.com/en/pr/view.do?num=931",
    sourceType: "기업 발표",
    time: "2025.05.28",
    publishedAt: "2025.05.28",
    location: "대한민국 · 포항",
    sentiment: "주의",
    confidence: 99,
    summary: "포스코퓨처엠이 1.1조원 유상증자를 결의했고 최대주주 포스코홀딩스가 기존 59.7% 지분에 배정된 신주 전량을 인수하기로 했다는 공식 발표입니다.",
    paragraphs: [
      "공식 뉴스룸 자료는 포스코홀딩스의 투자액을 5,256억원으로 밝히고 있습니다. 조달 자금은 캐나다 양극재 합작사와 국내 생산능력 확장 등에 활용될 계획입니다.",
      "이 자료는 실제 지분 관계의 근거와 기준일을 제공하므로 OWNS_STAKE 엣지에는 59.7%를 공시 관측값으로 저장했습니다.",
    ],
    evidence: [
      { prefix: "공식 발표상 ", highlight: "포스코퓨처엠은 1.1조원 규모 유상증자를 결의", suffix: "했습니다.", relationIds: ["holdings-posco"] },
      { prefix: "포스코홀딩스는 ", highlight: "기존 지분율 59.7%에 해당하는 신주 전량을 5,256억원에 인수", suffix: "하기로 했습니다.", relationIds: ["holdings-posco"] },
    ],
    entities: [
      { name: "포스코홀딩스", type: "COMPANY" }, { name: "포스코퓨처엠", type: "COMPANY" }, { name: "유상증자", type: "EVENT" },
    ],
    relations: [
      { source: "포스코홀딩스", relation: "OWNS_STAKE", target: "포스코퓨처엠", confidence: 99 },
      { source: "포스코홀딩스", relation: "PARTICIPATES_IN", target: "유상증자", confidence: 99 },
    ],
    affected: [
      { nodeId: "posco", label: "포스코퓨처엠", score: 67, reason: "자본확충과 대규모 설비투자" },
      { nodeId: "poscoholdings", label: "포스코홀딩스", score: 48, reason: "최대주주 자금 투입" },
    ],
  },
  skHynixHbm4: {
    id: "news-skhynix-hbm4",
    tag: "HBM",
    title: "SK하이닉스, HBM4 개발 완료·양산 준비 발표",
    source: "SK hynix Newsroom",
    sourceUrl: "https://news.skhynix.com/sk-hynix-completes-worlds-first-hbm4-development-and-readies-mass-production/",
    sourceType: "기업 발표",
    time: "2025.09.12",
    publishedAt: "2025.09.12",
    location: "대한민국 · 이천",
    sentiment: "긍정",
    confidence: 99,
    summary: "SK하이닉스가 차세대 AI 메모리 HBM4 개발을 완료하고 양산 준비를 마쳤다고 발표한 공식 자료입니다.",
    paragraphs: [
      "회사 발표에 따르면 HBM4는 이전 세대 대비 대역폭을 두 배로 높이고 전력 효율을 40% 개선했습니다. 이 사실은 AI 메모리 공급능력 확대 신호로 그래프에 반영됩니다.",
      "프로토타입은 기술 발표 자체를 긍정 이벤트로 보존하면서도 특정 공급자와 장비에 수요가 집중되는 네트워크 리스크를 별도로 계산합니다.",
    ],
    evidence: [
      { prefix: "공식 발표에서 ", highlight: "HBM4 개발 완료와 양산 준비", suffix: "를 확인할 수 있습니다.", relationIds: ["demand-hynix"] },
      { prefix: "제품 성능은 ", highlight: "이전 세대 대비 대역폭 2배·전력 효율 40% 개선", suffix: "으로 설명됐습니다.", relationIds: ["hynix-ai"] },
    ],
    entities: [
      { name: "SK하이닉스", type: "COMPANY" }, { name: "HBM4", type: "MATERIAL" }, { name: "양산 준비", type: "EVENT" },
    ],
    relations: [
      { source: "SK하이닉스", relation: "DEVELOPS", target: "HBM4", confidence: 99 },
      { source: "HBM4", relation: "SUPPLIES_TO", target: "AI 가속기 생태계", confidence: 91 },
    ],
    affected: [
      { nodeId: "skhynix", label: "SK하이닉스", score: 84, reason: "HBM4 양산 전환과 공급 집중" },
      { nodeId: "asml", label: "ASML", score: 58, reason: "첨단 DRAM 증설 장비 수요" },
      { nodeId: "naver", label: "NAVER", score: 41, reason: "AI 인프라 공급의 모델상 간접 노출" },
    ],
  },
  skHynixEarnings: {
    id: "news-skhynix-3q25",
    tag: "실적",
    title: "SK하이닉스 3Q25 사상 최대 실적·2026년 수요 협의 완료",
    source: "SK hynix Newsroom",
    sourceUrl: "https://news.skhynix.com/en/sk-hynix-announces-3q25-financial-results/",
    sourceType: "기업 발표",
    time: "2025.10.29",
    publishedAt: "2025.10.29",
    location: "대한민국 · 이천",
    sentiment: "긍정",
    confidence: 99,
    summary: "SK하이닉스가 2025년 3분기 매출 24.4489조원, 영업이익 11.3834조원을 기록하고 다음 해 HBM 공급 협의를 마쳤다고 발표했습니다.",
    paragraphs: [
      "공식 실적 발표는 HBM과 서버용 고성능 제품 판매가 분기 최대 실적을 견인했다고 설명합니다. HBM4 출하는 2025년 4분기 시작 계획으로 제시됐습니다.",
      "수요 가시성이 높아진다는 긍정 신호와 동시에 공급이 소수 기업에 집중될 때의 전방산업 민감도를 함께 관찰할 수 있습니다.",
    ],
    evidence: [
      { prefix: "3분기 실적은 ", highlight: "매출 24.4489조원·영업이익 11.3834조원", suffix: "으로 발표됐습니다.", relationIds: ["demand-hynix"] },
      { prefix: "회사는 ", highlight: "2026년 HBM 공급 협의를 완료하고 HBM4 출하를 시작", suffix: "한다고 밝혔습니다.", relationIds: ["hynix-ai"] },
    ],
    entities: [
      { name: "SK하이닉스", type: "COMPANY" }, { name: "HBM4", type: "MATERIAL" }, { name: "3Q25 실적", type: "EVENT" },
    ],
    relations: [
      { source: "SK하이닉스", relation: "SUPPLIES_TO", target: "HBM 고객군", confidence: 94 },
      { source: "HBM 수요", relation: "DRIVES", target: "SK하이닉스 실적", confidence: 97 },
    ],
    affected: [
      { nodeId: "skhynix", label: "SK하이닉스", score: 82, reason: "HBM 수요와 생산계획 집중" },
      { nodeId: "sksquare", label: "SK스퀘어", score: 50, reason: "20.5% 주요주주 노출" },
      { nodeId: "hanmi", label: "한미반도체", score: 46, reason: "HBM 후공정 투자 사이클의 모델상 노출" },
    ],
  },
  samsungResults: {
    id: "news-samsung-fy25",
    tag: "실적",
    title: "삼성전자 4Q25 역대 최대 분기 매출·HBM4 공급 계획",
    source: "Samsung Global Newsroom",
    sourceUrl: "https://news.samsung.com/global/samsung-electronics-announces-fourth-quarter-and-fy-2025-results",
    sourceType: "기업 발표",
    time: "2026.01.29",
    publishedAt: "2026.01.29",
    location: "대한민국 · 수원",
    sentiment: "긍정",
    confidence: 99,
    summary: "삼성전자가 2025년 4분기 연결 매출 93.8조원, 영업이익 20.1조원을 발표하고 2026년 1분기 HBM4 공급 계획을 제시했습니다.",
    paragraphs: [
      "공식 발표에서 DS 부문은 4분기 매출 44.0조원, 영업이익 16.4조원을 기록했습니다. 메모리 사업은 HBM 판매 확대와 가격 상승의 영향을 받았습니다.",
      "이 데이터는 삼성전자의 AI 메모리 공급 확대를 확인하는 실제 이벤트이며, 전방 고객으로의 파급 강도는 프로토타입 모델이 별도로 추정합니다.",
    ],
    evidence: [
      { prefix: "삼성전자는 ", highlight: "4분기 매출 93.8조원·영업이익 20.1조원", suffix: "을 발표했습니다.", relationIds: ["demand-samsung"] },
      { prefix: "메모리 사업은 ", highlight: "2026년 1분기 HBM4 제품 공급을 시작할 계획", suffix: "이라고 밝혔습니다.", relationIds: ["samsung-ai"] },
    ],
    entities: [
      { name: "삼성전자", type: "COMPANY" }, { name: "HBM4", type: "MATERIAL" }, { name: "FY2025 실적", type: "EVENT" },
    ],
    relations: [
      { source: "삼성전자", relation: "SUPPLIES_TO", target: "AI 메모리 고객군", confidence: 93 },
      { source: "HBM 판매", relation: "DRIVES", target: "DS 실적", confidence: 97 },
    ],
    affected: [
      { nodeId: "samsung", label: "삼성전자", score: 78, reason: "HBM4 공급 확대와 설비집중" },
      { nodeId: "naver", label: "NAVER", score: 39, reason: "AI 인프라 조달의 모델상 간접 노출" },
    ],
  },
  mobisOwnership: {
    id: "news-mobis-ownership",
    tag: "지배구조",
    title: "현대모비스 특수관계인 지분 32.70% 확인",
    source: "Hyundai Mobis IR",
    sourceUrl: "https://www.mobis.com/en/ir/irinfo.do",
    sourceType: "공식 IR",
    time: "2025.12.31 기준",
    publishedAt: "2025.12.31 기준",
    location: "대한민국 · 서울",
    sentiment: "중립",
    confidence: 99,
    summary: "현대모비스 공식 IR에서 기아 18.10%, 정몽구 7.47%, 현대제철 6.07%, 현대글로비스 0.72%, 정의선 0.33% 등 특수관계인 지분 합계 32.70%를 확인할 수 있습니다.",
    paragraphs: [
      "이 데이터는 2025년 12월 31일 기준 공식 주주현황입니다. 현대모비스가 현대자동차 지분을 보유하고 현대자동차가 기아를, 기아가 다시 현대모비스를 보유하는 연결 구조의 한 축입니다.",
      "프로토타입은 개별 보유 지분율을 공시 관측값으로 저장하며, 지배력 판단은 지분율 외에 의결권 제한과 특수관계인 합산을 추가 검토해야 한다는 점을 표시합니다.",
    ],
    evidence: [
      { prefix: "공식 IR 기준 ", highlight: "기아의 현대모비스 보유 지분은 18.10%", suffix: "입니다.", relationIds: ["kia-mobis"] },
      { prefix: "특수관계인 합계에는 ", highlight: "정몽구 7.47%·현대제철 6.07%·정의선 0.33%", suffix: "가 포함됩니다.", relationIds: ["cmk-mobis", "steel-mobis", "ec-mobis"] },
    ],
    entities: [
      { name: "현대모비스", type: "COMPANY" }, { name: "기아", type: "COMPANY" }, { name: "정몽구", type: "SHAREHOLDER" }, { name: "정의선", type: "SHAREHOLDER" },
    ],
    relations: [
      { source: "기아", relation: "OWNS_STAKE", target: "현대모비스", confidence: 99 },
      { source: "정몽구", relation: "OWNS_STAKE", target: "현대모비스", confidence: 99 },
    ],
    affected: [
      { nodeId: "mobis-g", label: "현대모비스", score: 72, reason: "순환 지분 구조의 핵심 연결축" },
      { nodeId: "kia-g", label: "기아", score: 55, reason: "현대모비스 18.10% 보유" },
      { nodeId: "hyundai-g", label: "현대자동차", score: 47, reason: "현대모비스 21.86% 보유 구조" },
    ],
  },
  hyundaiResults: {
    id: "news-hyundai-fy25",
    tag: "주주환원",
    title: "현대자동차 2025 실적 발표·4,007억원 자사주 매입",
    source: "Hyundai Motor Newsroom",
    sourceUrl: "https://www.hyundai.com/content/hyundai/worldwide/en/newsroom/detail/hyundai-motor-announces-2025-annual-and-q4-business-results-0000001116.html",
    sourceType: "기업 발표",
    time: "2026.01.29",
    publishedAt: "2026.01.29",
    location: "대한민국 · 서울",
    sentiment: "중립",
    confidence: 99,
    summary: "현대자동차가 2025년 연간 실적과 함께 약 4,007억원 규모 자사주 매입 계획을 발표한 공식 자료입니다.",
    paragraphs: [
      "현대자동차는 2025년 영업이익률 6.2%, 순이익 10.36조원을 발표했습니다. 순이익은 전년 대비 21.7% 감소했으며 주주환원 조치로 자사주 매입 계획을 제시했습니다.",
      "주주환원 이벤트는 지분 네트워크의 소유관계 자체를 바꾸지는 않지만, 계열사·주주의 경제적 이해관계를 분석하는 보조 신호로 연결됩니다.",
    ],
    evidence: [
      { prefix: "공식 실적 발표상 ", highlight: "2025년 순이익은 10.36조원으로 전년 대비 21.7% 감소", suffix: "했습니다.", relationIds: ["mobis-hyundai"] },
      { prefix: "회사는 ", highlight: "약 4,007억원 규모 자사주 매입", suffix: "을 발표했습니다.", relationIds: ["mobis-hyundai", "hyundai-kia"] },
    ],
    entities: [
      { name: "현대자동차", type: "COMPANY" }, { name: "현대모비스", type: "COMPANY" }, { name: "자사주 매입", type: "EVENT" },
    ],
    relations: [
      { source: "현대모비스", relation: "OWNS_STAKE", target: "현대자동차", confidence: 99 },
      { source: "현대자동차", relation: "ANNOUNCES", target: "자사주 매입", confidence: 99 },
    ],
    affected: [
      { nodeId: "hyundai-g", label: "현대자동차", score: 66, reason: "실적·자본배분 변화" },
      { nodeId: "mobis-g", label: "현대모비스", score: 45, reason: "21.86% 주요주주 노출" },
      { nodeId: "kia-g", label: "기아", score: 38, reason: "그룹 내 순환 지분 연결" },
    ],
  },
  kiaOwnership: {
    id: "news-kia-ownership",
    tag: "지분",
    title: "현대자동차의 기아 지분 35.17% 공식 확인",
    source: "Kia Shareholders",
    sourceUrl: "https://worldwide.kia.com/en/company/sustainability/governance/shareholders",
    sourceType: "공식 IR",
    time: "최신 공시 페이지",
    publishedAt: "공식 주주현황",
    location: "대한민국 · 서울",
    sentiment: "중립",
    confidence: 99,
    summary: "기아 공식 주주현황에서 현대자동차가 보통주 137,318,251주, 35.17%를 보유한 최대주주임을 확인할 수 있습니다.",
    paragraphs: [
      "현대자동차의 기아 지분 35.17%는 현대모비스→현대자동차→기아→현대모비스로 이어지는 지분 연결의 두 번째 구간입니다.",
      "50% 초과 지분 규칙만 적용하면 단독 지배회사로 자동 추론되지 않습니다. 실제 지배력 판단에는 특수관계인, 이사회, 의결권 계약 등을 추가해야 합니다.",
    ],
    evidence: [
      { prefix: "기아 공식 페이지 기준 ", highlight: "현대자동차는 기아 보통주 137,318,251주(35.17%)를 보유", suffix: "하고 있습니다.", relationIds: ["hyundai-kia"] },
      { prefix: "이 지분은 ", highlight: "현대자동차를 기아의 최대주주로 만드는 공시 관측값", suffix: "입니다.", relationIds: ["hyundai-kia"] },
    ],
    entities: [
      { name: "현대자동차", type: "COMPANY" }, { name: "기아", type: "COMPANY" }, { name: "최대주주", type: "EVENT" },
    ],
    relations: [
      { source: "현대자동차", relation: "OWNS_STAKE", target: "기아", confidence: 99 },
    ],
    affected: [
      { nodeId: "kia-g", label: "기아", score: 61, reason: "현대자동차 35.17% 지분" },
      { nodeId: "hyundai-g", label: "현대자동차", score: 52, reason: "기아 최대주주" },
      { nodeId: "mobis-g", label: "현대모비스", score: 41, reason: "기아의 현대모비스 지분과 순환 연결" },
    ],
  },
};
