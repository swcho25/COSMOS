/**
 * 그래프 레이아웃 — 3D 씬에 올릴 노드 좌표를 계산한다.
 * 모두 결정론적이라 같은 스냅샷이면 같은 자리다 (새로고침·재조회에도 흔들리지 않는다).
 */
import type { CompanyGraph, GraphEdge, LatestGraph, Market, RelationshipType } from "@/api/types";
import { INDUSTRY_COLORS, industryColor, relationshipMeta } from "./meta";
import { hashString, seededRandom } from "./rng";

export type Vec3 = [number, number, number];

export interface SceneNode {
  id: string;
  name: string;
  industry: string;
  market: Market;
  stockCode: string;
  color: string;
  pos: Vec3;
  /** 월드 단위 반지름 */
  size: number;
  /** 기업 중심 뷰에서의 단계 (은하 뷰는 0) */
  depth: number;
  degree: number;
}
export interface SceneEdge {
  id: string;
  source: string;
  target: string;
  from: Vec3;
  to: Vec3;
  type: RelationshipType;
  score: number;
  impact: GraphEdge["impactDirection"];
  color: string;
  directed: boolean;
}
export interface SceneModel {
  kind: "galaxy" | "system";
  centerId: string | null;
  nodes: SceneNode[];
  edges: SceneEdge[];
  nodeById: Map<string, SceneNode>;
  /** 노드 → 인접 노드 집합 (하이라이트용) */
  neighbors: Map<string, Set<string>>;
  /** 노드 → 인접 간선 id */
  incident: Map<string, Set<string>>;
}

const GOLDEN = Math.PI * (3 - Math.sqrt(5));
const INDUSTRY_ORDER = Object.keys(INDUSTRY_COLORS);
const KR = new Set(["KOSPI", "KOSDAQ"]);

function weightedDegree(edges: GraphEdge[]) {
  const deg = new Map<string, number>();
  const cnt = new Map<string, number>();
  edges.forEach((e) => {
    for (const id of [e.sourceCompanyId, e.targetCompanyId]) {
      deg.set(id, (deg.get(id) ?? 0) + e.score / 100);
      cnt.set(id, (cnt.get(id) ?? 0) + 1);
    }
  });
  return { deg, cnt };
}

function nodeSize(w: number) {
  return Math.min(2.9, 0.7 + Math.sqrt(w) * 0.52);
}

/** 겹침 완화 + 간선 인접 노드끼리 살짝 끌어당기는 결정론적 완화 단계 */
function relax(nodes: SceneNode[], edges: GraphEdge[], anchor: Map<string, Vec3>, iterations: number, opts: { spring: number; attract: number; planar: boolean }) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  for (let it = 0; it < iterations; it += 1) {
    // 반발
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        const dx = b.pos[0] - a.pos[0];
        const dy = b.pos[1] - a.pos[1];
        const dz = b.pos[2] - a.pos[2];
        const d2 = dx * dx + dy * dy + dz * dz;
        const min = (a.size + b.size) * 2.6 + 1.5;
        if (d2 < min * min && d2 > 1e-6) {
          const d = Math.sqrt(d2);
          const push = ((min - d) / d) * 0.5;
          a.pos[0] -= dx * push;
          a.pos[2] -= dz * push;
          b.pos[0] += dx * push;
          b.pos[2] += dz * push;
          if (!opts.planar) {
            a.pos[1] -= dy * push * 0.5;
            b.pos[1] += dy * push * 0.5;
          }
        }
      }
    }
    // 간선 인접 끌림
    edges.forEach((e) => {
      const a = byId.get(e.sourceCompanyId);
      const b = byId.get(e.targetCompanyId);
      if (!a || !b) return;
      const k = opts.attract * (e.score / 100);
      const dx = b.pos[0] - a.pos[0];
      const dz = b.pos[2] - a.pos[2];
      a.pos[0] += dx * k;
      a.pos[2] += dz * k;
      b.pos[0] -= dx * k;
      b.pos[2] -= dz * k;
    });
    // 원위치 스프링 (성단 밖으로 나가지 않도록)
    nodes.forEach((n) => {
      const o = anchor.get(n.id)!;
      n.pos[0] += (o[0] - n.pos[0]) * opts.spring;
      n.pos[1] += (o[1] - n.pos[1]) * opts.spring;
      n.pos[2] += (o[2] - n.pos[2]) * opts.spring;
    });
  }
}

