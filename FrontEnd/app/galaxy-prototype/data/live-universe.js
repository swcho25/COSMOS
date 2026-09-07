import { scenarios } from '../../../lib/risk-data';
import { REL_TYPES, SECTOR_LIST } from './universe';
import { MOCK_COMPANIES, MOCK_RELATIONS } from './mock-network';

const BASE_NODE_BY_ID = new Map(
  scenarios.flatMap((scenario) => scenario.nodes).map((node) => [node.id, node])
);
const BASE_COMPANY_NODES = scenarios
  .flatMap((scenario) => scenario.nodes)
  .filter((node) => node.kind === 'company');
const BASE_EDGES = scenarios.flatMap((scenario) => scenario.edges);

const normalize = (value = '') =>
  value.toLocaleLowerCase('ko-KR').replace(/[^a-z0-9가-힣]/g, '');

function tickerKey(value = '') {
  const upper = String(value).toUpperCase().replace(/[^A-Z0-9]/g, '');
  return /^A\d{6}$/.test(upper) ? upper.slice(1) : upper;
}

function sixDigitCode(value = '') {
  return String(value).match(/\d{6}/)?.[0] ?? '';
}

function tickerMatches(left, right) {
  const a = tickerKey(left);
  const b = tickerKey(right);
  if (!a || !b || a === 'NEWS' || b === 'NEWS') return false;
  if (a === b) return true;
  const codeA = sixDigitCode(left);
  const codeB = sixDigitCode(right);
  return Boolean(codeA && codeB && codeA === codeB);
}

function companyMatches(candidate, reference) {
  if (tickerMatches(candidate?.ticker ?? candidate?.code, reference?.ticker ?? reference?.code)) return true;
  const candidateName = normalize(candidate?.name ?? candidate?.label);
  if (!candidateName) return false;
  return [reference?.name, reference?.label, ...(reference?.aliases ?? [])]
    .map(normalize)
    .filter(Boolean)
    .some((name) => name === candidateName || (Math.min(name.length, candidateName.length) >= 4 && (name.includes(candidateName) || candidateName.includes(name))));
}

function findMatchingCompany(map, reference) {
  return [...map.values()].find((company) => companyMatches(company, reference));
}

function baseNodesForCompany(company) {
  return BASE_COMPANY_NODES.filter((node) => companyMatches(company, node));
}

function hash(value) {
  let h = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h >>> 0);
}

function entityId(name) {
  return `live-${hash(normalize(name)).toString(36)}`;
}

function scenarioSector(scenarioId) {
  if (scenarioId === 'battery') return 'battery';
  if (scenarioId === 'semiconductor') return 'semi';
  return 'auto';
}

function sectorFor(company, scenarioId) {
  const text = `${company?.industry ?? ''} ${company?.label ?? company?.name ?? ''}`.toLowerCase();
  if (/반도체|메모리|hbm|장비|chip|semiconductor/.test(text)) return 'semi';
  if (/배터리|2차전지|리튬|양극재|소재|battery|ev/.test(text)) return 'battery';
  if (/자동차|완성차|모빌리티|부품|motor|ford|tesla/.test(text)) return 'auto';
  if (/플랫폼|인터넷|ai|소프트웨어|클라우드|naver|kakao/.test(text)) return 'platform';
  if (/바이오|제약|헬스|bio|pharma/.test(text)) return 'bio';
  if (/에너지|전력|철강|중공업|조선|energy/.test(text)) return 'energy';
  return scenarioSector(scenarioId);
}

function typeForRelation(value = '', basis = 'reported') {
  const relation = value.toUpperCase();
  if (/OWN|STAKE|SHAREHOLDER|CONTROL/.test(relation)) return 'group';
  if (/SUPPL|PROCURE|MATERIAL|VENDOR/.test(relation)) return 'supply';
  if (/CUSTOMER|CONTRACT|ORDER|PARTNER/.test(relation)) return 'customer';
  if (/RIVAL|COMPETE|SUBSTITUTE/.test(relation)) return 'rival';
  if (basis === 'modeled' || /RISK|EXPOSURE|DRIVE|AFFECT/.test(relation)) return 'macro';
  return 'unknown';
}

