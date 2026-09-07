"use client";

import { useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent, type MouseEvent } from "react";
import { controllingEntities, diffuseRisk, rankCompanies, strongestPath, type NodeRisk } from "../lib/risk-engine";
import { mergeNewsSignalsNewestFirst, type NewsSignal } from "../lib/news-data";
import { applyCompanyRolesToNews, companyRoleFor, inferTargetPriceRoles, NEWS_ROLE_MODEL_ID, type CompanyRoleAnalysis } from "../lib/news-role-analysis";
import newsRoleWorkerUrl from "../lib/news-role.worker.ts?worker&url";
import { scenarios, type RiskEdge, type RiskNode, type Scenario } from "../lib/risk-data";
import PortalNav from "./portal-nav";
import ThreeNetworkGraph from "./three-network-graph";

type GraphMode = "news" | "aggregate" | "path" | "ownership";
export type DashboardView = "news" | "network" | "companies" | "data";
type FeedStatus = "loading" | "live" | "fallback";
type LocalAiState = "idle" | "loading" | "analyzing" | "ready" | "unsupported" | "error";
type NewsFilter = "all" | "battery" | "semiconductor" | "governance";
type LiveFeed = {
  items: NewsSignal[];
  requested?: string;
  collectedAt: string;
  provider: string;
  apiKeyConfigured?: boolean;
  fallbackReason?: string | null;
  refreshAfterSeconds?: number;
  companyQuery?: string | null;
  persistence?: { enabled: boolean; status: "stored" | "database_unavailable" | "database_error"; historyCount: number; database?: "d1" | "mysql" };
  bodyCrawl?: { enabled: boolean; attempted?: number; succeeded?: number; failed?: number; limit?: number };
  companyRegistry?: { provider: string; count: number; domesticCount?: number; foreignCount?: number; foreignTickerAssociations?: number; asOf: string };
};

const fallbackNews: NewsSignal[] = scenarios.flatMap((scenario) =>
  scenario.news.map((news) => ({
    ...news,
    scenarioId: scenario.id as Exclude<NewsFilter, "all">,
  })),
);

const categoryLabel: Record<NewsFilter, string> = {
  all: "전체",
  battery: "배터리",
  semiconductor: "반도체",
  governance: "지배구조",
};

function newsRootId(scenarioId: string) {
  return `news-root-${scenarioId}`;
}

function articleRootId(newsId: string) {
  return `article-root-${newsId}`;
}

function normalizeEntityName(value: string) {
  return value.toLocaleLowerCase("ko-KR").replace(/[^a-z0-9가-힣]/g, "");
}

function stableEntityId(value: string) {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) hash = ((hash << 5) - hash + value.charCodeAt(index)) | 0;
  return Math.abs(hash).toString(36);
}

const companySymbolByNodeId: Record<string, string> = {
  "samsung-sdi": "006400",
  "ecopro-bm": "247540",
  lf: "066970",
  "hyundai-wia": "011210",
  "hyundai-autoever": "307950",
  "hyundai-ec": "000720",
  "hyundai-securities": "001500",
  ford: "F",
  asml: "ASML",
  tesla: "TSLA",
  gm: "GM",
  nvidia: "NVDA",
  micron: "MU",
  tsmc: "TSM",
  broadcom: "AVGO",
};

const bundledCompanySymbols = new Set([
  "000270", "000660", "000720", "001500", "003670", "004020", "005380", "005490", "005930", "006400", "011210", "012330", "035420", "042700", "051910", "066970", "086280", "247540", "307950", "373220", "402340",
  "ASML", "AVGO", "F", "GM", "MU", "NVDA", "TSLA", "TSM",
]);

function companyLogoPath(node?: Pick<RiskNode, "code" | "kind">, nodeId?: string) {
  const symbol = node?.code ?? (nodeId ? companySymbolByNodeId[nodeId] : undefined);
  const supportedSymbol = symbol !== "NEWS" && Boolean(symbol?.match(/^(?:\d{6}|[A-Z]{1,5})$/));
  if ((node?.kind !== "company" && !(!node && nodeId)) || !supportedSymbol || !symbol) return null;
  return bundledCompanySymbols.has(symbol)
    ? `/company-logos/${symbol}.png`
    : `https://static.toss.im/png-icons/securities/icn-sec-fill-${symbol}.png`;
}

const knownCompanyById = new Map(
  scenarios.flatMap((scenario) => scenario.nodes).filter((node) => node.kind === "company").map((node) => [node.id, node]),
);

function CompanyLogo({ node, nodeId, label, className = "" }: { node?: RiskNode; nodeId?: string; label?: string; className?: string }) {
  const logoPath = companyLogoPath(node, nodeId);
  const companyLabel = node?.label ?? label ?? "기업";
  return (
    <span
      className={`stock-logo ${logoPath ? "has-image" : "fallback"} ${className}`.trim()}
      style={logoPath ? { backgroundImage: `url(${logoPath})` } : undefined}
      role="img"
      aria-label={`${companyLabel} 종목 이미지`}
    >
      {!logoPath && companyLabel.slice(0, 1).toUpperCase()}
    </span>
  );
}

function isDirectNewsCompany(news: NewsSignal, label: string) {
  const normalized = normalizeEntityName(label);
  const directNames = news.entities.filter((entity) => entity.type === "COMPANY").map((entity) => entity.name);
  return directNames.some((name) => {
    const candidate = normalizeEntityName(name);
    return candidate === normalized || candidate.includes(normalized) || normalized.includes(candidate);
  });
}

