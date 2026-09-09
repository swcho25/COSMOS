import { create } from "zustand";
import type { MetricWindow, PricePeriod, RelationshipType } from "@/api/types";
import { RELATIONSHIP_ORDER } from "@/lib/meta";
import type { RoleKey } from "@/lib/roles";

export type PanelTab = "news" | "price" | "board";
export type Phase = "galaxy" | "warp" | "system";
export type NavMode = "orbit" | "fly";

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
  industryId: string | null;
  minScore: number;
  activeTypes: Set<RelationshipType>;
  showLabels: boolean;
  /** 간선 예산: 켜면 점수 상위 관계만 기본 표시, 나머지는 노드 호버 때만 */
  edgeBudget: boolean;

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
  /** 카메라 조작 방식: orbit = 중심을 도는 궤도 카메라, fly = WASD 로 직접 날아다니는 탐색 카메라 */
  navMode: NavMode;

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
  setIndustry(id: string | null): void;
  setMinScore(v: number): void;
  toggleType(t: RelationshipType): void;
  toggleLabels(): void;
  toggleEdgeBudget(): void;
  setPanelTab(t: PanelTab): void;
  setWindow(w: MetricWindow): void;
  setPricePeriod(p: PricePeriod): void;
  setSearchOpen(v: boolean): void;
  setPanelOpen(v: boolean): void;
  toggleLegend(): void;
  hideCallout(): void;
  setNavMode(m: NavMode): void;
  toggleNavMode(): void;
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

  industryId: null,
  minScore: 0,
  activeTypes: new Set(RELATIONSHIP_ORDER),
  showLabels: true,
  edgeBudget: true,

  panelTab: "news",
  window: "30D",
  pricePeriod: "1M",
  searchOpen: false,
  panelOpen: true,
  legendOpen: true,
  calloutHidden: false,
  navMode: "orbit",

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
    set((s) => ({ pinnedId: s.pinnedId === id ? null : id }));
  },
  setRoleFilter(r) {
    set((s) => ({ roleFilter: s.roleFilter === r ? null : r }));
  },

  setIndustry(id) {
    set({ industryId: id, selectedEdgeId: null });
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
  setNavMode(m) {
    set({ navMode: m });
  },
  toggleNavMode() {
    set((s) => ({ navMode: s.navMode === "fly" ? "orbit" : "fly" }));
  },
  toggleLegend() {
    set((s) => ({ legendOpen: !s.legendOpen }));
  },
  hideCallout() {
    set({ calloutHidden: true });
  },
}));