function finish(kind: SceneModel["kind"], centerId: string | null, nodes: SceneNode[], edges: GraphEdge[]): SceneModel {
  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const neighbors = new Map<string, Set<string>>();
  const incident = new Map<string, Set<string>>();
  const sceneEdges: SceneEdge[] = [];
  edges.forEach((e) => {
    const a = nodeById.get(e.sourceCompanyId);
    const b = nodeById.get(e.targetCompanyId);
    if (!a || !b) return;
    const meta = relationshipMeta(e.relationshipType);
    sceneEdges.push({
      id: e.relationshipId,
      source: a.id,
      target: b.id,
      from: a.pos,
      to: b.pos,
      type: e.relationshipType,
      score: e.score,
      impact: e.impactDirection,
      color: meta.color,
      directed: meta.directed,
    });
    for (const [x, y] of [
      [a.id, b.id],
      [b.id, a.id],
    ]) {
      if (!neighbors.has(x)) neighbors.set(x, new Set());
      neighbors.get(x)!.add(y);
      if (!incident.has(x)) incident.set(x, new Set());
      incident.get(x)!.add(e.relationshipId);
    }
  });
  sceneEdges.sort((a, b) => a.score - b.score); // 강한 간선이 나중에(위에) 그려지도록
  return { kind, centerId, nodes, edges: sceneEdges, nodeById, neighbors, incident };
}

/* ------------------------------------------------------------------ */
/* 은하 — 산업 성단이 은하 디스크 위에 배치된다                              */
/* ------------------------------------------------------------------ */
export function layoutGalaxy(graph: LatestGraph): SceneModel {
  const rng = seededRandom(hashString(graph.snapshotId));
  const { deg, cnt } = weightedDegree(graph.edges);

  const groups = new Map<string, LatestGraph["nodes"]>();
  graph.nodes.forEach((n) => {
    if (!groups.has(n.industryName)) groups.set(n.industryName, []);
    groups.get(n.industryName)!.push(n);
  });
  const industries = [...groups.keys()].sort((a, b) => {
    const ia = INDUSTRY_ORDER.indexOf(a);
    const ib = INDUSTRY_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });

  const nodes: SceneNode[] = [];
  const anchor = new Map<string, Vec3>();
  const K = industries.length;
  const total = graph.nodes.length;
  // 성단이 하나뿐이면(산업 필터) 중심에 크게, 여러 개면 디스크 위에 고리로
  const ringRadius = K === 1 ? 0 : 42 + Math.sqrt(total) * 2.6;

  industries.forEach((ind, k) => {
    const members = groups.get(ind)!.slice().sort((a, b) => (deg.get(b.companyId) ?? 0) - (deg.get(a.companyId) ?? 0));
    const n = members.length;
    const theta = (k / Math.max(1, K)) * Math.PI * 2 - Math.PI / 2 + (k % 2) * 0.12;
    const wobble = Math.sin(k * 2.3) * 7;
    const cx = Math.cos(theta) * (ringRadius + wobble);
    const cz = Math.sin(theta) * (ringRadius + wobble) * 0.78;
    const cy = Math.sin(k * 1.9) * 5;
    const spread = K === 1 ? 10 + Math.sqrt(n) * 7 : 6.5 + Math.sqrt(n) * 4.6;

    members.forEach((m, i) => {
      // 해바라기 배열: 허브(연결 많은 기업)가 성단 중심에 온다
      const r = spread * Math.sqrt((i + 0.5) / n);
      const a = i * GOLDEN + k;
      let x = cx + Math.cos(a) * r;
      let z = cz + Math.sin(a) * r * 0.85;
      const y = cy + (rng() - 0.5) * (3 + r * 0.25);
      // 국내 기업은 은하 중심 쪽, 해외 기업은 바깥쪽으로 살짝 밀어 시장을 공간으로 구분한다
      if (K > 1) {
        const dirx = Math.cos(theta);
        const dirz = Math.sin(theta);
        const shift = (KR.has(m.market) ? -1 : 1) * spread * 0.32;
        x += dirx * shift;
        z += dirz * shift;
      }
      const pos: Vec3 = [x, y, z];
      anchor.set(m.companyId, [x, y, z]);
      nodes.push({
        id: m.companyId,
        name: m.name,
        industry: m.industryName,
        market: m.market,
        stockCode: m.stockCode,
        color: industryColor(m.industryName),
        pos,
        size: nodeSize(deg.get(m.companyId) ?? 0),
        depth: 0,
        degree: cnt.get(m.companyId) ?? 0,
      });
    });
  });

  relax(nodes, graph.edges, anchor, 90, { spring: 0.02, attract: 0.0035, planar: false });
  return finish("galaxy", null, nodes, graph.edges);
}

/* ------------------------------------------------------------------ */
/* 기업 중심 — depth 가 궤도 반지름이 된다                                  */
/* ------------------------------------------------------------------ */
const ORBIT = [0, 15, 27, 38];

