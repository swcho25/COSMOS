import { useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { AltitudeWindow } from "@/api/types";
import { Icon } from "@/components/ui";
import { DOCK_UI } from "@/lib/guide";
import { RELATIONSHIP_ORDER, relationshipMeta } from "@/lib/meta";
import { spreadStage } from "@/lib/spread";
import { useNarrowViewport } from "@/lib/viewport";
import { useGalaxy } from "@/store/galaxy";
import { pct, useWeight } from "@/store/weight";
import "./dock.css";

/**
 * 하단 중앙 조절 도크 — Human Atlas 의 "Explode anatomy" 카드와 같은 자리.
 * 넓은 화면: 기본은 펼친 상태(고급까지)이고, 접으면 '^' 알약 버튼 하나만 남는다. 펼치기·관계 종류·(고급) 뉴스·공시 비율·
 * 최소 점수·주가 고도가 한 카드에 가로로 이어지고, 맨 오른쪽 끝에 초기화·고급 토글을 세로로 둔다.
 * 모바일(900px 이하, lib/viewport): 같은 조절 수단을 두 장으로 나눈다 — 기본 장은 펼치기 슬라이더 옆에 관계 종류 4개를 2×2 로 두고,
 * '>' 를 누르면 기본 장이 왼쪽으로 밀려 나가며 고급 장(비율 · 최소 점수 · 주가 고도)이 오른쪽에서 들어온다. 초기화·'>'(열리면 '<') 열은
 * 두 장 모두에서 오른쪽에 그대로 있다. 스토어의 dockAdvanced 가 모바일에서는 '고급 장 보기' 를 뜻한다.
 * 열림·고급 여부는 로컬 상태 없이 store/galaxy 의 dockOpen·dockAdvanced 를 그대로 쓴다.
 */

const ALTITUDE_WINDOWS: AltitudeWindow[] = ["1D", "1M", "3M"];
/** 펼치기 슬라이더 스냅 지점과 허용 오차 — 손을 떼거나 키를 놓았을 때만 스냅한다 (드래그 중에는 그대로 따라간다) */
const SNAP_ANCHORS = [0, 50, 100];
const SNAP_TOLERANCE = 6;

function snapValue(v: number) {
  for (const anchor of SNAP_ANCHORS) {
    if (Math.abs(v - anchor) <= SNAP_TOLERANCE) return anchor;
  }
  return v;
}

export default function ControlDock() {
  const dockOpen = useGalaxy((s) => s.dockOpen);
  const setDockOpen = useGalaxy((s) => s.setDockOpen);
  const dockAdvanced = useGalaxy((s) => s.dockAdvanced);
  const setDockAdvanced = useGalaxy((s) => s.setDockAdvanced);
  const spread = useGalaxy((s) => s.spread);
  const setSpread = useGalaxy((s) => s.setSpread);
  const activeTypes = useGalaxy((s) => s.activeTypes);
  const toggleType = useGalaxy((s) => s.toggleType);
  const minScore = useGalaxy((s) => s.minScore);
  const setMinScore = useGalaxy((s) => s.setMinScore);
  const altitudeOn = useGalaxy((s) => s.altitudeOn);
  const setAltitude = useGalaxy((s) => s.setAltitude);
  const altitudeWindow = useGalaxy((s) => s.altitudeWindow);
  const setAltitudeWindow = useGalaxy((s) => s.setAltitudeWindow);
  const focusId = useGalaxy((s) => s.focusId);
  const resetControls = useGalaxy((s) => s.resetControls);

  const newsWeight = useWeight((s) => s.newsWeight);
  const setNewsWeight = useWeight((s) => s.setNewsWeight);
  const narrow = useNarrowViewport();
  /**
   * 뉴스·공시 비율 슬라이더의 드래그 중 표시값 — 드래그하는 동안은 이 값만 바뀌고(라벨만 갱신),
   * 실제 store(useWeight)·씬 재계산은 손을 떼거나(pointerUp) 키를 놓을 때(keyUp)만 한 번 커밋한다.
   * 매 onChange마다 커밋하면 은하 씬의 간선 지오메트리가 매 틱 다시 만들어져 드래그 중 렉이 생긴다.
   */
  const [draftNewsWeightPct, setDraftNewsWeightPct] = useState<number | null>(null);

  if (!dockOpen) {
    return (
      <div className="hud hud-dock">
        <button type="button" className="dock-open" onClick={() => setDockOpen(true)} title={DOCK_UI.open} aria-expanded={dockOpen}>
          <Icon.Chevron />
        </button>
      </div>
    );
  }

  const spreadPct = Math.round(spread * 100);
  const stage = spreadStage(spread);
  const inGalaxyView = focusId === null;
  const newsWeightPct = pct(newsWeight);

  function commitSpread(v: number) {
    setSpread(v / 100);
  }
  function releaseSpread(e: ReactPointerEvent<HTMLInputElement> | ReactKeyboardEvent<HTMLInputElement>) {
    const v = Number(e.currentTarget.value);
    const snapped = snapValue(v);
    if (snapped !== v) commitSpread(snapped);
  }
  /**
   * 마우스로 슬라이더를 놓으면 포커스를 떼어 준다 — 포커스가 남아 있으면 방향키가 씬이 아니라 이 슬라이더로 가서
   * "슬라이더를 만진 뒤 우주를 한 번 클릭해야" 카메라가 움직였다. 키보드(Tab)로 온 경우에는 그대로 둔다.
   */
  function releaseFocus(e: ReactPointerEvent<HTMLInputElement>) {
    e.currentTarget.blur();
  }
  function handleReset() {
    resetControls();
    useWeight.getState().reset();
    setDraftNewsWeightPct(null);
  }
  function releaseNewsWeight(e: ReactPointerEvent<HTMLInputElement> | ReactKeyboardEvent<HTMLInputElement>) {
    setNewsWeight(Number(e.currentTarget.value) / 100);
    setDraftNewsWeightPct(null);
  }

  /* ---- 조절 수단 조각 — 넓은 화면과 모바일 두 장이 같은 조각을 다른 자리에 놓는다 ---- */

  const spreadCol = (
    <div className="dock-col dock-spread">
      <div className="dock-row-head">
        <span className="lab" title={DOCK_UI.spreadTitle}>
          {DOCK_UI.spread}
        </span>
        <span className="chip chip-sm dock-val">{spreadPct}%</span>
      </div>
      <input
        type="range"
        min={0}
        max={100}
        step={1}
        value={spreadPct}
        onChange={(e) => commitSpread(Number(e.target.value))}
        onPointerUp={(e) => {
          releaseSpread(e);
          releaseFocus(e);
        }}
        onKeyUp={releaseSpread}
        title={DOCK_UI.spreadTitle}
      />
      <div className="dock-stages">
        <span className={stage === "galaxy" ? "on" : undefined}>{DOCK_UI.stages.galaxy}</span>
        <span className={stage === "flat" ? "on" : undefined}>{DOCK_UI.stages.flat}</span>
        <span className={stage === "sorted" ? "on" : undefined}>{DOCK_UI.stages.sorted}</span>
      </div>
    </div>
  );

  const typeChips = RELATIONSHIP_ORDER.map((t) => {
    const m = relationshipMeta(t);
    const on = activeTypes.has(t);
    return (
      <button key={t} type="button" className={`chip chip-sm ${on ? "on" : ""}`} onClick={() => toggleType(t)} title={m.description}>
        {m.label}
      </button>
    );
  });

  const displayNewsWeightPct = draftNewsWeightPct ?? newsWeightPct;
  const mixRow = (
    <div className="dock-row dock-mix">
      {/* 라벨이 곧 값이다 — "뉴스 50 · 공시 50" 한 줄로 비율을 읽는다. 드래그 중에는 draft 값을 보여 주고, 실제 반영은 놓을 때 한 번뿐이다 */}
      <span className="lab num dock-mix-val" title={DOCK_UI.mixTitle}>
        뉴스 <b>{displayNewsWeightPct}</b> · 공시 <b>{100 - displayNewsWeightPct}</b>
      </span>
      <input
        type="range"
        min={0}
        max={100}
        step={1}
        value={displayNewsWeightPct}
        onChange={(e) => setDraftNewsWeightPct(Number(e.target.value))}
        onPointerUp={(e) => {
          releaseNewsWeight(e);
          releaseFocus(e);
        }}
        onKeyUp={releaseNewsWeight}
        title={DOCK_UI.mixTitle}
        aria-label={DOCK_UI.mix}
      />
    </div>
  );

  const minScoreRow = (
    <div className="dock-row dock-minscore">
      <span className="lab">{DOCK_UI.minScore}</span>
      <input type="range" min={0} max={90} step={5} value={minScore} onChange={(e) => setMinScore(Number(e.target.value))} onPointerUp={releaseFocus} aria-label={DOCK_UI.minScore} />
      <span className="meta num">{minScore}</span>
    </div>
  );

  const altitudeRow = (
    <div className="dock-row dock-altitude">
      <button
        type="button"
        className={`toggle ${altitudeOn ? "on" : ""}`}
        onClick={() => setAltitude(!altitudeOn)}
        disabled={spread > 0}
        title={spread > 0 ? DOCK_UI.altitudeSuspended : DOCK_UI.altitudeTitle}
      >
        {DOCK_UI.altitude}
      </button>
      <div className="seg">
        {ALTITUDE_WINDOWS.map((w) => (
          <button key={w} type="button" className={altitudeWindow === w ? "on" : ""} onClick={() => setAltitudeWindow(w)} disabled={spread > 0} title={spread > 0 ? DOCK_UI.altitudeSuspended : undefined}>
            {w}
          </button>
        ))}
      </div>
    </div>
  );

  /* 초기화(위) · 고급 '<'(아래) — 카드 맨 오른쪽 끝. 글자 없이 아이콘만, 설명은 title.
     넓은 화면에서는 고급 그룹이 이 열의 왼쪽에서 열리고 닫히고, 모바일에서는 장이 바뀐다 — 두 버튼의 자리는 어느 쪽에서도 그대로다 */
  const tail = (
    <div className="dock-side dock-tail">
      <button type="button" className="dock-icon dock-reset" onClick={handleReset} title={DOCK_UI.resetTitle} aria-label={DOCK_UI.reset}>
        <Icon.Refresh />
      </button>
      <button type="button" className={`dock-icon dock-adv-toggle ${dockAdvanced ? "on" : ""}`} onClick={() => setDockAdvanced(!dockAdvanced)} title={DOCK_UI.advancedTitle} aria-expanded={dockAdvanced} aria-label={DOCK_UI.advanced}>
        <Icon.Chevron />
      </button>
    </div>
  );

  /* 접기 — 카드 위 가장자리 가운데로 볼록 튀어나온 손잡이 */
  const closeHandle = (
    <button type="button" className="dock-close" onClick={() => setDockOpen(false)} title={DOCK_UI.close} aria-label={DOCK_UI.close}>
      <Icon.Chevron />
    </button>
  );

  if (narrow) {
    return (
      <div className="hud hud-dock">
        <div className="card dock-card dock-mobile">
          {/* 두 장은 같은 자리에 겹쳐 있고 활성 장만 자리를 차지한다(카드 높이는 활성 장에 따른다). 비활성 장은 visibility 로 탭 순서에서도 빠진다 */}
          <div className="dock-panes">
            <section className={`dock-pane dock-pane-main${inGalaxyView ? "" : " no-spread"}${dockAdvanced ? "" : " is-active"}`} aria-label={DOCK_UI.types}>
              {inGalaxyView && spreadCol}
              <div className="dock-types-grid" title={DOCK_UI.types}>
                {typeChips}
              </div>
            </section>
            <section className={`dock-pane dock-pane-adv${dockAdvanced ? " is-active" : ""}`} aria-label={DOCK_UI.advanced}>
              {mixRow}
              {minScoreRow}
              {inGalaxyView && altitudeRow}
            </section>
          </div>
          {tail}
          {closeHandle}
        </div>
      </div>
    );
  }

  return (
    <div className="hud hud-dock">
      <div className="card dock-card">
        {inGalaxyView && (
          <>
            {spreadCol}
            <span className="dock-div" aria-hidden="true" />
          </>
        )}

        <div className="dock-col dock-types" title={DOCK_UI.types}>
          {typeChips}
        </div>

        {dockAdvanced && (
          <>
            <span className="dock-div" aria-hidden="true" />
            <div className="dock-col dock-advanced">
              {mixRow}
              {minScoreRow}
              {inGalaxyView && altitudeRow}
            </div>
          </>
        )}

        <span className="dock-div" aria-hidden="true" />
        {tail}
        {closeHandle}
      </div>
    </div>
  );
}
