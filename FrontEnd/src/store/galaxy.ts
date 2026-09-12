import { create } from "zustand";
import type { AltitudeWindow, MetricWindow, PricePeriod, RelationshipType } from "@/api/types";
import { RELATIONSHIP_ORDER } from "@/lib/meta";
import type { RoleKey } from "@/lib/roles";

export type PanelTab = "news" | "price" | "board";
/** 은하 범위 — "all" 전체 우주, "mine" 관심 기업(+1홉)만 보는 개인 은하 */
export type GalaxyScope = "all" | "mine";
export type Phase = "galaxy" | "warp" | "system";

interface GalaxyState {
  /* ---- 뷰 상태 ---- */
  phase: Phase;
  focusId: string | null; // system 뷰의 중심 기업
  warpTarget: string | null; // 워프가 끝나면 focus 가 될 기업 (null = 은하 복귀)
  warpStartedAt: number;
  warpCommitted: boolean;

  hoveredId: string | null;
  hoveredEdgeId: string | null;
  selectedEdgeId: string | null;
  /** 경로 기준 기업 (Shift+클릭). 다른 기업을 호버하면 여기까지의 최단 경로가 강조된다. 파생값(경로)은 스토어에 두지 않는다 */
  pinnedId: string | null;
  /** 기업 중심 뷰 1홉 요약에서 고른 역할 — 그 역할의 이웃·간선만 남긴다 */
  roleFilter: RoleKey | null;

  /* ---- 필터 ---- */
  /** 은하에 무엇을 올릴지 — 전체 우주냐 내 관심 기업이냐. 같은 화면을 두 범위로 쓴다 */
  scope: GalaxyScope;
  /** 내 은하에서 관심 기업의 1홉 이웃까지 함께 올릴지 (끄면 관심 기업끼리의 관계만 남는다) */
  includeNeighbors: boolean;
  industryId: string | null;
  minScore: number;
  activeTypes: Set<RelationshipType>;
  showLabels: boolean;
  /** 간선 예산: 켜면 점수 상위 관계만 기본 표시, 나머지는 노드 호버 때만 */
  edgeBudget: boolean;
  /** 주가 고도: 켜면 은하 뷰의 행성이 기간 등락률만큼 원반 위·아래로 뜬다 (기업 중심 뷰에는 적용하지 않는다) */
  altitudeOn: boolean;
  /** 고도의 기준 기간 */
  altitudeWindow: AltitudeWindow;

  /* ---- 패널 ---- */
  panelTab: PanelTab;
  window: MetricWindow;
  pricePeriod: PricePeriod;
  searchOpen: boolean;
  /** 우측 인텔리전스 패널 펼침 여부 (접으면 오른쪽 가장자리에 꼬리표) */
  panelOpen: boolean;
  /** 우하단 관계 유형·최소 점수 카드 펼침 여부 */
  legendOpen: boolean;
  /** 기업 중심 뷰의 최신 뉴스 콜아웃 닫힘 여부 (기업이 바뀌면 다시 열림) */
  calloutHidden: boolean;

  /* ---- 액션 ---- */
  warpTo(companyId: string | null): void;
  commitWarp(): void;
  completeWarp(): void;
  backToGalaxy(): void;
  setHovered(id: string | null): void;
  setHoveredEdge(id: string | null): void;
  selectEdge(id: string | null): void;
  clearSelection(): void;
  togglePin(id: string): void;
  setRoleFilter(r: RoleKey | null): void;
  setScope(scope: GalaxyScope): void;
  toggleNeighbors(): void;
  setIndustry(id: string | null): void;
  setMinScore(v: number): void;
  toggleType(t: RelationshipType): void;
  toggleLabels(): void;
  toggleEdgeBudget(): void;
  setAltitude(on: boolean): void;
  setAltitudeWindow(w: AltitudeWindow): void;
  setPanelTab(t: PanelTab): void;
  setWindow(w: MetricWindow): void;
  setPricePeriod(p: PricePeriod): void;
  setSearchOpen(v: boolean): void;
  setPanelOpen(v: boolean): void;
  toggleLegend(): void;
  hideCallout(): void;
}