export function layoutSystem(graph: CompanyGraph, lookup: (id: string) => { industryName: string; market: Market; stockCode: string } | undefined): SceneModel {
  const rng = seededRandom(hashString(graph.snapshotId + graph.centerCompanyId));
  const { deg, cnt } = weightedDegree(graph.edges);
  const depthOf = new Map(graph.nodes.map((n) => [n.companyId, n.depth]));

  // 각 노드의 "부모": 더 낮은 depth 의 이웃 중 점수가 가장 높은 것
  const parent = new Map<string, string>();
  const bestScore = new Map<string, number>();
  graph.edges.forEach((e) => {
    const ds = depthOf.get(e.sourceCompanyId) ?? 9;
    const dt = depthOf.get(e.targetCompanyId) ?? 9;
    if (ds === dt) return;
    const [child, par] = ds > dt ? [e.sourceCompanyId, e.targetCompanyId] : [e.targetCompanyId, e.sourceCompanyId];
    if ((bestScore.get(child) ?? -1) < e.score) {
      bestScore.set(child, e.score);
      parent.set(child, par);
    }
  });

  const angle = new Map<string, number>([[graph.centerCompanyId, 0]]);
  const byDepth = new Map<number, string[]>();
  graph.nodes.forEach((n) => {
    if (!byDepth.has(n.depth)) byDepth.set(n.depth, []);
    byDepth.get(n.depth)!.push(n.companyId);
  });
  const maxDepth = Math.max(...byDepth.keys());
  for (let d = 1; d <= maxDepth; d += 1) {
    const ids = (byDepth.get(d) ?? []).slice();
    if (d === 1) {
      ids.sort((a, b) => (deg.get(b) ?? 0) - (deg.get(a) ?? 0));
      ids.forEach((id, i) => angle.set(id, (i / ids.length) * Math.PI * 2 - Math.PI / 2));
      continue;
    }
    // 부모 각도 주변에 자식을 부채꼴로 배치
    const byParent = new Map<string, string[]>();
    ids.forEach((id) => {
      const p = parent.get(id) ?? graph.centerCompanyId;
      if (!byParent.has(p)) byParent.set(p, []);
      byParent.get(p)!.push(id);
    });
    const sector = (Math.PI * 2) / Math.max(1, byDepth.get(d - 1)?.length ?? 1);
    byParent.forEach((children, p) => {
      const base = angle.get(p) ?? rng() * Math.PI * 2;
      children.forEach((c, i) => {
        const t = children.length === 1 ? 0 : i / (children.length - 1) - 0.5;
        angle.set(c, base + t * sector * 0.85);
      });
    });
  }

  const nodes: SceneNode[] = [];
  const anchor = new Map<string, Vec3>();
  graph.nodes.forEach((n) => {
    const d = n.depth;
    const r = ORBIT[Math.min(d, ORBIT.length - 1)] + (d > 0 ? (rng() - 0.5) * 6 : 0);
    const a = angle.get(n.companyId) ?? rng() * Math.PI * 2;
    const pos: Vec3 = d === 0 ? [0, 0, 0] : [Math.cos(a) * r, (rng() - 0.5) * (2 + d * 2.2), Math.sin(a) * r * 0.82];
    anchor.set(n.companyId, [...pos] as Vec3);
    const info = lookup(n.companyId);
    nodes.push({
      id: n.companyId,
      name: n.name,
      industry: info?.industryName ?? "",
      market: info?.market ?? "",
      stockCode: info?.stockCode ?? "",
      color: industryColor(info?.industryName),
      pos,
      size: d === 0 ? 3.4 : nodeSize(deg.get(n.companyId) ?? 0) * (d === 1 ? 1.15 : d === 2 ? 0.95 : 0.8),
      depth: d,
      degree: cnt.get(n.companyId) ?? 0,
    });
  });
  const movable = nodes.filter((n) => n.depth > 0);
  relax(movable, graph.edges, anchor, 60, { spring: 0.05, attract: 0.0015, planar: false });
  return finish("system", graph.centerCompanyId, nodes, graph.edges);
}

/* ------------------------------------------------------------------ */
/* 뉴스 부분 그래프 — 선택 뉴스의 관련 기업 + 1홉 이웃                        */
/* ------------------------------------------------------------------ */
export function layoutNewsSubgraph(graph: LatestGraph, seedIds: string[], expand: boolean): SceneModel {
  const seed = new Set(seedIds.filter((id) => graph.nodes.some((n) => n.companyId === id)));
  const included = new Set(seed);
  if (expand) {
    graph.edges.forEach((e) => {
      if (seed.has(e.sourceCompanyId)) included.add(e.targetCompanyId);
      if (seed.has(e.targetCompanyId)) included.add(e.sourceCompanyId);
    });
  }
  const edges = graph.edges.filter((e) => included.has(e.sourceCompanyId) && included.has(e.targetCompanyId));
  const nodesRaw = graph.nodes.filter((n) => included.has(n.companyId));
  const sub: CompanyGraph = {
    snapshotId: graph.snapshotId + seedIds.join(","),
    asOfAt: graph.asOfAt,
    centerCompanyId: seedIds[0] ?? "",
    personalized: graph.personalized,
    nodes: nodesRaw.map((n) => ({ companyId: n.companyId, name: n.name, depth: seed.has(n.companyId) ? (n.companyId === seedIds[0] ? 0 : 1) : 2 })),
    edges,
  };
  const info = new Map(graph.nodes.map((n) => [n.companyId, n]));
  const model = layoutSystem(sub, (id) => info.get(id));
  // 뉴스 지도는 중심 기업 하나가 아니라 관련 기업 묶음이므로 depth 0·1을 같은 궤도에 놓는다
  return model;
}