function buildArticleNewsScenario(base: Scenario, news: NewsSignal): Scenario {
  const rootId = articleRootId(news.id);
  const baseById = new Map(base.nodes.map((node) => [node.id, node]));
  const reportedAffected = news.affected.map((company) => ({ ...company, direct: isDirectNewsCompany(news, company.label) }));
  const directCompanies = reportedAffected.filter((company) => company.direct);
  const directIds = new Set(directCompanies.map((company) => company.nodeId));
  const graphConnectedIndirect = directCompanies.flatMap((directCompany) => base.edges.flatMap((edge) => {
    const connectedId = edge.source === directCompany.nodeId ? edge.target : edge.target === directCompany.nodeId ? edge.source : null;
    const connectedNode = connectedId ? baseById.get(connectedId) : null;
    if (!connectedNode || connectedNode.kind !== "company" || directIds.has(connectedNode.id)) return [];
    const basisFactor = edge.basis === "modeled" ? .82 : 1;
    return [{
      nodeId: connectedNode.id,
      label: connectedNode.label,
      score: Math.max(25, Math.min(92, Math.round(directCompany.score * edge.weight * basisFactor))),
      reason: `${directCompany.label}와 연결된 ${edge.label} · ${edge.evidence}`,
      code: connectedNode.code,
      industry: connectedNode.industry,
      direct: false,
    }];
  }));
  const indirectCompanies = [...reportedAffected.filter((company) => !company.direct), ...graphConnectedIndirect]
    .filter((company, index, all) => all.findIndex((candidate) => candidate.nodeId === company.nodeId) === index)
    .sort((a, b) => b.score - a.score);
  const affectedWithDirectness = [...directCompanies, ...indirectCompanies];
  const affectedByName = new Map(affectedWithDirectness.map((company) => [normalizeEntityName(company.label), company]));
  const reportedRelations = news.relations.filter((relation) => relation.basis !== "modeled");
  const normalizedTitle = normalizeEntityName(news.title);
  const isArticleEventName = (name: string) => {
    const normalized = normalizeEntityName(name);
    return normalized.length >= 6 && normalizedTitle.includes(normalized);
  };

  const endpointNames = reportedRelations.flatMap((relation) => [relation.source, relation.target]);
  const relationOnlyNames = endpointNames.filter((name, index, all) => {
    const normalized = normalizeEntityName(name);
    return !isArticleEventName(name) && !affectedByName.has(normalized) && all.findIndex((candidate) => normalizeEntityName(candidate) === normalized) === index;
  });

  const companyNode = (company: (typeof affectedWithDirectness)[number], index: number, peers: number): RiskNode => {
    const existing = baseById.get(company.nodeId);
    return {
      ...(existing ?? {
        id: company.nodeId,
        label: company.label,
        kind: "company" as const,
        code: company.code ?? companySymbolByNodeId[company.nodeId] ?? "NEWS",
        industry: company.industry ?? categoryLabel[base.id as NewsFilter],
        description: company.reason,
      }),
      description: company.reason,
      sourceUrl: news.sourceUrl,
      sourceLabel: news.source,
      dataAsOf: news.publishedAt,
      x: company.direct ? .42 : .83,
      y: peers === 1 ? .5 : .14 + index * (.72 / Math.max(1, peers - 1)),
    };
  };

  const directNodes = directCompanies.map((company, index) => companyNode(company, index, directCompanies.length));
  const indirectNodes = indirectCompanies.map((company, index) => companyNode(company, index, indirectCompanies.length));
  const affectedIdByName = new Map(affectedWithDirectness.map((company) => [normalizeEntityName(company.label), company.nodeId]));
  const newsEntityByName = new Map(news.entities.map((entity) => [normalizeEntityName(entity.name), entity]));
  const relationNodes: RiskNode[] = relationOnlyNames.map((label, index) => {
    const entity = newsEntityByName.get(normalizeEntityName(label));
    const kind: RiskNode["kind"] = entity?.type === "COMPANY" ? "company" : entity?.type === "MATERIAL" ? "material" : entity?.type === "SHAREHOLDER" ? "shareholder" : "event";
    return {
      id: `article-entity-${stableEntityId(`${news.id}-${label}`)}`,
      label,
      kind,
      description: `${news.title}에서 추출된 ${entity?.type ?? "RELATION"} 엔티티`,
      sourceUrl: news.sourceUrl,
      sourceLabel: news.source,
      dataAsOf: news.publishedAt,
      x: .62,
      y: relationOnlyNames.length === 1 ? .5 : .18 + index * (.64 / Math.max(1, relationOnlyNames.length - 1)),
    };
  });
  const relationIdByName = new Map(relationNodes.map((node) => [normalizeEntityName(node.label), node.id]));
  const nodeIdForName = (name: string) => isArticleEventName(name) ? rootId : affectedIdByName.get(normalizeEntityName(name)) ?? relationIdByName.get(normalizeEntityName(name));

  const rootNode: RiskNode = {
    id: rootId,
    label: news.title.length > 18 ? `${news.title.slice(0, 18)}…` : news.title,
    kind: "event",
    country: news.location,
    description: news.summary,
    sourceUrl: news.sourceUrl,
    sourceLabel: news.source,
    dataAsOf: news.publishedAt,
    x: .07,
    y: .5,
  };

  const relationName: Record<RiskEdge["relation"], string> = {
    SUPPLIES_TO: "공급망 전이",
    OWNS_STAKE: "지분 연관",
    USES_MATERIAL: "원자재 연관",
    BELONGS_TO: "산업 연관",
    SHORTAGE_RISK: "리스크 전이",
    NEWS_MENTIONS: "직접 언급",
    EXTRACTED_RELATION: "기사 추출",
    MODEL_EXPOSURE: "연관 전이",
  };

  const directImpactEdges: RiskEdge[] = directCompanies.map((company) => {
    const normalizedCompany = normalizeEntityName(company.label);
    const directRelation = reportedRelations.find((relation) => {
      const source = normalizeEntityName(relation.source);
      const target = normalizeEntityName(relation.target);
      return (target === normalizedCompany && isArticleEventName(relation.source)) || (source === normalizedCompany && isArticleEventName(relation.target));
    });
    return {
      id: `article-direct-${news.id}-${company.nodeId}`,
      source: rootId,
      target: company.nodeId,
      relation: "NEWS_MENTIONS",
      weight: Math.min(.98, Math.max(.25, company.score / 100)),
      label: `${directRelation?.relation ?? "직접 언급"} · ${company.score}`,
      evidence: company.reason,
      basis: "reported",
      sourceUrl: news.sourceUrl,
      sourceLabel: news.source,
      dataAsOf: news.publishedAt,
    };
  });

  const propagationEdges: RiskEdge[] = indirectCompanies.flatMap((company) => {
    return directCompanies.flatMap((directCompany) => {
      const baseConnection = base.edges
        .filter((edge) => (edge.source === directCompany.nodeId && edge.target === company.nodeId) || (edge.target === directCompany.nodeId && edge.source === company.nodeId))
        .sort((a, b) => b.weight - a.weight)[0];
      if (!baseConnection) return [];
      const relation = baseConnection.relation;
      const relationshipWeight = baseConnection.weight;
      const propagatedWeight = Math.min(.98, Math.max(.25, (company.score / 100) * (.55 + relationshipWeight * .45)));

      return [{
        id: `article-model-${news.id}-${directCompany.nodeId}-${company.nodeId}`,
        source: directCompany.nodeId,
        target: company.nodeId,
        relation,
        weight: propagatedWeight,
        label: `${relationName[relation]} · ${Math.round(propagatedWeight * 100)}`,
        evidence: `${company.reason} · ${baseConnection.evidence}`,
        basis: baseConnection.basis,
        sourceUrl: baseConnection.sourceUrl ?? news.sourceUrl,
        sourceLabel: baseConnection.sourceLabel ?? news.source,
        dataAsOf: baseConnection.dataAsOf ?? news.publishedAt,
      }];
    });
  });

  const impactEdges = [...directImpactEdges, ...propagationEdges];

  const tripleEdges: RiskEdge[] = reportedRelations.flatMap((relation, index) => {
    const source = nodeIdForName(relation.source);
    const target = nodeIdForName(relation.target);
    if (!source || !target || source === target || (source === rootId && affectedWithDirectness.some((company) => company.nodeId === target))) return [];
    return [{
      id: `article-triple-${news.id}-${index}`,
      source,
      target,
      relation: "EXTRACTED_RELATION" as const,
      weight: Math.min(.99, Math.max(.3, relation.confidence / 100)),
      label: relation.relation,
      evidence: `기사에서 추출한 ${relation.source} → ${relation.target} 관계`,
      basis: "reported" as const,
      sourceUrl: news.sourceUrl,
      sourceLabel: news.source,
      dataAsOf: news.publishedAt,
    }];
  });

  const sentimentFactor = news.sentiment === "매우 부정" ? 1 : news.sentiment === "부정" ? .94 : news.sentiment === "주의" ? .86 : .76;
  return {
    ...base,
    title: "실제 뉴스별 관계 지도",
    subtitle: `${news.source} 기사 1건에서 추출한 직접 관계와 모델 전이를 분리 표시`,
    severityScore: Math.round(news.confidence * sentimentFactor),
    shockSource: rootId,
    maxDepth: 3,
    dataAsOf: `${news.source} · ${news.publishedAt}`,
    nodes: [rootNode, ...directNodes, ...relationNodes, ...indirectNodes],
    edges: [...impactEdges, ...tripleEdges],
    news: [news],
  };
}

function buildMultiNewsScenario(base: Scenario, allNews: NewsSignal[]): Scenario {
  const signals = allNews.filter((news) => news.scenarioId === base.id).slice(0, 6);
  if (!signals.length) return base;

  const rootId = newsRootId(base.id);
  const originalShock = base.nodes.find((node) => node.id === base.shockSource);
  const removeOriginalShock = originalShock?.kind === "event";
  const baseNodes = base.nodes
    .filter((node) => !removeOriginalShock || node.id !== base.shockSource)
    .map((node) => ({ ...node, x: .49 + node.x * .47 }));
  const baseEdges = base.edges.filter((edge) => !removeOriginalShock || (edge.source !== base.shockSource && edge.target !== base.shockSource));
  const existingIds = new Set(baseNodes.map((node) => node.id));
  const affectedCompanies = signals
    .flatMap((news) => news.affected)
    .filter((company, index, all) => all.findIndex((candidate) => candidate.nodeId === company.nodeId) === index);
  const missingCompanies = affectedCompanies.filter((company) => !existingIds.has(company.nodeId));
  const columnRows = Math.max(1, Math.ceil(missingCompanies.length / 2));

  const dynamicCompanyNodes: RiskNode[] = missingCompanies.map((company, index) => ({
    id: company.nodeId,
    label: company.label,
    kind: "company",
    code: company.code ?? companySymbolByNodeId[company.nodeId] ?? "NEWS",
    industry: company.industry ?? categoryLabel[base.id as NewsFilter],
    description: `${signals.filter((news) => news.affected.some((item) => item.nodeId === company.nodeId)).length}건의 최신 뉴스에서 인식된 연결 기업`,
    sourceUrl: signals.find((news) => news.affected.some((item) => item.nodeId === company.nodeId))?.sourceUrl,
    sourceLabel: "NAVER API HUB 뉴스",
    dataAsOf: signals[0]?.publishedAt,
    x: index % 2 === 0 ? .79 : .94,
    y: .11 + Math.floor(index / 2) * (.78 / Math.max(1, columnRows - 1)),
  }));

  const eventNodes: RiskNode[] = signals.map((news, index) => ({
    id: `news-event-${news.id}`,
    label: news.title.length > 15 ? `${news.title.slice(0, 15)}…` : news.title,
    kind: "event",
    country: news.location,
    description: news.summary,
    sourceUrl: news.sourceUrl,
    sourceLabel: news.source,
    dataAsOf: news.publishedAt,
    x: .25,
    y: signals.length === 1 ? .5 : .1 + index * (.8 / (signals.length - 1)),
  }));

  const rootNode: RiskNode = {
    id: rootId,
    label: `최신 뉴스 ${signals.length}건`,
    kind: "event",
    country: "LIVE",
    description: `${categoryLabel[base.id as NewsFilter]} 카테고리의 최신 뉴스에서 기업·관계를 통합한 충격원`,
    sourceLabel: "NAVER API HUB",
    dataAsOf: signals[0]?.publishedAt,
    x: .04,
    y: .5,
  };

  const sourceEdges: RiskEdge[] = signals.map((news, index) => {
    const sentimentFactor = news.sentiment === "매우 부정" ? 1 : news.sentiment === "부정" ? .94 : news.sentiment === "주의" ? .84 : news.sentiment === "긍정" ? .68 : .74;
    const recencyFactor = Math.max(.7, 1 - index * .055);
    const weight = Math.min(.98, Math.max(.45, news.confidence / 100 * sentimentFactor * recencyFactor));
    return {
      id: `news-source-${news.id}`,
      source: rootId,
      target: `news-event-${news.id}`,
      relation: "SHORTAGE_RISK",
      weight,
      label: `뉴스 강도 ${weight.toFixed(2)}`,
      evidence: `${news.source} · ${news.publishedAt} · 기사 신뢰도·톤·최신성 결합`,
      basis: "modeled",
      sourceUrl: news.sourceUrl,
      sourceLabel: news.source,
      dataAsOf: news.publishedAt,
    };
  });

  const companyEdges: RiskEdge[] = signals.flatMap((news) => news.affected.map((company) => {
    const weight = Math.min(.97, Math.max(.35, company.score / 100));
    return {
      id: `news-impact-${news.id}-${company.nodeId}`,
      source: `news-event-${news.id}`,
      target: company.nodeId,
      relation: "SHORTAGE_RISK" as const,
      weight,
      label: `연결 강도 ${weight.toFixed(2)}`,
      evidence: `${news.title} · ${company.reason}`,
      basis: "modeled" as const,
      sourceUrl: news.sourceUrl,
      sourceLabel: news.source,
      dataAsOf: news.publishedAt,
    };
  }));

  return {
    ...base,
    title: `${categoryLabel[base.id as NewsFilter]} 멀티뉴스 리스크 네트워크`,
    subtitle: `최신 ${signals.length}건의 뉴스와 ${affectedCompanies.length}개 기업 연결을 통합 분석`,
    shockSource: rootId,
    severityScore: Math.round(sourceEdges.reduce((sum, edge) => sum + edge.weight, 0) / sourceEdges.length * 100),
    maxDepth: base.maxDepth + 1,
    dataAsOf: `NAVER API HUB 최신 ${signals.length}건 · ${signals[0]?.publishedAt}`,
    nodes: [rootNode, ...eventNodes, ...baseNodes, ...dynamicCompanyNodes],
    edges: [...sourceEdges, ...companyEdges, ...baseEdges],
  };
}

