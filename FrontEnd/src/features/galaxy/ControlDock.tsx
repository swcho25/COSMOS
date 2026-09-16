import { type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { AltitudeWindow } from "@/api/types";
import { Icon } from "@/components/ui";
import { DOCK_UI } from "@/lib/guide";
import { RELATIONSHIP_ORDER, relationshipMeta } from "@/lib/meta";
import { spreadStage } from "@/lib/spread";
import { useGalaxy } from "@/store/galaxy";
import { DEFAULT_NEWS_WEIGHT, pct, useWeight } from "@/store/weight";
import "./dock.css";

/**
 * 하단 중앙 조절 도크 — Human Atlas 의 "Explode anatomy" 카드와 같은 자리.
 * 접힌 상태는 '^' 알약 버튼 하나만, 펼치면 펼치기·관계 종류·(고급) 뉴스·공시 비율·최소 점수·주가 고도·초기화가
 * 한 카드에 가로로 이어진다. 열림·고급 여부는 로컬 상태 없이 store/galaxy 의 dockOpen·dockAdvanced 를 그대로 쓴다.
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
  const isMixDefault = newsWeight === DEFAULT_NEWS_WEIGHT;

  function commitSpread(v: number) {
    setSpread(v / 100);
  }
  function releaseSpread(e: ReactPointerEvent<HTMLInputElement> | ReactKeyboardEvent<HTMLInputElement>) {
    const v = Number(e.currentTarget.value);
    const snapped = snapValue(v);
    if (snapped !== v) commitSpread(snapped);
  }
  function handleReset() {
    resetControls();
    useWeight.getState().reset();
  }

  return (
    <div className="hud hud-dock">
      <div className="card dock-card">
        {inGalaxyView && (
          <>
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
                onPointerUp={releaseSpread}
                onKeyUp={releaseSpread}
                title={DOCK_UI.spreadTitle}
              />
              <div className="dock-stages">
                <span className={stage === "galaxy" ? "on" : undefined}>{DOCK_UI.stages.galaxy}</span>
                <span className={stage === "flat" ? "on" : undefined}>{DOCK_UI.stages.flat}</span>
                <span className={stage === "sorted" ? "on" : undefined}>{DOCK_UI.stages.sorted}</span>
              </div>
            </div>
            <span className="dock-div" aria-hidden="true" />
          </>
        )}

        <div className="dock-col dock-types" title={DOCK_UI.types}>
          {RELATIONSHIP_ORDER.map((t) => {
            const m = relationshipMeta(t);
            const on = activeTypes.has(t);
            return (
              <button key={t} type="button" className={`chip chip-sm ${on ? "on" : ""}`} onClick={() => toggleType(t)} title={m.description}>
                {m.label}
              </button>
            );
          })}
        </div>

        <span className="dock-div" aria-hidden="true" />

        {/* 초기화(위) · 고급 '>'(아래) — 글자 없이 아이콘만, 설명은 title */}
        <div className="dock-side">
          <button type="button" className="dock-icon dock-reset" onClick={handleReset} title={DOCK_UI.resetTitle} aria-label={DOCK_UI.reset}>
            <Icon.Refresh />
          </button>
          <button type="button" className={`dock-icon dock-adv-toggle ${dockAdvanced ? "on" : ""}`} onClick={() => setDockAdvanced(!dockAdvanced)} title={DOCK_UI.advancedTitle} aria-expanded={dockAdvanced} aria-label={DOCK_UI.advanced}>
            <Icon.Chevron />
          </button>
        </div>

        {dockAdvanced && (
          <>
            <span className="dock-div" aria-hidden="true" />
            <div className="dock-col dock-advanced">
              <div className="dock-row dock-mix">
                {/* 라벨이 곧 값이다 — "뉴스 50 · 공시 50" 한 줄로 비율을 읽는다 */}
                <span className="lab num dock-mix-val" title={DOCK_UI.mixTitle}>
                  뉴스 <b>{newsWeightPct}</b> · 공시 <b>{100 - newsWeightPct}</b>
                </span>
                <input type="range" min={0} max={100} step={1} value={newsWeightPct} onChange={(e) => setNewsWeight(Number(e.target.value) / 100)} title={DOCK_UI.mixTitle} aria-label={DOCK_UI.mix} />
                {!isMixDefault && (
                  <button type="button" className="btn btn-xs dock-mix-reset" onClick={() => useWeight.getState().reset()}>
                    {DOCK_UI.mixReset}
                  </button>
                )}
              </div>

              <div className="dock-row dock-minscore">
                <span className="lab">{DOCK_UI.minScore}</span>
                <input type="range" min={0} max={90} step={5} value={minScore} onChange={(e) => setMinScore(Number(e.target.value))} />
                <span className="meta num">{minScore}</span>
              </div>

              {inGalaxyView && (
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
              )}
            </div>
          </>
        )}

        {/* 접기 — 카드 위 가장자리 가운데로 볼록 튀어나온 손잡이 */}
        <button type="button" className="dock-close" onClick={() => setDockOpen(false)} title={DOCK_UI.close} aria-label={DOCK_UI.close}>
          <Icon.Chevron />
        </button>
      </div>
    </div>
  );
}
