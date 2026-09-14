import { useEffect, useState } from "react";
import { HINT_CHIPS } from "@/lib/guide";
import { useGalaxy } from "@/store/galaxy";
import { useUi } from "@/store/ui";
import { codeOf, isTyping } from "@/three/keyboard";
import { dragState } from "@/three/morph";
import "./hud-guide.css";

/** 다섯 힌트가 전부 확인된 뒤 행을 지우기까지의 유예(ms) — 사라지는 순간을 눈으로 좇지 않게 약간 둔다 */
const HIDE_DELAY_MS = 3000;

/**
 * 좌하단 조작 힌트 칩 1행 (P0-7). 실제 조작을 감지해 해당 칩만 회색으로 바꾼다 — 설명이 아니라 확인이다.
 * 다섯 개를 모두 해 보면 더 볼 것이 없으므로 잠시 뒤 행 전체를 숨긴다.
 */
export default function HintChips() {
  const seenHints = useUi((s) => s.seenHints);
  const markHint = useUi((s) => s.markHint);
  const [hidden, setHidden] = useState(false);

  useEffect(() => {
    const canvas = document.querySelector<HTMLCanvasElement>(".galaxy .stage canvas");
    if (!canvas) return;

    let down = false;
    let downX = 0;
    let downY = 0;
    let rotateMarked = false;

    const onPointerDown = (e: PointerEvent) => {
      down = true;
      downX = e.clientX;
      downY = e.clientY;
      rotateMarked = false;
    };
    const onPointerMove = (e: PointerEvent) => {
      if (!down) return;
      if (dragState.id !== null) {
        markHint("dragPlanet");
        return;
      }
      if (rotateMarked) return;
      const dx = e.clientX - downX;
      const dy = e.clientY - downY;
      if (dx * dx + dy * dy >= 64) {
        rotateMarked = true;
        markHint("rotate");
      }
    };
    const onPointerUp = () => {
      down = false;
    };
    const onWheel = () => markHint("zoom");
    const onKeyDown = (e: KeyboardEvent) => {
      if (isTyping()) return;
      const code = codeOf(e);
      if (code === "KeyW" || code === "KeyA" || code === "KeyS" || code === "KeyD") markHint("move");
    };

    canvas.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("wheel", onWheel, { passive: true });
    window.addEventListener("keydown", onKeyDown);
    const unsubPin = useGalaxy.subscribe((s, prev) => {
      if (s.pinnedId !== null && prev.pinnedId === null) markHint("path");
    });
    return () => {
      canvas.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("wheel", onWheel);
      window.removeEventListener("keydown", onKeyDown);
      unsubPin();
    };
  }, [markHint]);

  const allSeen = HINT_CHIPS.every((h) => seenHints.has(h.id));
  useEffect(() => {
    if (!allSeen) return;
    const t = window.setTimeout(() => setHidden(true), HIDE_DELAY_MS);
    return () => window.clearTimeout(t);
  }, [allSeen]);

  if (hidden) return null;

  return (
    <div className="hud hud-hints">
      <div className="hint-row">
        {HINT_CHIPS.map((h) => (
          <span key={h.id} className={`hint-chip ${seenHints.has(h.id) ? "seen" : ""}`}>
            <kbd>{h.keys}</kbd>
            {h.does}
          </span>
        ))}
      </div>
    </div>
  );
}