const kindLabel: Record<RiskNode["kind"], string> = {
  company: "기업",
  shareholder: "주주",
  industry: "산업",
  material: "원자재",
  event: "이벤트",
};

function NetworkGraph({
  scenario,
  risk,
  selectedId,
  path,
  progress,
  mode,
  onSelect,
}: {
  scenario: Scenario;
  risk: Map<string, NodeRisk>;
  selectedId: string;
  path: string[];
  progress: number;
  mode: GraphMode;
  onSelect: (id: string) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;

    const logoImages = new Map<string, HTMLImageElement>();
    const draw = () => {
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.round(rect.width * ratio);
      canvas.height = Math.round(rect.height * ratio);
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.scale(ratio, ratio);
      ctx.clearRect(0, 0, rect.width, rect.height);

      const padX = 46;
      const padY = 34;
      const width = rect.width - padX * 2;
      const height = rect.height - padY * 2;
      const byId = new Map(scenario.nodes.map((node) => [node.id, node]));
      const p = (node: RiskNode) => ({ x: padX + node.x * width, y: padY + node.y * height });
      const pathEdges = new Set(path.slice(1).map((id, index) => `${path[index]}:${id}`));
      const modeledTargets = new Set(scenario.edges.filter((edge) => edge.id.startsWith("article-model-")).map((edge) => edge.target));

      ctx.lineCap = "round";
      scenario.edges.forEach((edge) => {
        const source = byId.get(edge.source);
        const target = byId.get(edge.target);
        if (!source || !target) return;
        const a = p(source);
        const b = p(target);
        const angle = Math.atan2(b.y - a.y, b.x - a.x);
        const sourceRadius = source.kind === "material" || source.kind === "shareholder" ? 23 : 18;
        const targetRadius = target.kind === "material" || target.kind === "shareholder" ? 23 : 18;
        const ax = a.x + Math.cos(angle) * sourceRadius;
        const ay = a.y + Math.sin(angle) * sourceRadius;
        const bx = b.x - Math.cos(angle) * (targetRadius + 4);
        const by = b.y - Math.sin(angle) * (targetRadius + 4);
        const targetDepth = risk.get(edge.target)?.depth ?? 99;
        const isReached = targetDepth <= Math.max(0, Math.ceil(progress * (scenario.maxDepth + 1)));
        const isPath = pathEdges.has(`${edge.source}:${edge.target}`);
        const isNewsEdge = edge.id.startsWith("news-");
        const isArticleEdge = edge.id.startsWith("article-");
        const isArticleDirect = edge.id.startsWith("article-direct-");
        const isArticleModel = edge.id.startsWith("article-model-");
        const isArticleTriple = edge.id.startsWith("article-triple-");
        const relevant = mode === "ownership" ? edge.relation === "OWNS_STAKE" : mode === "path" ? isPath : mode === "news" ? isArticleEdge : true;
        const alpha = relevant ? (isReached ? .68 : .19) : .07;
        const edgeColor = isPath && mode === "path"
          ? `rgba(255, 92, 64, ${alpha + .18})`
          : isArticleDirect
            ? `rgba(20, 174, 92, ${alpha + .16})`
            : isArticleTriple
              ? `rgba(139, 92, 246, ${alpha + .16})`
              : isArticleModel
                ? `rgba(237, 145, 55, ${alpha + .13})`
                : isNewsEdge
                  ? `rgba(20, 174, 92, ${alpha + .14})`
                  : `rgba(62, 79, 107, ${alpha})`;

        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(bx, by);
        ctx.setLineDash(edge.relation === "OWNS_STAKE" ? [5, 5] : []);
        ctx.strokeStyle = edgeColor;
        ctx.lineWidth = isPath && mode === "path" ? 2.8 : .8 + edge.weight * 2.3;
        ctx.stroke();
        ctx.setLineDash([]);

        if (relevant) {
          const arrowSize = 5;
          ctx.beginPath();
          ctx.moveTo(bx, by);
          ctx.lineTo(bx - arrowSize * Math.cos(angle - Math.PI / 6), by - arrowSize * Math.sin(angle - Math.PI / 6));
          ctx.lineTo(bx - arrowSize * Math.cos(angle + Math.PI / 6), by - arrowSize * Math.sin(angle + Math.PI / 6));
          ctx.closePath();
          ctx.fillStyle = edgeColor;
          ctx.fill();

          if (mode === "news" && isArticleEdge) {
            const label = edge.label.length > 22 ? `${edge.label.slice(0, 22)}…` : edge.label;
            const labelX = (ax + bx) / 2;
            const labelY = (ay + by) / 2 - 7;
            ctx.font = "700 8px \"Malgun Gothic\", Arial, sans-serif";
            const labelWidth = ctx.measureText(label).width + 12;
            ctx.beginPath();
            ctx.roundRect(labelX - labelWidth / 2, labelY - 8, labelWidth, 16, 6);
            ctx.fillStyle = "rgba(255,255,255,.92)";
            ctx.fill();
            ctx.strokeStyle = isArticleTriple ? "rgba(139,92,246,.22)" : isArticleModel ? "rgba(237,145,55,.24)" : "rgba(20,174,92,.22)";
            ctx.lineWidth = 1;
            ctx.stroke();
            ctx.fillStyle = isArticleTriple ? "#6d3fd1" : isArticleModel ? "#a85b16" : "#087b3b";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(label, labelX, labelY + .5);
          }
        }
      });

      scenario.nodes.forEach((node) => {
        const point = p(node);
        const nodeRisk = risk.get(node.id);
        const score = nodeRisk?.score ?? 0;
        const reached = (nodeRisk?.depth ?? 99) <= Math.max(0, Math.ceil(progress * (scenario.maxDepth + 1)));
        const radius = node.kind === "material" || node.kind === "shareholder" ? 23 : 18;
        const selected = selectedId === node.id;
        const isOrigin = node.id === scenario.shockSource;

        if (score > 0 && reached) {
          const glow = radius + 7 + score / 16;
          const gradient = ctx.createRadialGradient(point.x, point.y, radius, point.x, point.y, glow);
          gradient.addColorStop(0, `rgba(220, 76, 56, ${score / 430})`);
          gradient.addColorStop(1, "rgba(220, 76, 56, 0)");
          ctx.beginPath();
          ctx.arc(point.x, point.y, glow, 0, Math.PI * 2);
          ctx.fillStyle = gradient;
          ctx.fill();
        }

        if (selected) {
          ctx.beginPath();
          ctx.arc(point.x, point.y, radius + 6, 0, Math.PI * 2);
          ctx.strokeStyle = "rgba(49, 130, 246, .42)";
          ctx.lineWidth = 2;
          ctx.stroke();
        }

        const stockLogo = logoImages.get(node.id);
        const hasStockLogo = Boolean(stockLogo?.complete && stockLogo.naturalWidth);
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
        const isNewsEvent = node.id.startsWith("news-event-");
        const isModeledCompany = modeledTargets.has(node.id);
        ctx.fillStyle = hasStockLogo ? "#fff" : isOrigin ? "#3182f6" : isNewsEvent ? "#e8f3ff" : isModeledCompany ? "#fff1dd" : node.kind === "material" ? "#7357c6" : node.kind === "shareholder" ? "#7c654d" : "#333d4b";
        ctx.fill();
        if (hasStockLogo && stockLogo) {
          ctx.save();
          ctx.beginPath();
          ctx.arc(point.x, point.y, radius - 1, 0, Math.PI * 2);
          ctx.clip();
          ctx.drawImage(stockLogo, point.x - radius + 1, point.y - radius + 1, (radius - 1) * 2, (radius - 1) * 2);
          ctx.restore();
        }
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 3;
        ctx.stroke();

        if (!hasStockLogo) {
          ctx.fillStyle = isNewsEvent ? "#1b64da" : isModeledCompany ? "#a85b16" : "#fff";
          ctx.font = `700 ${isOrigin ? 11 : 9}px "Cascadia Mono", Consolas, monospace`;
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(isOrigin ? "LIVE" : isNewsEvent ? "N" : node.kind === "company" ? "CO" : node.kind === "shareholder" ? "%" : "RM", point.x, point.y + .5);
        }

        ctx.fillStyle = "#26334a";
        ctx.font = `650 ${rect.width < 650 ? 9 : 11}px "Malgun Gothic", Arial, sans-serif`;
        ctx.textBaseline = "alphabetic";
        ctx.fillText(node.label, point.x, point.y + radius + 18);

        if (nodeRisk && node.id !== scenario.shockSource) {
          ctx.fillStyle = score >= 60 ? "#dc4c38" : "#8993a2";
          ctx.font = "700 8px \"Cascadia Mono\", Consolas, monospace";
          ctx.fillText(`${score}`, point.x, point.y - radius - 8);
        }
      });
    };

    scenario.nodes.forEach((node) => {
      const logoPath = companyLogoPath(node);
      if (!logoPath) return;
      const image = new Image();
      image.onload = draw;
      image.src = logoPath;
      logoImages.set(node.id, image);
    });
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => {
      observer.disconnect();
      logoImages.forEach((image) => { image.onload = null; });
    };
  }, [mode, path, progress, risk, scenario, selectedId]);

  const handleClick = (event: MouseEvent<HTMLCanvasElement>) => {
    const canvas = ref.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const padX = 46;
    const padY = 34;
    const width = rect.width - padX * 2;
    const height = rect.height - padY * 2;
    const mx = event.clientX - rect.left;
    const my = event.clientY - rect.top;
    const match = scenario.nodes
      .map((node) => ({ node, distance: Math.hypot(mx - (padX + node.x * width), my - (padY + node.y * height)) }))
      .sort((a, b) => a.distance - b.distance)[0];
    if (match?.distance <= 36) onSelect(match.node.id);
  };

  return (
    <canvas
      className="network-canvas"
      ref={ref}
      onClick={handleClick}
      aria-label={`${scenario.title} 리스크 전이 네트워크. 노드를 클릭하면 상세정보가 표시됩니다.`}
    />
  );
}

