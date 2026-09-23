import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { useGalaxy } from "@/store/galaxy";

/** 패널이 열리고 닫히는 애니메이션 길이 — galaxy.css 의 .hud-panel 등장/퇴장 keyframe·.stage 폭 전환과 같은 값으로 맞춘다 */
export const PANEL_TRANSITION_MS = 1200;

/**
 * 패널을 "열림" 신호가 꺼진 뒤에도 애니메이션이 끝날 때까지 잠시 더 그려 준다.
 * React 는 조건부 렌더링을 즉시 언마운트하므로, 퇴장 애니메이션(.panel-closing)을 재생할 시간이 없었다 —
 * 이 훅이 그 사이를 잠깐 "닫히는 중" 상태로 붙잡아 GalaxyPage/PathGalaxyPage 가 PanelFrame 을 계속 그리게 한다.
 * children 은 active 인 동안에는 항상 최신 내용을 그대로 통과시키고, 닫히는 동안에는 마지막으로 봤던 내용을
 * state 로 얼려 그대로 보여준다(그 사이 선택 상태가 비어도 패널 내용이 갑자기 빈 값을 시도하다 깜빡이지 않는다).
 * active 가 바뀌는 순간의 단계 전환은 렌더 중에 바로 반영한다(React 의 "이전 값과 비교해 렌더 중 state 조정" 패턴) —
 * ref 를 렌더 중에 읽거나 쓰지 않기 위해서다. 실제 타이머(퇴장 애니메이션이 끝난 뒤 완전히 치우기)만 effect 에 남긴다.
 *
 * contentKey 는 children 이 "같은 내용" 인지 판단하는 안정적인 식별자다(예: 선택된 기업/간선 id).
 * children(JSX 엘리먼트)은 호출부가 매 렌더마다 새로 만들어 내는 객체라 참조로 비교하면 절대 같아지지
 * 않는다 — 그 값으로 "바뀌었으면 얼린다" 를 하면 조건이 항상 참이 되어 렌더 중 setState 가 매번 실행되고
 * 그때마다 다시 렌더 → 다시 변경 감지 → 다시 setState 로 무한 루프(React error #301, "Too many re-renders")에
 * 빠진다. contentKey(문자열/ null)는 실제 선택이 바뀔 때만 변하므로 이 비교가 정상적으로 안정된다.
 */
export function useLingeringPanel(active: boolean, children: ReactNode, contentKey: string | null, ms: number = PANEL_TRANSITION_MS): { node: ReactNode; show: boolean; closing: boolean } {
  const [frozenChildren, setFrozenChildren] = useState(children);
  const [frozenKey, setFrozenKey] = useState(contentKey);
  const [phase, setPhase] = useState<"shown" | "closing" | "hidden">(active ? "shown" : "hidden");
  const [prevActive, setPrevActive] = useState(active);

  // active 가 막 바뀌었다면 — 렌더 중에 바로 반영한다 (다음 커밋을 기다리는 effect 대신)
  if (active !== prevActive) {
    setPrevActive(active);
    setPhase(active ? "shown" : "closing");
  }
  // active 인 동안 내용이 바뀔 때마다(다른 기업/간선 선택) 마지막 내용을 다시 얼려 둔다 — contentKey 비교라 안정적으로 멈춘다
  if (active && contentKey !== frozenKey) {
    setFrozenKey(contentKey);
    setFrozenChildren(children);
  }

  useEffect(() => {
    if (phase !== "closing") return;
    const t = setTimeout(() => setPhase("hidden"), ms);
    return () => clearTimeout(t);
  }, [phase, ms]);

  return { node: active ? children : frozenChildren, show: phase !== "hidden", closing: phase === "closing" };
}

/** 이만큼 끌어 내리면 한 단계 접힌다 — 전체 펼침 → 살짝 보기, 살짝 보기 → 닫힘 */
const COLLAPSE_PX = 90;
/** 살짝 보기에서 이만큼 끌어 올리면 전체로 */
const EXPAND_PX = 40;
/** 위로 끄는 동안 시트가 따라 올라가는 한도 — 그 이상은 '저항' 으로 멈춰 손을 떼면 펼쳐진다는 신호만 준다 */
const LIFT_MAX_PX = 24;

interface SheetProps {
  /** 끌어 내려 닫을 때 — 패널의 onClose 와 같은 일을 한다 */
  onDismiss: () => void;
  children: ReactNode;
  /** useLingeringPanel 이 붙잡아 두고 있는 "닫히는 중" 구간 — true 인 동안 퇴장 애니메이션(.panel-closing)을 재생한다 */
  closing?: boolean;
}