export const useGalaxy = create<GalaxyState>((set, get) => ({
  phase: "galaxy",
  focusId: null,
  warpTarget: null,
  warpStartedAt: 0,
  warpCommitted: false,

  hoveredId: null,
  hoveredEdgeId: null,
  selectedEdgeId: null,
  pinnedId: null,
  roleFilter: null,

  scope: "all",
  includeNeighbors: true,
  industryId: null,
  minScore: 0,
  activeTypes: new Set(RELATIONSHIP_ORDER),
  showLabels: true,
  edgeBudget: true,
  altitudeOn: true,
  altitudeWindow: "1M",

  panelTab: "news",
  window: "30D",
  pricePeriod: "1M",
  searchOpen: false,
  panelOpen: true,
  legendOpen: true,
  calloutHidden: false,

  warpTo(companyId) {
    const s = get();
    if (s.phase === "warp") return;
    if (companyId !== null && companyId === s.focusId) return;
    if (companyId === null && s.focusId === null) return;
    set({
      phase: "warp",
      warpStartedAt: performance.now(),
      warpTarget: companyId,
      warpCommitted: false,
      selectedEdgeId: null,
      hoveredId: null,
      hoveredEdgeId: null,
      pinnedId: null,
      roleFilter: null,
      panelOpen: true,
      calloutHidden: false,
    });
  },
  commitWarp() {
    set((s) => ({ warpCommitted: true, focusId: s.warpTarget }));
  },
  completeWarp() {
    set((s) => ({ phase: s.warpTarget ? "system" : "galaxy" }));
  },
  backToGalaxy() {
    get().warpTo(null);
  },

  setHovered(id) {
    if (get().hoveredId !== id) set({ hoveredId: id });
  },
  setHoveredEdge(id) {
    if (get().hoveredEdgeId !== id) set({ hoveredEdgeId: id });
  },
  selectEdge(id) {
    set((s) => ({ selectedEdgeId: s.selectedEdgeId === id ? null : id, panelOpen: true }));
  },
  clearSelection() {
    set({ selectedEdgeId: null, hoveredId: null, hoveredEdgeId: null, pinnedId: null, roleFilter: null });
  },
  togglePin(id) {
    // 워프 중에는 아직 이전 모델이 보이고 집을 수도 있다 — 그때 찍은 핀은 착지한 뷰에 없는 기업을 가리킨 채 남는다 (warpTo 와 같은 가드)
    if (get().phase === "warp") return;
    set((s) => ({ pinnedId: s.pinnedId === id ? null : id }));
  },
  setRoleFilter(r) {
    set((s) => ({ roleFilter: s.roleFilter === r ? null : r }));
  },

  setScope(scope) {
    // 범위가 바뀌면 모델이 통째로 갈린다 — 새 모델에 없을 수 있는 상태(선택 간선·경로 핀·역할 필터)를 setIndustry 와 같게 턴다
    set({ scope, selectedEdgeId: null, pinnedId: null, roleFilter: null });
  },
  toggleNeighbors() {
    set((s) => ({ includeNeighbors: !s.includeNeighbors }));
  },
  setIndustry(id) {
    // 새 모델에는 없을 수 있는 상태(선택 간선·경로 핀·역할 필터)를 함께 턴다 — 핀이 남으면 crosshair 커서만 남고 경로가 잡히지 않는다
    set({ industryId: id, selectedEdgeId: null, pinnedId: null, roleFilter: null });
  },
  setMinScore(v) {
    set({ minScore: Math.max(0, Math.min(100, v)) });
  },
  toggleType(t) {
    set((s) => {
      const next = new Set(s.activeTypes);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return { activeTypes: next };
    });
  },
  toggleEdgeBudget() {
    set((s) => ({ edgeBudget: !s.edgeBudget }));
  },
  setAltitude(on) {
    set({ altitudeOn: on });
  },
  setAltitudeWindow(w) {
    set({ altitudeWindow: w });
  },
  toggleLabels() {
    set((s) => ({ showLabels: !s.showLabels }));
  },
  setPanelTab(t) {
    set({ panelTab: t });
  },
  setWindow(w) {
    set({ window: w });
  },
  setPricePeriod(p) {
    set({ pricePeriod: p });
  },
  setSearchOpen(v) {
    set({ searchOpen: v });
  },
  setPanelOpen(v) {
    set({ panelOpen: v });
  },
  toggleLegend() {
    set((s) => ({ legendOpen: !s.legendOpen }));
  },
  hideCallout() {
    set({ calloutHidden: true });
  },
}));