function NewsDetail({
  news,
  index,
  total,
  onClose,
  onNavigate,
  onSelectCompany,
}: {
  news: NewsSignal;
  index: number;
  total: number;
  onClose: () => void;
  onNavigate: (direction: -1 | 1) => void;
  onSelectCompany: (nodeId: string) => void;
}) {
  const reportedRelations = news.relations.filter((relation) => relation.basis !== "modeled");
  const articleParagraphs = news.articleBody
    ? news.articleBody.split(/\n{2,}/).map((paragraph) => paragraph.trim()).filter(Boolean)
    : news.paragraphs;
  return (
    <div className="news-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <article className="news-drawer" role="dialog" aria-modal="true" aria-labelledby="news-detail-title">
        <header className="news-drawer-header">
          <div className="news-breadcrumb"><span>EVENT INTELLIGENCE</span><i>/</i> NEWS EVIDENCE</div>
          <button className="news-close" onClick={onClose} aria-label="뉴스 상세 닫기">×</button>
        </header>

        <div className="news-detail-scroll">
          <section className="news-article-head">
            <div className="news-source-row">
              <div className="source-monogram">{news.source.split(" ").map((word) => word[0]).join("").slice(0, 2)}</div>
              <div><b>{news.source}</b><small>{news.publishedAt}</small></div>
              <span className="prototype-label">SOURCE LINKED</span>
            </div>
            <div className="article-category"><span>{news.tag}</span><i>소스 스냅샷</i><i>{news.sourceType}</i></div>
            <h2 id="news-detail-title">{news.title}</h2>
            <p className="article-summary">{news.summary}</p>
            <a className="source-link-button" href={news.sourceUrl} target="_blank" rel="noreferrer noopener">원문 출처 열기 ↗</a>
            <div className="article-meta">
              <span><small>발생 지역</small><b>{news.location}</b></span>
              <span><small>뉴스 톤</small><b className={news.sentiment.includes("부정") ? "negative" : ""}>{news.sentiment}</b></span>
              <span><small>NLP 신뢰도</small><b>{news.confidence}%</b></span>
              <span><small>기사 추출 관계</small><b>{reportedRelations.length} TRIPLES</b></span>
            </div>
          </section>

          <div className="news-detail-grid">
            <section className="article-column">
              <div className="detail-section-title"><span>01</span><div><small>ARTICLE</small><b>뉴스 내용</b></div></div>
              <div className="article-body">
                {articleParagraphs.map((paragraph, paragraphIndex) => <p key={paragraphIndex}>{paragraph}</p>)}
              </div>

              <div className="detail-section-title evidence-title"><span>02</span><div><small>EVIDENCE MAPPING</small><b>소스 기반 핵심 사실</b></div><em>원문을 직접 인용하지 않고 사실을 요약해 엣지와 연결합니다</em></div>
              <div className="evidence-list">
                {news.evidence.map((item, evidenceIndex) => (
                  <article className="evidence-card" key={`${news.id}-${evidenceIndex}`}>
                    <div className="evidence-number">E{String(evidenceIndex + 1).padStart(2, "0")}</div>
                    <p>{item.prefix}<mark>{item.highlight}</mark>{item.suffix}</p>
                    <div className="evidence-links"><span>LINKED EDGES</span>{item.relationIds.map((id) => <b key={id}>{id.toUpperCase()}</b>)}</div>
                  </article>
                ))}
              </div>

              <div className="article-disclaimer">
                {news.articleExtraction?.status === "extracted"
                  ? "뉴스 검색 API는 본문을 제공하지 않아 공개 원문 페이지에서 본문을 추출했습니다. 반복 요청을 줄이기 위해 서버 캐시를 사용할 수 있으며, 사실관계와 이용 조건은 위 원문 링크에서 확인해 주세요."
                  : news.articleExtraction?.status === "failed"
                    ? "원문 사이트의 접근 정책 또는 문서 구조로 본문 자동 추출에 실패해 검색 API의 제공 요약을 표시합니다. 사실관계는 위 원문 링크에서 확인해 주세요."
                    : "뉴스 API/RSS가 제공한 기사 제목·요약·발행정보를 표시합니다. 사실관계는 위 원문 링크에서 확인할 수 있고 리스크·전이 점수는 프로토타입 모델의 추정치입니다."}
              </div>
            </section>

            <aside className="intelligence-column">
              <section className="intelligence-card confidence-card">
                <div className="intel-heading"><div><small>EXTRACTION QUALITY</small><b>분석 신뢰도</b></div><span>MODEL v0.3</span></div>
                <div className="confidence-content">
                  <div className="confidence-ring" style={{ "--confidence": `${news.confidence}%` } as CSSProperties}><b>{news.confidence}</b><small>/100</small></div>
                  <div><b>{news.confidence >= 90 ? "HIGH CONFIDENCE" : "REVIEWED"}</b><p>NER·관계 문맥·출처 신뢰도를 결합한 점수</p></div>
                </div>
              </section>

              <section className="intelligence-card">
                <div className="intel-heading"><div><small>KNOWLEDGE TRIPLES</small><b>기사 근거 추출 관계</b></div><span>{reportedRelations.length} FOUND</span></div>
                <div className="triple-list">
                  {reportedRelations.map((relation, relationIndex) => (
                    <div className="triple-row" key={`${relation.source}-${relation.target}-${relationIndex}`}>
                      <div><span>S</span><b>{relation.source}</b></div>
                      <div className="relation-arrow"><small>{relation.relation}</small><i>→</i></div>
                      <div><span>O</span><b>{relation.target}</b></div>
                      <em>{relation.confidence}%</em>
                    </div>
                  ))}
                </div>
              </section>

              <section className="intelligence-card">
                <div className="intel-heading"><div><small>NAMED ENTITIES</small><b>인식 엔티티</b></div><span>{news.entities.length} ENTITIES</span></div>
                <div className="entity-chip-list">
                  {news.entities.map((entity) => {
                    const companyRole = entity.type === "COMPANY" ? companyRoleFor(news, entity.name) : undefined;
                    const roleLabel = companyRole?.role === "affected" ? "영향 대상" : companyRole?.role === "source" ? "분석 출처" : companyRole?.role === "mentioned" ? "단순 언급" : entity.type;
                    return <span className={companyRole ? `role-${companyRole.role}` : ""} key={`${entity.type}-${entity.name}`}><i>{entity.type.slice(0, 2)}</i>{entity.name}<small>{roleLabel}</small></span>;
                  })}
                </div>
              </section>

              {news.companyRoleAnalysis && <section className="intelligence-card role-analysis-card">
                <div className="intel-heading"><div><small>LOCAL AI ROLE FILTER</small><b>기업 문맥 역할</b></div><span>{news.companyRoleAnalysis.provider === "local-qwen3-webgpu" ? "LOCAL GPU" : "CONTEXT RULE"}</span></div>
                <div className="company-role-list">
                  {news.companyRoleAnalysis.companies.map((company) => (
                    <div key={company.name}><b>{company.name}</b><span className={`role-badge ${company.role}`}>{company.role === "affected" ? "영향 대상" : company.role === "source" ? "분석 출처" : "단순 언급"}</span><small>{company.reason ?? `${Math.round(company.confidence * 100)}% 신뢰도`}</small></div>
                  ))}
                </div>
                <p className="role-analysis-note">분석 출처와 단순 언급 기업은 기사 근거에는 남기되 리스크 관계지도에서는 제외합니다.</p>
              </section>}

              <section className="intelligence-card affected-card">
                <div className="intel-heading"><div><small>IMPACT MAPPING</small><b>관련 영향 기업</b></div><span>RISK SCORE</span></div>
                <div className="news-affected-list">
                  {news.affected.map((company, companyIndex) => (
                    <button key={company.nodeId} onClick={() => onSelectCompany(company.nodeId)}>
                      <CompanyLogo node={knownCompanyById.get(company.nodeId)} nodeId={company.nodeId} label={company.label} className="affected-company-logo" />
                      <div><b>{company.label}</b><small>{company.reason}</small></div>
                      <em aria-label={`${companyIndex + 1}순위 · 리스크 ${company.score}`}>{company.score}</em><i>그래프 보기 →</i>
                    </button>
                  ))}
                </div>
              </section>
            </aside>
          </div>
        </div>

        <footer className="news-drawer-footer">
          <span>{String(index + 1).padStart(2, "0")} / {String(total).padStart(2, "0")}</span>
          <div>
            <button onClick={() => onNavigate(-1)} aria-label="이전 뉴스">← 이전 뉴스</button>
            <button onClick={() => onNavigate(1)} aria-label="다음 뉴스">다음 뉴스 →</button>
          </div>
        </footer>
      </article>
    </div>
  );
}

const companySearchExamples = ["SK하이닉스", "삼성전자", "LG에너지솔루션", "포스코퓨처엠", "현대자동차", "기아", "ASML", "NVIDIA"];