function companyFromAffected(company, news) {
  const base = BASE_NODE_BY_ID.get(company.nodeId);
  return {
    id: company.nodeId,
    name: company.label,
    ticker: company.code ?? base?.code ?? company.market ?? 'NEWS',
    sector: sectorFor({ ...base, ...company }, news.scenarioId),
    cap: Math.max(3, Math.round((company.score || 35) * 0.42)),
    score: company.score || 35,
    industry: company.industry ?? base?.industry ?? '뉴스 인식 기업',
    description: company.reason ?? base?.description ?? news.summary,
    sourceUrl: news.sourceUrl,
    origin: 'news',
    direct: news.entities
      .filter((entity) => entity.type === 'COMPANY')
      .some((entity) => {
        const a = normalize(entity.name);
        const b = normalize(company.label);
        return a === b || a.includes(b) || b.includes(a);
      }),
  };
}

function baseCompany(node, scenarioId, score = 28) {
  return {
    id: node.id,
    name: node.label,
    ticker: node.code ?? 'NEWS',
    sector: sectorFor(node, scenarioId),
    cap: Math.max(3, Math.round(score * 0.42)),
    score,
    industry: node.industry ?? '연결 기업',
    description: node.description,
    sourceUrl: node.sourceUrl,
    origin: 'reference',
    direct: false,
  };
}

function upsertCompany(map, company) {
  const matched = findMatchingCompany(map, company);
  const id = matched?.id ?? company.id;
  const previous = map.get(id);
  if (!previous || company.score > previous.score || company.direct) {
    map.set(id, {
      ...previous,
      ...company,
      id,
      direct: Boolean(previous?.direct || company.direct),
      isMock: Boolean(previous?.isMock && company.isMock),
    });
  }
  return map.get(id);
}

function keyOf(a, b) {
  return a < b ? `${a}|${b}` : `${b}|${a}`;
}

function mergeEdge(map, edge) {
  const previous = map.get(edge.key);
  const priority = { disclosed: 4, reported: 3, modeled: 2, mock: 1 };
  const nextPriority = priority[edge.basis] ?? 0;
  const previousPriority = priority[previous?.basis] ?? 0;
  if (!previous || nextPriority > previousPriority || (nextPriority === previousPriority && Math.abs(edge.corr) > Math.abs(previous.corr))) {
    map.set(edge.key, edge);
  }
}