/**
 * 모바일 바텀시트(900px 이하) — 우측 인텔리전스·관계 패널을 담는 .hud-panel 의 모바일 껍데기.
 * 두 단계다: 살짝 보기(is-peek — 기업 패널은 머리·연결 단계·기업·탭 줄까지, 관계 패널은 62dvh)와 전체 펼침(is-full — 위 8px 까지).
 * 맨 위 손잡이(.sheet-grip)를 끌어 내리면 한 단계 접히고(전체 → 살짝 → 닫힘), 살짝 보기에서 끌어 올리거나 손잡이를 탭하면 전체로 펼쳐진다.
 * 기업 패널의 탭(뉴스·주가·게시판)을 누르면 CompanyPanel 이 setSheetExpanded(true) 로 전체로 올린다. 단계는 store.sheetExpanded 가 들고,
 * 새 기업·관계를 고르면 스토어가 살짝 보기로 되돌린다. 끄는 동안의 이동은 React 상태가 아니라 DOM transform 으로 그린다 —
 * 포인터 이동마다 패널 전체(쿼리 포함)가 다시 렌더되지 않게.
 */
export default function MobileSheet({ onDismiss, children, closing }: SheetProps) {
  const expanded = useGalaxy((s) => s.sheetExpanded);
  const setExpanded = useGalaxy((s) => s.setSheetExpanded);
  const frame = useRef<HTMLDivElement>(null);
  const startY = useRef<number | null>(null);
  const moved = useRef(false);
  const [dragging, setDragging] = useState(false);

  function onDown(e: ReactPointerEvent<HTMLButtonElement>) {
    startY.current = e.clientY;
    moved.current = false;
    setDragging(true);
    // 손가락이 손잡이 밖으로 나가도 move/up 이 계속 오도록. 합성 이벤트(테스트)처럼 활성 포인터가 아니면 던지므로 조용히 넘긴다
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* 포인터 캡처 불가 — 끌기는 손잡이 위에서만 이어진다 */
    }
  }
  function onMove(e: ReactPointerEvent<HTMLButtonElement>) {
    if (startY.current === null || !frame.current) return;
    const d = e.clientY - startY.current;
    if (Math.abs(d) > 4) moved.current = true;
    // 아래로는 그대로 따라가고, 위로는 조금만 따라간다 (시트는 바닥에 붙어 있어 위로 올릴 빈자리가 없다)
    frame.current.style.transform = `translateY(${Math.max(-LIFT_MAX_PX, d)}px)`;
  }
  function onUp(e: ReactPointerEvent<HTMLButtonElement>) {
    if (startY.current === null) return;
    const d = e.clientY - startY.current;
    startY.current = null;
    setDragging(false);
    if (frame.current) frame.current.style.transform = "";
    if (d > COLLAPSE_PX) {
      if (expanded) setExpanded(false);
      else onDismiss();
    } else if (d < -EXPAND_PX && !expanded) {
      setExpanded(true);
    }
  }
  /* 손잡이를 끌지 않고 탭하면 단계를 토글한다 (끈 뒤에 따라오는 click 은 무시) */
  function onTap() {
    if (moved.current) {
      moved.current = false;
      return;
    }
    setExpanded(!expanded);
  }

  return (
    <div ref={frame} className={`hud hud-panel sheet ${expanded ? "is-full" : "is-peek"}${dragging ? " is-dragging" : ""}${closing ? " panel-closing" : ""}`}>
      <button
        type="button"
        className="sheet-grip"
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onClick={onTap}
        aria-label={expanded ? "시트 접기 — 아래로 끌면 접히고, 더 끌면 닫힙니다" : "시트 펼치기 — 위로 끌거나 누르면 전체로 펼쳐집니다"}
        title={expanded ? "아래로 끌어 접기" : "위로 끌어 펼치기"}
      >
        <i aria-hidden="true" />
      </button>
      {children}
    </div>
  );
}

/** 패널 껍데기 — 넓은 화면은 지금까지의 .hud-panel 그대로, 모바일은 바텀시트(MobileSheet) */
export function PanelFrame({ narrow, onDismiss, children, closing }: { narrow: boolean } & SheetProps) {
  if (narrow) return (
    <MobileSheet onDismiss={onDismiss} closing={closing}>
      {children}
    </MobileSheet>
  );
  return <div className={`hud hud-panel${closing ? " panel-closing" : ""}`}>{children}</div>;
}