export default function RiskDashboard({ initialView = "news" }: { initialView?: DashboardView }) {
  const [scenarioId, setScenarioId] = useState(scenarios[0].id);
  const [selectedId, setSelectedId] = useState(articleRootId(fallbackNews[0].id));
  const [mode, setMode] = useState<GraphMode>("news");
  const [progress, setProgress] = useState(1);
  const [isRunning, setIsRunning] = useState(false);
  const [notice, setNotice] = useState("");
  const [activeNewsId, setActiveNewsId] = useState<string | null>(null);
  const [selectedNewsId, setSelectedNewsId] = useState<string | null>(null);
  const [liveFeed, setLiveFeed] = useState<LiveFeed | null>(null);
  const [feedStatus, setFeedStatus] = useState<FeedStatus>("loading");
  const [newsFilter, setNewsFilter] = useState<NewsFilter>("all");
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [companyQuery, setCompanyQuery] = useState("");
  const [searchedCompany, setSearchedCompany] = useState("");
  const [searchError, setSearchError] = useState("");
  const [companyRoleResults, setCompanyRoleResults] = useState<Record<string, CompanyRoleAnalysis>>({});
  const [localAiState, setLocalAiState] = useState<LocalAiState>("idle");
  const [localAiProgress, setLocalAiProgress] = useState(0);
  const [roleWorkerReady, setRoleWorkerReady] = useState(false);
  const companySearchInputRef = useRef<HTMLInputElement>(null);
  const liveFeedRef = useRef<LiveFeed | null>(null);
  const roleWorkerRef = useRef<Worker | null>(null);
  const submittedRoleJobsRef = useRef(new Map<string, string>());
  const initialRefreshKeyRef = useRef("");

  const scenario = useMemo(() => scenarios.find((item) => item.id === scenarioId) ?? scenarios[0], [scenarioId]);
  const isInitialNewsLoading = feedStatus === "loading" && !liveFeed;
  const rawDisplayNews = useMemo(
    () => liveFeed?.items.length ? liveFeed.items : [],
    [liveFeed],
  );
  const displayNews = useMemo(() => rawDisplayNews.map((news) => {
    const analysis = companyRoleResults[news.id] ?? inferTargetPriceRoles(news);
    return applyCompanyRolesToNews(news, analysis);
  }), [companyRoleResults, rawDisplayNews]);
  const filteredNews = newsFilter === "all" ? displayNews : displayNews.filter((item) => item.scenarioId === newsFilter);
  const activeNews = displayNews.find((item) => item.id === activeNewsId) ?? displayNews[0] ?? null;
  const categoryNews = useMemo(() => displayNews.filter((news) => news.scenarioId === scenario.id), [displayNews, scenario.id]);
  const multiNewsScenario = useMemo(() => buildMultiNewsScenario(scenario, categoryNews), [categoryNews, scenario]);
  const articleNewsScenario = useMemo(() => activeNews ? buildArticleNewsScenario(scenario, activeNews) : multiNewsScenario, [activeNews, multiNewsScenario, scenario]);
  const graphScenario = mode === "aggregate" || mode === "ownership" ? multiNewsScenario : articleNewsScenario;
  const risk = useMemo(() => diffuseRisk(graphScenario), [graphScenario]);
  const ranked = useMemo(() => rankCompanies(graphScenario), [graphScenario]);
  const path = useMemo(() => strongestPath(graphScenario), [graphScenario]);
  const controls = useMemo(() => controllingEntities(graphScenario), [graphScenario]);
  const byId = useMemo(() => new Map(graphScenario.nodes.map((node) => [node.id, node])), [graphScenario]);
  const selectedNode = byId.get(selectedId) ?? graphScenario.nodes[0];
  const selectedRisk = risk.get(selectedNode.id);
  const connectedEdges = graphScenario.edges.filter((edge) => edge.source === selectedNode.id || edge.target === selectedNode.id);
  const directArticleCompanyIds = new Set(activeNews?.affected.filter((company) => isDirectNewsCompany(activeNews, company.label)).map((company) => company.nodeId) ?? []);
  const directArticleCompanies = directArticleCompanyIds.size;
  const indirectArticleCompanies = articleNewsScenario.nodes.filter((node) => node.kind === "company" && !directArticleCompanyIds.has(node.id)).length;
  const reportedRelationCount = activeNews?.relations.filter((relation) => relation.basis !== "modeled").length ?? 0;
  const selectedNewsIndex = filteredNews.findIndex((item) => item.id === selectedNewsId);
  const selectedNews = selectedNewsIndex >= 0 ? filteredNews[selectedNewsIndex] : null;
  const collectedTime = liveFeed ? new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(liveFeed.collectedAt)) : null;
  const feedNotice = feedStatus === "loading"
    ? searchedCompany ? `${searchedCompany} 최신 뉴스를 검색하고 있습니다…` : "최신 금융 뉴스를 수집하고 있습니다…"
    : feedStatus === "live"
      ? `${searchedCompany ? `${searchedCompany} 검색 결과 ${displayNews.length}건` : `${liveFeed?.provider ?? "뉴스 피드"} 수집 완료`} · ${collectedTime} KST · 5분 자동 갱신`
      : "실시간 피드 연결 실패 · 저장된 뉴스가 아직 없어 잠시 후 다시 시도합니다.";
  const aiAnalyzedNews = rawDisplayNews.filter((news) => companyRoleResults[news.id]).length;
  const localAiNotice = localAiState === "loading"
    ? `로컬 AI 모델 받는 중 ${localAiProgress}% · 최초 1회 후 브라우저 캐시`
    : localAiState === "analyzing"
      ? `로컬 GPU로 기업 역할 판별 중 · ${aiAnalyzedNews}/${Math.min(rawDisplayNews.length, 12)}건`
      : localAiState === "ready"
        ? `로컬 AI · Qwen3 0.6B · GPU 역할 판별 ${aiAnalyzedNews}건 완료`
        : localAiState === "unsupported"
          ? "WebGPU 미지원 · 고정밀 목표주가 문맥 규칙 적용 중"
          : localAiState === "error"
            ? "로컬 AI를 불러오지 못해 고정밀 문맥 규칙으로 안전하게 대체했습니다"
            : "로컬 AI 준비 중";
  const companyCatalog = useMemo(() => {
    const nodes = new Map<string, RiskNode>();
    scenarios.flatMap((item) => item.nodes).filter((node) => node.kind === "company").forEach((node) => nodes.set(node.id, node));
    displayNews.flatMap((news) => news.affected).forEach((company) => {
      if (!nodes.has(company.nodeId)) nodes.set(company.nodeId, { id: company.nodeId, label: company.label, kind: "company", code: company.code ?? companySymbolByNodeId[company.nodeId] ?? "NEWS", industry: company.industry ?? categoryLabel[(displayNews.find((news) => news.affected.some((item) => item.nodeId === company.nodeId))?.scenarioId ?? "all") as NewsFilter], description: company.reason });
    });
    return [...nodes.values()].map((node) => {
      const relationCount = scenarios.reduce((total, item) => total + item.edges.filter((edge) => edge.source === node.id || edge.target === node.id).length, 0);
      const relatedNews = displayNews.filter((news) => news.affected.some((company) => company.nodeId === node.id));
      const riskScore = Math.max(0, ...relatedNews.flatMap((news) => news.affected.filter((company) => company.nodeId === node.id).map((company) => company.score)));
      return { node, relationCount, newsCount: relatedNews.length, riskScore };
    }).sort((a, b) => b.riskScore - a.riskScore || b.relationCount - a.relationCount || a.node.label.localeCompare(b.node.label, "ko"));
  }, [displayNews]);
  const graphNodeCount = new Set(scenarios.flatMap((item) => item.nodes.map((node) => node.id))).size;
  const graphEdgeCount = scenarios.reduce((total, item) => total + item.edges.length, 0);
  const reportedEdgeCount = scenarios.reduce((total, item) => total + item.edges.filter((edge) => edge.basis !== "modeled").length, 0);
  const activePortalNav = initialView === "news"
    ? "news"
    : initialView === "companies" || initialView === "data"
      ? "companies"
      : "home";
  const pageTitle = initialView === "news"
    ? isInitialNewsLoading ? "최신 금융 뉴스를 불러오는 중입니다" : activeNews?.title ?? "실시간 금융 뉴스"
    : initialView === "network"
      ? "뉴스 기반 기업 관계 지도"
      : initialView === "companies"
        ? "연결 기업 데이터"
        : "데이터 수집 및 품질 현황";
  const pageDescription = initialView === "news"
    ? activeNews ? `${activeNews.source} · ${activeNews.publishedAt} · 기사에 직접 언급된 기업과 핵심 내용을 확인합니다.` : "NAVER API HUB에서 수집한 최신 금융 뉴스를 확인합니다."
    : initialView === "network"
      ? `선택 뉴스에서 직접 언급 ${directArticleCompanies}개사와 간접 전이 ${indirectArticleCompanies}개사를 연결해 충격 경로를 분석합니다.`
      : initialView === "companies"
        ? `뉴스와 공급망·지분 그래프에서 인식된 ${companyCatalog.length}개 기업의 연결 관계와 뉴스 노출도를 조회합니다.`
        : `뉴스 ${displayNews.length}건과 기준 그래프 ${graphNodeCount}개 노드·${graphEdgeCount}개 관계의 수집 상태를 확인합니다.`;

  useEffect(() => {
    const controller = new AbortController();
    if (!initialRefreshKeyRef.current) {
      initialRefreshKeyRef.current = `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
    }
    const refreshKey = refreshNonce === 0
      ? initialRefreshKeyRef.current
      : `${refreshNonce}-${Date.now()}`;
    const params = new URLSearchParams({ scenario: "all", refresh: refreshKey, includeBody: "1", bodyLimit: "6" });
    if (searchedCompany) params.set("company", searchedCompany);
    const requestKey = `all:${searchedCompany.toLocaleLowerCase("ko-KR")}`;
    const currentKey = liveFeedRef.current
      ? `${liveFeedRef.current.requested ?? "all"}:${(liveFeedRef.current.companyQuery ?? "").toLocaleLowerCase("ko-KR")}`
      : "";

    const acceptFeed = (feed: LiveFeed) => {
        if (!feed.items?.length) throw new Error("empty_feed");
        const previousFeed = liveFeedRef.current;
        const sameFeed = previousFeed
          && (previousFeed.requested ?? "all") === (feed.requested ?? "all")
          && (previousFeed.companyQuery ?? "") === (feed.companyQuery ?? "");
        const nextFeed = {
          ...feed,
          items: sameFeed ? mergeNewsSignalsNewestFirst(feed.items, previousFeed.items, 60) : feed.items,
        };
        liveFeedRef.current = nextFeed;
        setLiveFeed(nextFeed);
        setSearchError("");
        const first = nextFeed.items[0];
        setActiveNewsId((current) => current && nextFeed.items.some((item) => item.id === current) ? current : first.id);
        if (first.scenarioId) {
          const next = scenarios.find((item) => item.id === first.scenarioId) ?? scenarios[0];
          const normalizedSearch = normalizeEntityName(searchedCompany);
          const searchedNode = normalizedSearch
            ? first.affected.find((company) => {
                const normalizedLabel = normalizeEntityName(company.label);
                return normalizedLabel === normalizedSearch || normalizedLabel.includes(normalizedSearch) || normalizedSearch.includes(normalizedLabel);
              })
            : null;
          setScenarioId(next.id);
          setSelectedId(searchedNode?.nodeId ?? articleRootId(first.id));
        }
        setFeedStatus("live");
    };

    const fetchFeed = async (feedParams: URLSearchParams) => {
      const response = await fetch(`/api/news?${feedParams.toString()}`, { signal: controller.signal, cache: "no-store" });
      if (!response.ok) {
        const failure = await response.json().catch(() => null) as { message?: string } | null;
        throw new Error(failure?.message ?? `news_feed_${response.status}`);
      }
      return response.json() as Promise<LiveFeed>;
    };

    const synchronize = async () => {
      let historyLoaded = currentKey === requestKey;
      if (!historyLoaded) {
        const historyParams = new URLSearchParams(params);
        historyParams.set("history", "1");
        historyParams.delete("includeBody");
        historyParams.delete("bodyLimit");
        try {
          acceptFeed(await fetchFeed(historyParams));
          historyLoaded = true;
        } catch (error: unknown) {
          if (error instanceof DOMException && error.name === "AbortError") return;
        }
      }
      try {
        acceptFeed(await fetchFeed(params));
      } catch (error: unknown) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (searchedCompany) setSearchError(error instanceof Error ? error.message : "기업 뉴스를 불러오지 못했습니다.");
        if (historyLoaded || liveFeedRef.current) {
          setFeedStatus("live");
          setNotice("DB에 저장된 최신 뉴스 목록을 표시하고 있습니다. 신규 기사 동기화는 다음 갱신에서 다시 시도합니다.");
        } else {
          setFeedStatus("fallback");
        }
      }
    };
    void synchronize();
    return () => controller.abort();
  }, [refreshNonce, searchedCompany]);

  useEffect(() => {
    if (initialView !== "news" && initialView !== "network") return;
    if (!("gpu" in navigator)) {
      window.queueMicrotask(() => setLocalAiState("unsupported"));
      return;
    }
    let worker: Worker;
    try {
      worker = new Worker(newsRoleWorkerUrl, { type: "module" });
    } catch {
      window.queueMicrotask(() => setLocalAiState("error"));
      return;
    }
    roleWorkerRef.current = worker;
    worker.addEventListener("error", () => setLocalAiState("error"));
    worker.addEventListener("message", (event: MessageEvent<{
      type: "status" | "result";
      state?: LocalAiState;
      progress?: number;
      id?: string;
      companies?: CompanyRoleAnalysis["companies"] | null;
    }>) => {
      const message = event.data;
      if (message.type === "status" && message.state) {
        if (message.state === "idle") setRoleWorkerReady(true);
        setLocalAiState(message.state);
        if (typeof message.progress === "number") setLocalAiProgress(message.progress);
        return;
      }
      if (message.type === "result" && message.id && message.companies?.length) {
        setCompanyRoleResults((current) => ({
          ...current,
          [message.id!]: { provider: "local-qwen3-webgpu", modelId: NEWS_ROLE_MODEL_ID, companies: message.companies! },
        }));
      }
    });
    return () => {
      worker.terminate();
      roleWorkerRef.current = null;
    };
  }, [initialView]);

  useEffect(() => {
    const worker = roleWorkerRef.current;
    if (!roleWorkerReady || !worker) return;
    rawDisplayNews.slice(0, 12).forEach((news) => {
      const candidates = news.affected.map((company) => company.label);
      if (!candidates.length) return;
      const fingerprint = `${news.title}|${news.summary}|${candidates.join("|")}`;
      if (submittedRoleJobsRef.current.get(news.id) === fingerprint) return;
      submittedRoleJobsRef.current.set(news.id, fingerprint);
      worker.postMessage({ type: "analyze", id: news.id, title: news.title, summary: news.summary, candidates });
    });
  }, [rawDisplayNews, roleWorkerReady]);

  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (event.key !== "/" || target?.matches("input, textarea, select, [contenteditable='true']")) return;
      event.preventDefault();
      companySearchInputRef.current?.focus();
    };
    window.addEventListener("keydown", focusSearch);
    const params = new URLSearchParams(window.location.search);
    const requestedCompany = params.get("company")?.trim().replace(/\s+/g, " ") ?? "";
    if ((initialView === "news" || initialView === "network") && requestedCompany.length >= 2) {
      window.requestAnimationFrame(() => {
        setCompanyQuery(requestedCompany);
        setSearchedCompany(requestedCompany);
        setFeedStatus("loading");
      });
    }
    if (params.get("focus") === "search") {
      window.requestAnimationFrame(() => companySearchInputRef.current?.focus());
    }
    return () => window.removeEventListener("keydown", focusSearch);
  }, [initialView]);

  useEffect(() => {
    if (feedStatus !== "live" || !liveFeed) return;
    const refreshSeconds = Math.max(60, Math.min(liveFeed.refreshAfterSeconds ?? 300, 900));
    const timer = window.setTimeout(() => {
      setFeedStatus("loading");
      setRefreshNonce((value) => value + 1);
    }, refreshSeconds * 1000);
    return () => window.clearTimeout(timer);
  }, [feedStatus, liveFeed]);

  useEffect(() => {
    if (!isRunning) return;
    const timer = window.setInterval(() => {
      setProgress((value) => {
        const next = value + .035;
        if (next >= 1) {
          window.clearInterval(timer);
          setIsRunning(false);
          setNotice("전이 분석이 완료되었습니다. 상위 영향 기업과 핵심 경로가 갱신되었습니다.");
          return 1;
        }
        return next;
      });
    }, 55);
    return () => window.clearInterval(timer);
  }, [isRunning]);

  useEffect(() => {
    if (!selectedNews) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const closeOnEscape = (event: KeyboardEvent) => event.key === "Escape" && setSelectedNewsId(null);
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [selectedNews]);

  const activateNews = (news: NewsSignal, openDetail = false) => {
    const next = scenarios.find((item) => item.id === news.scenarioId) ?? scenarios[0];
    setActiveNewsId(news.id);
    setScenarioId(next.id);
    setSelectedId(articleRootId(news.id));
    setMode("news");
    setProgress(1);
    setNotice(`선택한 실제 뉴스 1건의 직접 관계와 간접 전이 지도를 열었습니다.`);
    setSelectedNewsId(openDetail ? news.id : null);
  };

  const refreshFeed = () => {
    setSelectedNewsId(null);
    setFeedStatus("loading");
    setRefreshNonce((value) => value + 1);
  };

  const searchCompanyNews = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const query = companyQuery.trim().replace(/\s+/g, " ");
    if (query.length < 2) {
      setSearchError("기업명을 두 글자 이상 입력해 주세요.");
      companySearchInputRef.current?.focus();
      return;
    }
    if (initialView !== "news" && initialView !== "network") {
      window.location.assign(`/news/?company=${encodeURIComponent(query)}`);
      return;
    }
    setSearchError("");
    setSelectedNewsId(null);
    setNewsFilter("all");
    setFeedStatus("loading");
    setSearchedCompany(query);
    setRefreshNonce((value) => value + 1);
    if (initialView === "network") {
      window.history.replaceState(null, "", `/network/?company=${encodeURIComponent(query)}`);
      setNotice(`‘${query}’을 중심으로 최신 뉴스 관계망을 구성하고 있습니다.`);
    }
  };

  const clearCompanySearch = () => {
    setCompanyQuery("");
    setSearchedCompany("");
    setSearchError("");
    setSelectedNewsId(null);
    setNewsFilter("all");
    setFeedStatus("loading");
    setRefreshNonce((value) => value + 1);
  };

  const runSimulation = () => {
    setSelectedId(graphScenario.shockSource);
    setProgress(0);
    setNotice("가중 그래프 확산을 실행하고 있습니다…");
    setIsRunning(true);
  };

  const navigateNews = (direction: -1 | 1) => {
    const nextIndex = (selectedNewsIndex + direction + filteredNews.length) % filteredNews.length;
    activateNews(filteredNews[nextIndex], true);
  };

  return (
    <main
      className="shell galaxy-shell"
      style={{
        "--portal-line": "var(--galaxy-line)",
        "--portal-muted": "var(--galaxy-muted)",
        "--portal-brand": "var(--galaxy-cyan)",
      } as CSSProperties}
    >
      <section className="workspace">
        <PortalNav active={activePortalNav} />
        <div
          className="dashboard-tools"
          role="region"
          aria-label="페이지 도구"
          style={{
            minHeight: 52,
            padding: "7px 24px",
            display: "flex",
            alignItems: "center",
            justifyContent: "flex-end",
            borderBottom: "1px solid var(--galaxy-line)",
            background: "rgba(7, 10, 18, .72)",
          }}
        >
          <div className="header-actions">
            <form className="navbar-company-search" role="search" onSubmit={searchCompanyNews}>
              <span aria-hidden="true">⌕</span>
              <input
                ref={companySearchInputRef}
                value={companyQuery}
                onChange={(event) => setCompanyQuery(event.target.value)}
                aria-label="NAVBAR 기업 뉴스 검색"
                placeholder="기업 뉴스 검색"
                minLength={2}
                maxLength={40}
                autoComplete="off"
              />
              <kbd>/</kbd>
              <button type="submit" disabled={initialView === "news" && feedStatus === "loading"}>검색</button>
            </form>
            <div className={`live-badge ${feedStatus}`}><span /> {feedStatus === "loading" ? "연결 중" : feedStatus === "live" ? `${collectedTime} 기준` : "대체 데이터"}</div>
          </div>
        </div>

        <div className={`content view-${initialView}`}>
          <section className="hero-strip">
            <div className="hero-copy">
              <div className="hero-meta">
                <span className={`status-pill ${scenario.severity.toLowerCase()}`}>{initialView === "news" ? "실시간 뉴스" : initialView === "network" ? "그래프 분석" : initialView === "companies" ? "기업 데이터" : "데이터 상태"}</span>
                <span>{initialView === "data" ? liveFeed?.provider ?? "데이터 파이프라인" : activeNews?.tag ?? "최신 뉴스"} · {scenario.shortLabel}</span>
              </div>
              <h2>{pageTitle}</h2>
              <p>{pageDescription}</p>
            </div>
            <div className="scenario-actions">
              {activeNews && initialView !== "data" && <button className="hero-refresh-button" onClick={() => setSelectedNewsId(activeNews.id)}>기사 근거 상세 보기 ↗</button>}
              {(initialView === "news" || initialView === "data") && <button className="hero-refresh-button" onClick={refreshFeed} disabled={feedStatus === "loading"}>{feedStatus === "loading" ? "뉴스 동기화 중…" : "최신 뉴스 새로고침 ↻"}</button>}
              {initialView === "network" && <button className="simulate-button" onClick={runSimulation} disabled={isRunning}>
                {isRunning ? `전이 계산 중 ${Math.round(progress * 100)}%` : "선택 뉴스 전이 분석"}<span>→</span>
              </button>}
            </div>
          </section>

          <div className="status-line" role="status" aria-live="polite">
            <span className={isRunning || feedStatus === "loading" || localAiState === "loading" || localAiState === "analyzing" ? "pulse" : ""} />{notice || feedNotice}<em className={`local-ai-status ${localAiState}`}>{localAiNotice}</em>
          </div>

          {initialView === "news" && <section className="panel live-news-panel" aria-label="실시간 금융 뉴스">
            <div className="live-news-header">
              <div><p className="section-kicker">실시간 수집</p><h3>최신 금융 뉴스</h3></div>
              <div className="event-panel-meta">
                <small>{feedStatus === "live" ? `${liveFeed?.provider} · ${collectedTime} KST 수집 · 5분 자동 갱신` : feedStatus === "loading" ? "DB 뉴스 불러오는 중" : "뉴스 연결 재시도 필요"}</small>
                <button className="refresh-feed" onClick={refreshFeed} disabled={feedStatus === "loading"}>{feedStatus === "loading" ? "수집 중…" : "새로고침 ↻"}</button>
                <span className="signal-count">{displayNews.length} NEWS</span>
              </div>
            </div>
            <form className="company-news-search" role="search" onSubmit={searchCompanyNews}>
              <label htmlFor="company-news-query">기업 뉴스 검색</label>
              <div className="company-search-field">
                <span aria-hidden="true">⌕</span>
                <input
                  id="company-news-query"
                  name="company"
                  value={companyQuery}
                  onChange={(event) => setCompanyQuery(event.target.value)}
                  placeholder="기업명을 입력하세요. 예: SK하이닉스"
                  list="company-search-examples"
                  minLength={2}
                  maxLength={40}
                  autoComplete="off"
                />
                <datalist id="company-search-examples">
                  {companySearchExamples.map((company) => <option value={company} key={company} />)}
                </datalist>
                {searchedCompany && <button className="company-search-reset" type="button" onClick={clearCompanySearch}>전체 뉴스</button>}
                <button className="company-search-submit" type="submit" disabled={feedStatus === "loading"}>{feedStatus === "loading" && searchedCompany ? "검색 중…" : "검색"}</button>
              </div>
              <p className={searchError ? "search-feedback error" : "search-feedback"} role={searchError ? "alert" : "status"}>
                {searchError || (searchedCompany ? `‘${liveFeed?.companyQuery ?? searchedCompany}’ 최신 뉴스 ${displayNews.length}건을 표시하고 있습니다.` : "기업명을 검색하면 네이버의 최신 기사와 해당 기업 관계 지도를 함께 불러옵니다.")}
              </p>
            </form>
            <div className="news-filter-tabs" role="tablist" aria-label="뉴스 카테고리">
              {(Object.keys(categoryLabel) as NewsFilter[]).map((filter) => (
                <button role="tab" aria-selected={newsFilter === filter} className={newsFilter === filter ? "active" : ""} key={filter} onClick={() => setNewsFilter(filter)}>
                  {categoryLabel[filter]} <span>{filter === "all" ? displayNews.length : displayNews.filter((item) => item.scenarioId === filter).length}</span>
                </button>
              ))}
            </div>
            <div className="news-stream">
              {isInitialNewsLoading && (
                <div className="news-loading-state" role="status">
                  <span className="news-loading-spinner" aria-hidden="true" />
                  <b>방금 올라온 금융 뉴스를 불러오고 있습니다</b>
                  <p>최신 기사 수집이 끝나면 자동으로 표시됩니다.</p>
                </div>
              )}
              {filteredNews.map((item) => (
                <button className={`news-stream-row ${activeNewsId === item.id ? "active" : ""}`} key={item.id} onClick={() => activateNews(item)}>
                  <span className="news-stream-tag">{item.tag}</span>
                  <div className="news-stream-copy"><b>{item.title}</b><small>{item.source} · {item.time} · {item.sourceType}</small><p>{item.summary}</p></div>
                  <div className="news-stream-impact"><small>연결 기업</small><b>{item.affected.length}</b><em>관계 지도 →</em></div>
                </button>
              ))}
              {!isInitialNewsLoading && !filteredNews.length && <div className="empty-news">저장된 최신 뉴스가 없습니다. 잠시 후 새로고침해 주세요.</div>}
            </div>
            <div className="live-news-note">
              <span>{liveFeed?.apiKeyConfigured ? `NAVER API HUB · 국내 ${liveFeed.companyRegistry?.domesticCount ?? 0}개 + 해외 ${liveFeed.companyRegistry?.foreignCount ?? 0}개 기업 인식 · 5분 자동 갱신` : "NAVER API HUB 키 대기 · 현재 RSS 자동 대체"}</span>
              <p>{localAiNotice}. 목표주가·투자의견을 낸 증권사는 분석 출처로 남기고 실제 영향 대상에서 제외합니다.</p>
            </div>
          </section>}

          {(initialView === "news" || initialView === "network") && <section className="metric-grid" aria-label="핵심 리스크 지표">
            <article className="metric-card">
              <div className="metric-label"><span>01</span> 선택 뉴스 연결 기업</div>
              <div className="metric-value">{activeNews?.affected.length ?? 0}<small>개사</small></div>
              <p>실제 기사 <b>1건</b> 기준 관계 지도</p>
            </article>
            <article className="metric-card">
              <div className="metric-label"><span>02</span> 직접 / 간접 연결 <em>LIVE</em></div>
              <div className="metric-value">{directArticleCompanies}<small> / {indirectArticleCompanies}</small></div>
              <p>기사 언급 / 공급망 모델 전이</p>
            </article>
            <article className="metric-card">
              <div className="metric-label"><span>03</span> 기사 추출 관계</div>
              <div className="metric-value">{reportedRelationCount}<small>TRIPLE</small></div>
              <p>주어 → 관계 → 목적어</p>
            </article>
            <article className="metric-card">
              <div className="metric-label"><span>04</span> NLP 신뢰도</div>
              <div className="score-row"><div className="metric-value">{activeNews?.confidence ?? 0}<small>/100</small></div><div className="mini-ring" style={{ "--score": `${activeNews?.confidence ?? 0}%` } as CSSProperties}>{activeNews?.confidence ?? 0}</div></div>
              <p>엔티티·관계 문맥·출처 신뢰도</p>
            </article>
          </section>}

          {(initialView === "news" || initialView === "network") && <section className="analysis-grid">
            <article className="panel network-panel">
              <div className="panel-heading graph-heading">
                <div><p className="section-kicker">선택한 뉴스</p><h3>기업 관계 지도</h3></div>
                <div className="graph-tabs" role="tablist" aria-label="그래프 보기 방식">
                  <button className={mode === "news" ? "active" : ""} onClick={() => { setMode("news"); if (activeNews) setSelectedId(articleRootId(activeNews.id)); }}>선택 뉴스 지도</button>
                  <button className={mode === "aggregate" ? "active" : ""} onClick={() => { setMode("aggregate"); setSelectedId(newsRootId(scenario.id)); }}>카테고리 통합망</button>
                  <button className={mode === "path" ? "active" : ""} onClick={() => setMode("path")}>핵심 경로</button>
                  <button className={mode === "ownership" ? "active" : ""} onClick={() => setMode("ownership")}>지분 관계</button>
                </div>
              </div>
              {activeNews && (
                <div className="graph-news-source">
                  <div><span>SELECTED LIVE NEWS</span><b>{activeNews.title}</b><small>{activeNews.source} · {activeNews.publishedAt}</small></div>
                  <div className="graph-source-stats"><span><b>{directArticleCompanies}</b> 직접</span><span><b>{indirectArticleCompanies}</b> 간접</span><span><b>{reportedRelationCount}</b> 트리플</span></div>
                  <button onClick={() => setSelectedNewsId(activeNews.id)}>근거 상세 ↗</button>
                </div>
              )}
              <div className="graph-stage three-graph-stage">
                <ThreeNetworkGraph
                  scenario={graphScenario}
                  risk={risk}
                  selectedId={selectedNode.id}
                  path={path}
                  progress={progress}
                  mode={mode}
                  onSelect={setSelectedId}
                  logoPath={companyLogoPath}
                  fallback={<NetworkGraph scenario={graphScenario} risk={risk} selectedId={selectedNode.id} path={path} progress={progress} mode={mode} onSelect={setSelectedId} />}
                />
                <div className="graph-key">
                  <span><i className="key-origin" /> 선택 뉴스</span><span><i className="key-direct" /> 직접 언급</span><span><i className="key-triple" /> 추출 관계</span><span><i className="key-model" /> 간접 전이</span>
                </div>
              </div>
              <div className="graph-caption">
                <span>관계 가중치</span><i className="line thin"/><i className="line medium"/><i className="line thick"/><b>높음</b>
                <em>{graphScenario.nodes.length} NODES · {graphScenario.edges.length} EDGES · {mode === "aggregate" || mode === "ownership" ? `${categoryNews.length} NEWS` : "1 SELECTED NEWS"}</em>
              </div>
            </article>

            <article className="panel exposure-panel">
              <div className="panel-heading">
                <div><p className="section-kicker">영향 분석</p><h3>영향도가 큰 기업</h3></div>
                <span className="model-tag">점수순</span>
              </div>
              <div className="ranking-list">
                {ranked.slice(0, 6).map((item, index) => (
                  <button className={`rank-row ${selectedNode.id === item.node.id ? "selected" : ""}`} key={item.node.id} onClick={() => setSelectedId(item.node.id)}>
                    <span className="rank">{String(index + 1).padStart(2, "0")}</span>
                    <CompanyLogo node={item.node} className="ranking-company-logo" />
                    <span className="company-name"><b>{item.node.label}</b><small>{item.node.code === "비상장" ? "PRIVATE" : `${item.node.code?.match(/^\d{6}$/) ? "KRX" : "GLOBAL"} · ${item.node.code}`}</small></span>
                    <span className="risk-bar"><i style={{ width: `${item.score}%` }} /></span>
                    <span className="rank-score"><b>{item.score}</b><small>{item.depth} HOP</small></span>
                  </button>
                ))}
              </div>
              <div className="ranking-note"><span>직접 영향</span><span>간접 영향</span><span>점수 100 기준</span></div>
            </article>
          </section>}

          {(initialView === "news" || initialView === "network") && <section className="insight-grid">
            <article className="panel path-panel">
              <div className="panel-heading compact"><div><p className="section-kicker">주요 경로</p><h3>가장 강한 전이 경로</h3></div><span className="path-strength">{Math.max(41, Math.round(graphScenario.severityScore * .71))}% 전이강도</span></div>
              <div className="path-flow">
                {path.map((nodeId, index) => {
                  const node = byId.get(nodeId)!;
                  return <div className="path-step" key={nodeId}><span className={index === 0 ? "origin" : ""}>{index}</span><div><small>{kindLabel[node.kind]}</small><b>{node.label}</b></div>{index < path.length - 1 && <i>→</i>}</div>;
                })}
              </div>
              <div className="path-explanation">가중치 곱이 최대인 경로를 탐색해 가장 강한 충격 전이 사슬을 표시합니다.</div>
            </article>

            <article className="panel detail-panel">
              <div className="panel-heading compact"><div><p className="section-kicker">상세 정보</p><h3>선택한 항목</h3></div><span className={`entity-kind ${selectedNode.kind}`}>{kindLabel[selectedNode.kind]}</span></div>
              <div className="entity-main">
                <div className="entity-ident"><CompanyLogo node={selectedNode} className="entity-company-logo" /><div><h4>{selectedNode.label}</h4><p>{selectedNode.description}</p></div></div>
                <div className="entity-score"><b>{selectedRisk?.score ?? 0}</b><small>RISK SCORE</small></div>
              </div>
              <div className="entity-stats">
                <div><small>종목코드</small><b>{selectedNode.code ?? "N/A"}</b></div>
                <div><small>신용등급</small><b>{selectedNode.rating ?? "N/A"}</b></div>
                <div><small>연결 관계</small><b>{connectedEdges.length}</b></div>
                <div><small>전이 깊이</small><b>{selectedRisk ? `${selectedRisk.depth} HOP` : "N/A"}</b></div>
              </div>
              <div className="evidence-row">
                근거 <span>{connectedEdges[0]?.evidence ?? selectedNode.sourceLabel ?? "모델 이벤트 노드"}</span>
                {(connectedEdges[0]?.sourceUrl ?? selectedNode.sourceUrl) && <a href={connectedEdges[0]?.sourceUrl ?? selectedNode.sourceUrl} target="_blank" rel="noreferrer noopener">출처 ↗</a>}
                <em>{connectedEdges[0]?.basis === "modeled" ? "MODEL EST." : connectedEdges[0]?.dataAsOf ?? selectedNode.dataAsOf}</em>
              </div>
            </article>

          </section>}

          {initialView === "network" && scenario.id === "governance" && (
            <section className="rule-banner">
              <div><span>RULE INFERENCE</span><b>지배회사 관계 {controls.length}건 자동 추론</b></div>
              <p>{controls.map((item) => `${item.owner} → ${item.target} (${item.confidence}%)`).join("  ·  ") || "지배력 임계치 50%를 초과한 관계가 없습니다."}</p>
            </section>
          )}

          {initialView === "companies" && (
            <section className="panel directory-panel" aria-label="기업 데이터 목록">
              <div className="directory-heading">
                <div><p className="section-kicker">COMPANY DIRECTORY</p><h3>뉴스·관계망 연결 기업</h3><p>기사 노출도와 기준 그래프의 관계 수를 함께 표시합니다.</p></div>
                <span>{companyCatalog.length} COMPANIES</span>
              </div>
              <div className="company-table" role="table" aria-label="기업별 연결 현황">
                <div className="company-table-head" role="row"><span>기업</span><span>산업</span><span>최근 뉴스</span><span>연결 관계</span><span>노출 점수</span></div>
                {companyCatalog.map(({ node, relationCount, newsCount, riskScore }) => (
                  <a className="company-table-row" href="/network/" key={node.id} role="row">
                    <span><CompanyLogo node={node} className="directory-company-logo" /><b>{node.label}</b><small>{node.code ?? "NEWS"}</small></span>
                    <span>{node.industry ?? "분류 예정"}</span>
                    <span><b>{newsCount}</b>건</span>
                    <span><b>{relationCount}</b>개</span>
                    <span><strong>{riskScore}</strong><em><i style={{ width: `${riskScore}%` }} /></em></span>
                  </a>
                ))}
              </div>
            </section>
          )}

          {initialView === "data" && (
            <section className="data-page" aria-label="데이터 수집 현황">
              <div className="data-source-grid">
              <article className="panel source-status-card"><span className={`source-dot ${feedStatus}`} /><small>LIVE NEWS</small><h3>NAVER API HUB</h3><b>{displayNews.length}<em>건</em></b><p>{feedStatus === "live" ? `${collectedTime} KST 최신 수집 완료` : feedStatus === "loading" ? "DB 뉴스와 최신 뉴스를 동기화하고 있습니다." : "뉴스 연결을 다시 시도해 주세요."}</p></article>
                <article className="panel source-status-card"><span className="source-dot live" /><small>DISCLOSURE</small><h3>공시 기반 관계</h3><b>{reportedEdgeCount}<em>개</em></b><p>지분·공급망·원자재 관계의 관측 근거</p></article>
                <article className="panel source-status-card"><span className="source-dot live" /><small>KNOWLEDGE GRAPH</small><h3>그래프 엔진</h3><b>{graphNodeCount}<em>노드</em></b><p>{graphEdgeCount}개 관계를 가중 방향 그래프로 분석</p></article>
              </div>
              <article className="panel pipeline-panel">
                <div className="directory-heading"><div><p className="section-kicker">PIPELINE STATUS</p><h3>수집·추출·분석 파이프라인</h3></div><span>ALL SYSTEMS OPERATIONAL</span></div>
                <div className="pipeline-steps">
                  <div><i>01</i><span><small>COLLECT</small><b>뉴스·공시 수집</b><p>NAVER API HUB와 공시 스냅샷</p></span><em>정상</em></div>
                  <div><i>02</i><span><small>EXTRACT</small><b>기업·관계 추출</b><p>NER 및 관계 트리플 생성</p></span><em>정상</em></div>
                  <div><i>03</i><span><small>CONNECT</small><b>지식 그래프 연결</b><p>직접 관계와 모델 전이 분리</p></span><em>정상</em></div>
                  <div><i>04</i><span><small>ANALYZE</small><b>리스크 확산 분석</b><p>가중 경로와 기업 영향도 계산</p></span><em>정상</em></div>
                </div>
              </article>
            </section>
          )}

          <footer className="dashboard-footer">
            <div><b>COSMOS</b><span>Ontology-driven financial supply chain intelligence</span></div>
            <div className="pipeline-health"><span className="ok" /> DART ETL <span className="ok" /> NEWS NLP <span className="ok" /> GRAPH ENGINE</div>
            <p>NAVER API HUB 실제 뉴스 한 건마다 독립 관계 지도를 생성합니다. 직접 언급·추출 트리플은 기사 근거로, 공급망 간접 전이는 모델 추정치로 명확히 구분합니다.</p>
          </footer>
        </div>
      </section>
      {selectedNews && (
        <NewsDetail
          news={selectedNews}
          index={selectedNewsIndex}
          total={filteredNews.length}
          onClose={() => setSelectedNewsId(null)}
          onNavigate={navigateNews}
          onSelectCompany={(nodeId) => {
            setSelectedId(nodeId);
            setSelectedNewsId(null);
            setNotice("뉴스 근거와 연결된 기업 노드를 선택했습니다.");
          }}
        />
      )}
    </main>
  );
}
