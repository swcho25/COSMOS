import type { SceneEdge, SceneModel } from "./graph";

export interface PathResult {
  /** from 에서 to 까지의 노드 id (양끝 포함) */
  nodes: string[];
  /** nodes[i] 와 nodes[i+1] 를 잇는 간선 id */
  edges: string[];
  /** 경로 간선 중 가장 낮은 점수 — 병목 */
  bottleneck: number;
}

/**
 * 두 기업 사이의 최단 경로 — 힙 없는 Dijkstra (노드 ≤ 200 이라 O(N²) 으로 충분).
 * 가중치는 `1 + (100 - score) / 100` 이라 홉 수가 먼저, 같은 홉 수면 점수 합이 높은 쪽이 이긴다.
 * allowed 는 최소 점수·관계 유형 필터만 넘긴다 — 간선 예산(상위만)은 무시해 숨어 있는 간선으로도 이어 준다.
 * 인접 간선을 직접 순회하므로 같은 두 노드 사이에 간선이 여럿이어도 필터를 통과한 최선의 것을 고른다.
 */
export function shortestPath(model: SceneModel, from: string, to: string, allowed?: (e: SceneEdge) => boolean): PathResult | null {
  if (from === to || !model.nodeById.has(from) || !model.nodeById.has(to)) return null;
  const dist = new Map<string, number>([[from, 0]]);
  const prevEdge = new Map<string, SceneEdge>();
  const done = new Set<string>();

  for (;;) {
    // 미방문 중 최단 거리 노드
    let u: string | null = null;
    let best = Infinity;
    dist.forEach((d, id) => {
      if (!done.has(id) && d < best) {
        best = d;
        u = id;
      }
    });
    if (u === null) return null;
    if (u === to) break;
    done.add(u);
    const uId: string = u;
    model.incident.get(uId)?.forEach((eid) => {
      const e = model.edgeById.get(eid);
      if (!e || (allowed && !allowed(e))) return;
      const v = e.source === uId ? e.target : e.source;
      if (done.has(v)) return;
      const nd = best + 1 + (100 - e.score) / 100;
      if (nd < (dist.get(v) ?? Infinity)) {
        dist.set(v, nd);
        prevEdge.set(v, e);
      }
    });
  }

  // 역추적
  const nodes: string[] = [to];
  const edges: string[] = [];
  let bottleneck = 100;
  let cur = to;
  while (cur !== from) {
    const e = prevEdge.get(cur);
    if (!e) return null;
    edges.push(e.id);
    bottleneck = Math.min(bottleneck, e.score);
    cur = e.source === cur ? e.target : e.source;
    nodes.push(cur);
  }
  nodes.reverse();
  edges.reverse();
  return { nodes, edges, bottleneck };
}
