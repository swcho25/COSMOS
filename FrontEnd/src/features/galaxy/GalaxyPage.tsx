import { useCallback, useEffect, useMemo, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import Scene, { type Callout } from "@/three/Scene";
import { layoutGalaxy, layoutSystem } from "@/lib/graph";
import { fmtDateTime, fmtRelative } from "@/lib/format";
import { industryColor } from "@/lib/meta";
import { useCompanyGraph, useIndustries, useLatestGraph, useNewsFeed } from "@/lib/queries";
import { useGalaxy } from "@/store/galaxy";
import { Icon, TypeLegend } from "@/components/ui";
import CompanyPanel from "./CompanyPanel";
import RelationshipPanel from "./RelationshipPanel";
import SearchPalette from "./SearchPalette";
import "./galaxy.css";

/**
 * 전체 은하 (화면구성도 ①). 산업 성단 위에 기업 항성과 관계 간선을 그리고,
 * 노드를 누르면 워프해 그 기업 중심 관계망(depth 궤도)으로 들어간다.
 */
export default function GalaxyPage() {
  const [params, setParams] = useSearchParams();
  const qc = useQueryClient();

  const industryId = useGalaxy((s) => s.industryId);
  const focusId = useGalaxy((s) => s.focusId);
  const phase = useGalaxy((s) => s.phase);
  const selectedEdgeId = useGalaxy((s) => s.selectedEdgeId);
  const minScore = useGalaxy((s) => s.minScore);
  const activeTypes = useGalaxy((s) => s.activeTypes);
  const showLabels = useGalaxy((s) => s.showLabels);
  const setIndustry = useGalaxy((s) => s.setIndustry);
  const setMinScore = useGalaxy((s) => s.setMinScore);
  const toggleType = useGalaxy((s) => s.toggleType);
  const toggleLabels = useGalaxy((s) => s.toggleLabels);
  const warpTo = useGalaxy((s) => s.warpTo);
  const selectEdge = useGalaxy((s) => s.selectEdge);
  const setSearchOpen = useGalaxy((s) => s.setSearchOpen);
  const panelOpen = useGalaxy((s) => s.panelOpen);
  const setPanelOpen = useGalaxy((s) => s.setPanelOpen);
  const legendOpen = useGalaxy((s) => s.legendOpen);
  const toggleLegend = useGalaxy((s) => s.toggleLegend);
  const calloutHidden = useGalaxy((s) => s.calloutHidden);
  const hideCallout = useGalaxy((s) => s.hideCallout);
  const navMode = useGalaxy((s) => s.navMode);
  const toggleNavMode = useGalaxy((s) => s.toggleNavMode);

  const industries = useIndustries();
  const universe = useLatestGraph(null); // 전체 우주 — 색·시장 조회용
  const latest = useLatestGraph(industryId);
  const companyGraph = useCompanyGraph(focusId);
  const focusNews = useNewsFeed({ companyId: focusId ?? undefined, size: 1 }, Boolean(focusId));

  const galaxyModel = useMemo(() => (latest.data ? layoutGalaxy(latest.data) : null), [latest.data]);
  const lookup = useMemo(() => {
    const m = new Map(universe.data?.nodes.map((n) => [n.companyId, n]) ?? []);
    return (id: string) => m.get(id);
  }, [universe.data]);
  const systemModel = useMemo(() => (companyGraph.data ? layoutSystem(companyGraph.data, lookup) : null), [companyGraph.data, lookup]);

  /* URL ?company= 와 포커스 동기화 — 진입 시 딥링크를 먼저 소비한 뒤에만 URL 을 상태에 맞춘다 */
  const pendingDeepLink = useRef(params.get("company"));
  useEffect(() => {
    const requested = pendingDeepLink.current;
    if (!requested || !galaxyModel) return;
    pendingDeepLink.current = null;
    if (requested !== useGalaxy.getState().focusId) warpTo(requested);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [galaxyModel]);
  useEffect(() => {
    if (pendingDeepLink.current) return;
    const cur = params.get("company");
    if ((focusId ?? null) === (cur ?? null)) return;
    const next = new URLSearchParams(params);
    if (focusId) next.set("company", focusId);
    else next.delete("company");
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusId]);

  /* 키보드: ESC 은하 복귀 */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const s = useGalaxy.getState();
      if (e.key === "Escape" && !s.searchOpen) {
        if (s.selectedEdgeId) selectEdge(null);
        else if (s.focusId && s.phase !== "warp") s.backToGalaxy();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectEdge]);

  const pickCompany = useCallback(
    (id: string) => {
      // 산업 필터 밖의 기업이면 필터를 풀고 이동한다
      const inFilter = !industryId || latest.data?.nodes.some((n) => n.companyId === id);
      if (!inFilter) setIndustry(null);
      warpTo(id);
    },
    [industryId, latest.data, setIndustry, warpTo],
  );

  const callout: Callout | null = useMemo(() => {
    const n = focusNews.data?.pages[0]?.items[0];
    if (!focusId || !n || phase !== "system" || calloutHidden) return null;
    return { companyId: focusId, kicker: "latest news signal", title: n.title, meta: `${n.publisher} · ${fmtRelative(n.publishedAt)}`, onClose: hideCallout };
  }, [focusNews.data, focusId, phase, calloutHidden, hideCallout]);

  const focusName = focusId ? systemModel?.nodeById.get(focusId)?.name ?? lookup(focusId)?.name ?? "…" : null;
  const snapshot = latest.data;
  const stale = snapshot ? Date.now() > Date.parse(snapshot.nextRefreshAt) : false;
  const loading = latest.isLoading || (Boolean(focusId) && companyGraph.isLoading && !systemModel);

  return (
    <div className="galaxy">
      <div className="stage">
        <Scene galaxy={galaxyModel} system={systemModel} callout={callout} />
      </div>

      {loading && (
        <div className="stage-loading" aria-live="polite">
          <span className="kick">news × relationship graph</span>
          <b>COSMOS</b>
          <i />
          <span>{focusId ? "기업 관계망을 불러오는 중" : "최신 스냅샷을 불러오는 중"}</span>
        </div>
      )}
      {latest.isError && (
        <div className="stage-loading" style={{ pointerEvents: "auto" }}>
          <b>연결 실패</b>
          <span>그래프 스냅샷을 가져오지 못했습니다.</span>
          <button type="button" className="btn btn-g btn-sm" onClick={() => latest.refetch()}>
            다시 시도
          </button>
        </div>
      )}

      {/* 상단: 검색 + 산업 칩 (은하 뷰에서만) */}
      {!focusId && (
      <div className="hud hud-top">
        <button type="button" className="field search-trigger" onClick={() => setSearchOpen(true)}>
          <Icon.Search />
          <span>기업명 또는 종목코드를 검색하세요</span>
          <span className="kbd">Ctrl K</span>
        </button>
        <div className="industry-chips" role="group" aria-label="산업 필터">
          <button type="button" className={`chip ${industryId === null ? "on" : ""}`} onClick={() => (setIndustry(null), focusId && warpTo(null))}>
            전체 <b className="num">{universe.data?.nodes.length ?? "-"}</b>
          </button>
          {industries.data?.items
            .filter((i) => i.companyCount > 0)
            .map((i) => (
              <button
                key={i.industryId}
                type="button"
                className={`chip ${industryId === i.industryId ? "on" : ""}`}
                onClick={() => {
                  setIndustry(industryId === i.industryId ? null : i.industryId);
                  if (focusId) warpTo(null);
                }}
              >
                <i style={{ background: industryColor(i.name) }} />
                {i.name} <b className="num">{i.companyCount}</b>
              </button>
            ))}
        </div>
      </div>
      )}

      {/* 좌상단: 기업 중심 뷰 브레드크럼 */}
      {focusId && (
        <div className="hud hud-crumb">
          <div className="crumb">
            <button type="button" onClick={() => warpTo(null)} title="은하로 돌아가기 (ESC)">
              <Icon.Back /> 전체 은하
            </button>
            <span aria-hidden="true">›</span>
            <b>{focusName}</b>
            <span className="meta">중심 관계망 · 최대 3단계</span>
          </div>
          <button type="button" className="crumb" onClick={() => setSearchOpen(true)} title="다른 기업 검색">
            <Icon.Search /> <span className="kbd">Ctrl K</span>
          </button>
        </div>
      )}

      {/* 좌하단: 상태 */}
      <div className="hud hud-status">
        <div className="card status-card">
          <div>
            <div className="t">
              <i className={`dot ${stale ? "warn" : ""}`} />
              {snapshot?.personalized ? "개인화 그래프" : "공개 스냅샷"}
              {stale && <span className="meta">· 새 스냅샷 가능</span>}
            </div>
            <div className="meta num" style={{ marginTop: 4 }}>
              {snapshot ? `${fmtDateTime(snapshot.asOfAt)} 기준 · 다음 갱신 ${fmtDateTime(snapshot.nextRefreshAt).slice(-5)}` : "불러오는 중"}
            </div>
          </div>
          <button type="button" onClick={() => qc.invalidateQueries({ queryKey: ["graph"] })} aria-label="그래프 새로고침" title="최신 스냅샷 확인">
            <Icon.Refresh />
          </button>
        </div>
      </div>

      {/* 하단 중앙: 카메라 모드 전환 + 도움말 */}
      <div className="hud hud-nav">
        <button type="button" className={`hud-tag nav-toggle ${navMode === "fly" ? "on" : ""}`} onClick={toggleNavMode} title="F 키로도 전환됩니다">
          <span className="nav-icon" aria-hidden>
            {navMode === "fly" ? "🚀" : "◎"}
          </span>
          {navMode === "fly" ? "탐색 모드" : "궤도 모드"}
          <kbd>F</kbd>
        </button>
      </div>
      {(navMode === "fly" || (!focusId && !selectedEdgeId)) && (
        <div className="hud hud-help">
          {navMode === "fly"
            ? "W A S D 이동 · Q E 위아래 · Shift 가속 · 드래그로 시선 · 휠로 전진 · 기업을 누르면 워프"
            : "드래그로 회전 · 휠로 확대 · 기업을 누르면 워프, 관계선을 누르면 근거를 봅니다"}
        </div>
      )}

      {/* 우하단: 관계 유형·최소 점수 (접으면 꼬리표) */}
      {!focusId && !selectedEdgeId && (
        <div className="hud hud-legend">
          {legendOpen ? (
            <div className="card legend-card">
              <div className="row between">
                <span className="kick">관계 유형 · 굵을수록 점수 높음</span>
                <div className="row" style={{ gap: 6 }}>
                  <button type="button" className={`toggle ${showLabels ? "on" : ""}`} onClick={toggleLabels}>
                    라벨
                  </button>
                  <button type="button" className="toggle" onClick={toggleLegend} aria-label="필터 접기" title="접기">
                    <Icon.Close />
                  </button>
                </div>
              </div>
              <TypeLegend active={activeTypes} onToggle={toggleType} />
              <div className="row-ctl">
                <span className="lab">최소 점수</span>
                <input type="range" min={0} max={90} step={5} value={minScore} onChange={(e) => setMinScore(Number(e.target.value))} aria-label="관계 최소 점수" />
                <span className="num" style={{ fontSize: 13, fontWeight: 700, width: 28, textAlign: "right" }}>
                  {minScore}
                </span>
              </div>
            </div>
          ) : (
            <button type="button" className="hud-tag" onClick={toggleLegend} title="관계 유형 · 최소 점수 필터 펼치기">
              필터 · 범례
              {(minScore > 0 || activeTypes.size < 4) && <i className="dot" style={{ background: "var(--primary-2)" }} />}
            </button>
          )}
        </div>
      )}

      {/* 우측 패널 — 닫으면 오른쪽 가장자리 꼬리표로 접힌다 */}
      {(focusId || selectedEdgeId) &&
        (panelOpen ? (
          <div className="hud hud-panel">
            {selectedEdgeId ? (
              <RelationshipPanel relationshipId={selectedEdgeId} onClose={() => setPanelOpen(false)} onPickCompany={pickCompany} />
            ) : (
              focusId && <CompanyPanel companyId={focusId} onClose={() => setPanelOpen(false)} onPickCompany={pickCompany} />
            )}
          </div>
        ) : (
          <div className="hud hud-panel-tag">
            <button type="button" className="hud-tag vertical" onClick={() => setPanelOpen(true)} title="패널 펼치기">
              {selectedEdgeId ? "기업 관계" : `${focusName} · 인텔리전스`}
            </button>
          </div>
        ))}

      <SearchPalette onPick={pickCompany} />
    </div>
  );
}
