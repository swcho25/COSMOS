import { useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { useGalaxy } from "@/store/galaxy";

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
}

/**
 * 모바일 바텀시트(900px 이하) — 우측 인텔리전스·관계 패널을 담는 .hud-panel 의 모바일 껍데기.
 * 두 단계다: 살짝 보기(is-peek — 기업 패널은 머리·연결 단계·기업·탭 줄까지, 관계 패널은 62dvh)와 전체 펼침(is-full — 위 8px 까지).
 * 맨 위 손잡이(.sheet-grip)를 끌어 내리면 한 단계 접히고(전체 → 살짝 → 닫힘), 살짝 보기에서 끌어 올리거나 손잡이를 탭하면 전체로 펼쳐진다.
 * 기업 패널의 탭(뉴스·주가·게시판)을 누르면 CompanyPanel 이 setSheetExpanded(true) 로 전체로 올린다. 단계는 store.sheetExpanded 가 들고,
 * 새 기업·관계를 고르면 스토어가 살짝 보기로 되돌린다. 끄는 동안의 이동은 React 상태가 아니라 DOM transform 으로 그린다 —
 * 포인터 이동마다 패널 전체(쿼리 포함)가 다시 렌더되지 않게.
 */
export default function MobileSheet({ onDismiss, children }: SheetProps) {
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
    <div ref={frame} className={`hud hud-panel sheet ${expanded ? "is-full" : "is-peek"}${dragging ? " is-dragging" : ""}`}>
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
export function PanelFrame({ narrow, onDismiss, children }: { narrow: boolean } & SheetProps) {
  if (narrow) return <MobileSheet onDismiss={onDismiss}>{children}</MobileSheet>;
  return <div className="hud hud-panel">{children}</div>;
}
