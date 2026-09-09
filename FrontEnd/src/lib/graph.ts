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
        const min = (a.size + b.size) * 3.2 + 2.5;
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
/** 구면 위에 n 개 점을 고르게 놓는 피보나치 분포 (단위 벡터) */
function fibonacciSphere(n: number, i: number, phase: number): Vec3 {
  if (n <= 1) return [0, 1, 0];
  const y = 1 - (2 * (i + 0.5)) / n;
  const r = Math.sqrt(Math.max(0, 1 - y * y));
  const a = i * GOLDEN + phase;
  return [Math.cos(a) * r, y, Math.sin(a) * r];
}

/** 성단 껍질 j(1부터)에 앉을 수 있는 노드 수 — 바깥 껍질일수록 넓다 */
const SHELL_CAP = (j: number) => 2 * (j + 1) * (j + 1);
const SHELL_STEP = 10.5;
const CLUSTER_GAP = 18;

/** 성단 안에서 i 번째(연결 많은 순) 노드의 껍질 번호와 껍질 내 순번·정원 */
function shellOf(i: number) {
  let j = 1;
  let start = 1;
  while (i >= start + SHELL_CAP(j)) {
    start += SHELL_CAP(j);
    j += 1;
  }
  return { shell: j, index: i - start, cap: SHELL_CAP(j) };
}
function clusterRadius(n: number) {
  return n <= 1 ? 4 : shellOf(n - 1).shell * SHELL_STEP + 4;
}

/**
 * 전체 은하 — 입체 성단들이 은하 핵을 둘러싼 구조.
 *  - 성단 하나 = 허브(연결 많은 기업)를 중심으로 한 동심 구 껍질. 연결 순으로 안쪽 껍질부터 채운다
 *  - 기업 수가 가장 많은 산업이 은하 핵을 차지하고, 나머지 성단은 핵을 둘러싼 구면 위에 고르게 놓인다
 *  - 어느 방향에서 봐도 앞뒤·위아래로 성단이 겹겹이 보여 깊이가 생기고, 배치 규칙은 눈에 읽힌다
 */
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

  // 성단 중심 배치: 핵 + 구면
  const core = industries.slice().sort((a, b) => groups.get(b)!.length - groups.get(a)!.length)[0];
  const shellInds = industries.filter((ind) => ind !== core);
  const maxR = shellInds.reduce((m, ind) => Math.max(m, clusterRadius(groups.get(ind)!.length)), 0);
  const coreR = clusterRadius(groups.get(core)!.length);
  // 구면 위 이웃 간 각거리 ≈ sqrt(4π/K). 그 호 길이가 성단 지름 + 여백보다 크도록 반지름을 정한다
  const sep = Math.sqrt((4 * Math.PI) / Math.max(1, shellInds.length));
  const R = shellInds.length === 0 ? 0 : Math.max(coreR + maxR + CLUSTER_GAP, ((2 * maxR + CLUSTER_GAP) / sep) * 1.05);

  const centers = new Map<string, Vec3>();
  centers.set(core, [0, 0, 0]);
  const phase = rng() * Math.PI * 2;
  shellInds.forEach((ind, k) => {
    const [ux, uy, uz] = fibonacciSphere(shellInds.length, k, phase);
    // 위아래는 살짝 눌러 은하다운 원반 기운을 남기되, 앞뒤(z)는 그대로 두어 깊이를 살린다
    centers.set(ind, [ux * R, uy * R * 0.72, uz * R]);
  });

  const nodes: SceneNode[] = [];
  industries.forEach((ind, k) => {
    const members = groups.get(ind)!.slice().sort((a, b) => (deg.get(b.companyId) ?? 0) - (deg.get(a.companyId) ?? 0));
    const [cx, cy, cz] = centers.get(ind)!;
    const spin = k * 1.7 + phase;
    members.forEach((m, i) => {
      let pos: Vec3 = [cx, cy, cz];
      if (i > 0) {
        const { shell, index, cap } = shellOf(i);
        const count = Math.min(cap, members.length - (i - index)); // 이 껍질에 실제로 앉는 수
        const [ux, uy, uz] = fibonacciSphere(count, index, spin + shell * 0.9);
        const r = shell * SHELL_STEP;
        pos = [cx + ux * r, cy + uy * r, cz + uz * r];
      }
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
