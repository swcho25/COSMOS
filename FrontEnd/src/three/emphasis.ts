import type { SceneModel } from "@/lib/graph";

export interface Emphasis {
  node: Map<string, number>;
  edge: Map<string, number>;
  /** 강조의 이유가 되는 노드 (라벨 항상 표시) */
  pinned: Set<string>;
}

export interface EmphasisInput {
  hoveredId: string | null;
  hoveredEdgeId: string | null;
  selectedEdgeId: string | null;
  minScore: number;
  activeTypes: Set<string>;
}

const DEPTH_DIM = [1, 1, 0.74, 0.56];

/**
 * 어떤 노드·간선을 얼마나 밝게 그릴지 계산한다.
 * 우선순위: 호버 노드 > 선택 간선 > 호버 간선 > 기업 중심 뷰의 depth 흐림 > 기본.
 * 필터(최소 점수·관계 유형)에 걸린 간선은 0 으로 숨긴다.
 */
export function computeEmphasis(model: SceneModel, input: EmphasisInput): Emphasis {
  const node = new Map<string, number>();
  const edge = new Map<string, number>();
  const pinned = new Set<string>();

  const visible = new Set<string>();
  model.edges.forEach((e) => {
    const ok = e.score >= input.minScore && input.activeTypes.has(String(e.type));
    if (ok) visible.add(e.id);
  });

  const base = (id: string) => {
    const n = model.nodeById.get(id);
    return n ? DEPTH_DIM[Math.min(n.depth, 3)] : 1;
  };

  if (input.hoveredId && model.nodeById.has(input.hoveredId)) {
    const h = input.hoveredId;
    const nb = model.neighbors.get(h) ?? new Set();
    model.nodes.forEach((n) => node.set(n.id, n.id === h ? 1 : nb.has(n.id) ? 0.95 : 0.16));
    model.edges.forEach((e) => edge.set(e.id, !visible.has(e.id) ? 0 : e.source === h || e.target === h ? 1 : 0.08));
    pinned.add(h);
    nb.forEach((id) => pinned.add(id));
    return { node, edge, pinned };
  }

  const edgeFocus = input.selectedEdgeId ?? input.hoveredEdgeId;
  if (edgeFocus) {
    const sel = model.edges.find((e) => e.id === edgeFocus);
    if (sel) {
      const strong = input.selectedEdgeId ? 0.14 : 0.35;
      model.nodes.forEach((n) => node.set(n.id, n.id === sel.source || n.id === sel.target ? 1 : strong));
      model.edges.forEach((e) => edge.set(e.id, !visible.has(e.id) ? 0 : e.id === sel.id ? 1 : strong * 0.6));
      pinned.add(sel.source);
      pinned.add(sel.target);
      return { node, edge, pinned };
    }
  }

  model.nodes.forEach((n) => node.set(n.id, base(n.id)));
  model.edges.forEach((e) => edge.set(e.id, visible.has(e.id) ? Math.min(base(e.source), base(e.target)) : 0));
  if (model.centerId) pinned.add(model.centerId);
  return { node, edge, pinned };
}