function addMockContext(companies, edges, activeNewsId) {
  const current = [...companies.values()];
  const activeSectors = new Set(current.map((company) => company.sector));
  const existingByMockId = new Map();
  const scores = new Map();

  MOCK_COMPANIES.forEach((mock) => {
    const existing = current.find((company) => companyMatches(company, mock));
    if (existing) {
      existingByMockId.set(mock.id, existing);
      scores.set(mock.id, 1200 + mock.cap);
    } else if (!activeNewsId || activeSectors.has(mock.sector)) {
      scores.set(mock.id, 700 + mock.cap);
    }
  });

  // 개별 기사는 직접 기업·관련 업종을 씨앗으로 산업 관계 카탈로그를 2-hop 확장한다.
  if (activeNewsId) {
    for (let hop = 0; hop < 2; hop += 1) {
      const frontier = new Set(scores.keys());
      MOCK_RELATIONS.forEach((relation) => {
        const aKnown = frontier.has(relation.a);
        const bKnown = frontier.has(relation.b);
        if (aKnown === bKnown) return;
        const nextId = aKnown ? relation.b : relation.a;
        const nextCompany = MOCK_COMPANIES.find((company) => company.id === nextId);
        if (!nextCompany || scores.has(nextId)) return;
        scores.set(nextId, (hop === 0 ? 500 : 300) + relation.strength * 100 + nextCompany.cap * 0.08);
      });
    }
  } else {
    MOCK_COMPANIES.forEach((mock) => scores.set(mock.id, 500 + mock.cap));
  }

  const maxNodes = activeNewsId ? 20 : MOCK_COMPANIES.length;
  const selectedIds = new Set(
    [...scores.entries()]
      .sort((left, right) => right[1] - left[1])
      .slice(0, maxNodes)
      .map(([id]) => id)
  );
  const canonicalId = new Map();

  MOCK_COMPANIES.filter((mock) => selectedIds.has(mock.id)).forEach((mock) => {
    const existing = existingByMockId.get(mock.id) ?? findMatchingCompany(companies, mock);
    if (existing) {
      canonicalId.set(mock.id, existing.id);
      return;
    }
    const id = `mock-${tickerKey(mock.id).toLowerCase() || hash(mock.name).toString(36)}`;
    const company = {
      id,
      name: mock.name,
      ticker: mock.ticker,
      sector: mock.sector,
      cap: Math.max(5, Math.min(45, Math.round(Math.sqrt(mock.cap) * 3))),
      score: 24,
      industry: mock.industry,
      description: '관계지도 밀도와 전이 경로를 시연하기 위한 산업 네트워크 목업 노드',
      sourceUrl: null,
      direct: false,
      origin: 'mock',
      isMock: true,
    };
    companies.set(id, company);
    canonicalId.set(mock.id, id);
  });

  MOCK_RELATIONS.forEach((relation) => {
    if (!selectedIds.has(relation.a) || !selectedIds.has(relation.b)) return;
    const a = canonicalId.get(relation.a);
    const b = canonicalId.get(relation.b);
    if (!a || !b || a === b) return;
    const companyA = companies.get(a);
    const companyB = companies.get(b);
    const crossSector = companyA?.sector !== companyB?.sector;
    const corr = Math.min(0.9, 0.48 + relation.strength * 0.42);
    mergeEdge(edges, {
      key: keyOf(a, b),
      a,
      b,
      corr,
      type: relation.type,
      reason: relation.reason,
      crossSector,
      serendipity: relation.type === 'unknown' || (relation.type === 'macro' && crossSector),
      basis: 'mock',
      origin: 'mock',
      isMock: true,
      sourceNewsId: activeNewsId,
      sourceTitle: '프로토타입 보강용 산업 관계 카탈로그',
      sourceUrl: null,
      confidence: Math.round(45 + relation.strength * 35),
    });
  });
}

function createPositions(companies) {
  const bySector = new Map(SECTOR_LIST.map((sector) => [sector.id, []]));
  companies.forEach((company) => (bySector.get(company.sector) ?? bySector.get('platform')).push(company));
  const positions = {};
  const sectorCenters = {};

  SECTOR_LIST.forEach((sector, sectorIndex) => {
    const angle = (sectorIndex / SECTOR_LIST.length) * Math.PI * 2 + 0.35;
    const center = [Math.cos(angle) * 46, Math.sin(sectorIndex * 2.399) * 13, Math.sin(angle) * 38];
    sectorCenters[sector.id] = center;
    const list = bySector.get(sector.id) ?? [];
    list.forEach((company, index) => {
      const seed = hash(company.id) / 0xffffffff;
      const phi = index * 2.399963 + seed * 0.8;
      const y = list.length <= 1 ? 0 : 1 - (index / (list.length - 1)) * 2;
      const radius = Math.sqrt(Math.max(0.02, 1 - y * y));
      const spread = Math.min(16, 8 + list.length * 0.8);
      positions[company.id] = [
        center[0] + Math.cos(phi) * radius * spread,
        center[1] + y * spread * 0.66,
        center[2] + Math.sin(phi) * radius * spread,
      ];
    });
  });
  return { positions, sectorCenters };
}

export function buildLiveUniverse(items = [], activeNewsId = null) {
  const activeNews = items.find((item) => item.id === activeNewsId) ?? items[0] ?? null;
  const scopedItems = activeNewsId && activeNews ? [activeNews] : items.slice(0, 16);
  const companies = new Map();
  const edges = new Map();
  const newsByCompany = {};

  scopedItems.forEach((news) => {
    const articleCompanies = [];
    news.affected.forEach((affected) => {
      const company = upsertCompany(companies, companyFromAffected(affected, news));
      articleCompanies.push(company);
      (newsByCompany[company.id] ||= []).push(news);
    });

    news.entities
      .filter((entity) => entity.type === 'COMPANY')
      .forEach((entity) => {
        const match = articleCompanies.find((company) => {
          const a = normalize(company.name);
          const b = normalize(entity.name);
          return a === b || a.includes(b) || b.includes(a);
        });
        if (match) return;
        const company = upsertCompany(companies, {
          id: entityId(entity.name),
          name: entity.name,
          ticker: 'NEWS',
          sector: scenarioSector(news.scenarioId),
          cap: 13,
          score: 34,
          industry: '기사 직접 언급',
          description: news.summary,
          sourceUrl: news.sourceUrl,
          origin: 'news',
          direct: true,
        });
        articleCompanies.push(company);
        (newsByCompany[company.id] ||= []).push(news);
      });

    const direct = articleCompanies.filter((company) => company.direct);
    const anchor = direct.sort((a, b) => b.score - a.score)[0] ?? articleCompanies[0];
    if (!anchor) return;

    articleCompanies.forEach((company) => {
      if (company.id === anchor.id) return;
      const triple = news.relations.find((relation) => {
        const relationText = `${relation.source} ${relation.target}`;
        return relationText.includes(anchor.name) && relationText.includes(company.name);
      });
      const basis = triple?.basis ?? (company.direct ? 'reported' : 'modeled');
      const corr = Math.min(0.97, 0.34 + Math.min(anchor.score, company.score) * 0.0064);
      const type = typeForRelation(triple?.relation, basis);
      mergeEdge(edges, {
        key: keyOf(anchor.id, company.id),
        a: anchor.id,
        b: company.id,
        corr,
        type,
        reason: triple
          ? `${triple.source} → ${triple.relation} → ${triple.target}`
          : company.reason || news.summary,
        crossSector: anchor.sector !== company.sector,
        serendipity: basis === 'modeled' && anchor.sector !== company.sector,
        basis,
        sourceNewsId: news.id,
        sourceTitle: news.title,
        sourceUrl: news.sourceUrl,
        confidence: triple?.confidence ?? news.confidence,
      });
    });

    // 기사에 직접 언급된 기업에서 기준 공급망·지분 그래프의 1-hop 기업까지 확장한다.
    direct.forEach((company) => {
      const baseIds = new Set(baseNodesForCompany(company).map((node) => node.id));
      BASE_EDGES.filter((edge) => baseIds.has(edge.source) || baseIds.has(edge.target))
        .slice(0, 8)
        .forEach((edge) => {
          const matchedBaseId = baseIds.has(edge.source) ? edge.source : edge.target;
          const otherId = edge.source === matchedBaseId ? edge.target : edge.source;
          const node = BASE_NODE_BY_ID.get(otherId);
          if (!node || node.kind !== 'company') return;
          const other = findMatchingCompany(companies, node)
            ?? baseCompany(node, news.scenarioId, Math.max(24, Math.round(company.score * edge.weight)));
          const canonicalOther = upsertCompany(companies, other);
          (newsByCompany[canonicalOther.id] ||= []).push(news);
          const type = typeForRelation(edge.relation, edge.basis);
          mergeEdge(edges, {
            key: keyOf(company.id, canonicalOther.id),
            a: company.id,
            b: canonicalOther.id,
            corr: Math.min(0.94, 0.3 + edge.weight * 0.68),
            type,
            reason: `${edge.label} · ${edge.evidence}`,
            crossSector: company.sector !== canonicalOther.sector,
            serendipity: edge.basis === 'modeled' && company.sector !== canonicalOther.sector,
            basis: edge.basis,
            sourceNewsId: news.id,
            sourceTitle: news.title,
            sourceUrl: edge.sourceUrl ?? news.sourceUrl,
            confidence: Math.round(edge.weight * 100),
          });
        });
    });
  });

  addMockContext(companies, edges, activeNewsId);
  const companyList = [...companies.values()];
  const { positions, sectorCenters } = createPositions(companyList);
  return {
    activeNews,
    companies: companyList,
    companyById: Object.fromEntries(companyList.map((company) => [company.id, company])),
    positions,
    sectorCenters,
    edges: [...edges.values()].sort((a, b) => Math.abs(b.corr) - Math.abs(a.corr)),
    newsByCompany,
  };
}

export function liveNodeRadius(company) {
  return 0.82 + Math.sqrt(Math.max(2, company.cap)) * 0.2;
}

export function liveTypeMeta(type) {
  return REL_TYPES[type] ?? REL_TYPES.unknown;
}
