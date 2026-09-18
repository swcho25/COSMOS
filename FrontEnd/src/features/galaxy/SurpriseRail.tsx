import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { cycleTabFocus, EdgeGlyph, Empty, Icon } from "@/components/ui";
import { fmtScore } from "@/lib/format";
import type { SceneModel } from "@/lib/graph";
import { SURPRISE_UI } from "@/lib/guide";
import { relationshipMeta } from "@/lib/meta";
import { rankSurprises, type Surprise } from "@/lib/surprise";
import { useNarrowViewport } from "@/lib/viewport";
import { useGalaxy } from "@/store/galaxy";
import { beamColorFor } from "@/three/palette";
import "./surprise.css";

interface Props {
  /** 은하 뷰면 galaxyModel, 기업 관계망이면 systemModel — GalaxyPage 가 phase 에 맞춰 넘긴다 */
  model: SceneModel | null;
  onPickCompany: (id: string) => void;
  /** 모바일 상단 둘째 줄에 산업 선택판과 나란히 놓일 때 — 자리를 잡는 <aside> 없이 꼬리표 버튼만 그린다(자리는 GalaxyPage 의 .hud-top-line 이 정한다) */
  inline?: boolean;
}

/**
 * 왼쪽 "Special Link" 레일 — 관계가 많은 기업끼리의 뻔한 연결 말고, 산업·시장을 건너뛰거나 경쟁하면서도 손잡은
 * 특별한 관계를 골라 보여 준다. 이 제품의 첫 약속을 화면에서 지키는 카드다.
 * lib/surprise 의 순위를 지금 화면 필터(최소 점수·활성 유형)로 한 번 더 걸러 씬에 실제로 보이는 간선만 남긴다.
 * 행에 호버·포커스하면 씬의 그 빔이 밝아지고(기존 emphasis 규칙), 누르면 관계 패널이 근거와 함께 연다 —
 * "3클릭 내 근거" 를 1클릭으로 줄이는 지점이다.
 */
export default function SurpriseRail({ model, onPickCompany, inline = false }: Props) {
  const phase = useGalaxy((s) => s.phase);
  const surpriseOpen = useGalaxy((s) => s.surpriseOpen);
  const toggleSurprise = useGalaxy((s) => s.toggleSurprise);
  const minScore = useGalaxy((s) => s.minScore);
  const activeTypes = useGalaxy((s) => s.activeTypes);
  const selectEdge = useGalaxy((s) => s.selectEdge);
  const setHoveredEdge = useGalaxy((s) => s.setHoveredEdge);
  // 좁은 화면(900px 이하, lib/viewport)에서는 카드를 왼쪽 레일 대신 바텀시트로 띄운다 — 꼬리표는 제자리(상단 둘째 줄 또는 단독 레일)에 남는다
  const narrow = useNarrowViewport();

  const rows = useMemo(() => {
    if (!model) return [];
    const incidentTo = model.kind === "system" ? model.centerId : null;
    const ranked = rankSurprises(model, { limit: 6, incidentTo, perCompany: 2 });
    // 순위는 모델 전체 기준 — 지금 화면에 실제로 보이는 간선(최소 점수·활성 유형)만 한 번 더 남긴다
    return ranked.filter((s) => {
      const e = model.edgeById.get(s.edgeId);
      return e !== undefined && e.score >= minScore && activeTypes.has(e.type);
    });
  }, [model, minScore, activeTypes]);

  /* 모바일 바텀시트 열림은 로컬 상태다 — 데스크톱 레일의 열림(store.surpriseOpen, 기본 true)과 묶으면 폰에서 첫 화면부터 시트가 덮는다.
     항상 닫힌 채 시작하고, 꼬리표 탭으로 열고 닫는다 */
  const [sheetOpen, setSheetOpen] = useState(false);
  /* 시트가 열리면 닫기 버튼으로 포커스를 옮긴다 — 다이얼로그 관례이고, 그래야 ESC·Tab 순환이 시트 안에서 잡힌다 */
  const sheet = narrow && sheetOpen;
  const closeBtn = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (sheet) closeBtn.current?.focus();
  }, [sheet]);

  if (phase === "warp" || !model) return null;

  const centerId = model.kind === "system" ? model.centerId : null;
  const open = narrow ? sheetOpen : surpriseOpen;
  const toggle = narrow ? () => setSheetOpen((v) => !v) : toggleSurprise;
  const closeSheet = () => setSheetOpen(false);

  const tag = (
    <button type="button" className={`hud-tag${sheet ? " on" : ""}`} onClick={toggle} title={SURPRISE_UI.kicker} aria-expanded={narrow ? open : undefined}>
      <span aria-hidden="true">{SURPRISE_UI.mark}</span> {SURPRISE_UI.kicker} · {SURPRISE_UI.expandTag(rows.length)}
    </button>
  );
  if (!open) return inline ? tag : <aside className="hud hud-surprise">{tag}</aside>;

  // 제목 줄은 기업 중심 뷰에서만 — 은하 뷰에서는 머리의 'Special Link' 가 곧 이름이라 한 줄 더 둘 이유가 없다
  const centerName = centerId ? model.nodeById.get(centerId)?.name : undefined;
  /* 모바일에서는 행이나 기업을 고르면 시트를 닫는다 — 관계 패널도 바텀시트, 기업 관계망은 화면 전체라 두 장이 겹치지 않게 */
  const pickEdge = narrow ? (id: string | null) => (selectEdge(id), closeSheet()) : selectEdge;
  const pickCompany = narrow ? (id: string) => (onPickCompany(id), closeSheet()) : onPickCompany;
  /* ESC 는 시트만 닫는다(GalaxyPage 의 전역 ESC 까지 내려가지 않게), Tab 은 시트 안에서 돈다 */
  function onSheetKey(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key === "Escape") {
      e.stopPropagation();
      closeSheet();
      return;
    }
    cycleTabFocus(e, e.currentTarget);
  }

  const card = (
    <div className={`card surprise-card${narrow ? " surprise-sheet" : ""}`} role={narrow ? "dialog" : undefined} aria-modal={narrow ? true : undefined} aria-label={narrow ? SURPRISE_UI.kicker : undefined} onKeyDown={narrow ? onSheetKey : undefined}>
      <div className="row between surprise-head">
        <span className="kick surprise-kick">
          <span aria-hidden="true">{SURPRISE_UI.mark}</span> {SURPRISE_UI.kicker}
        </span>
        <button ref={closeBtn} type="button" className="toggle" onClick={toggle} aria-label={SURPRISE_UI.collapse} title={SURPRISE_UI.collapse}>
          <Icon.Close />
        </button>
      </div>
      {centerName && <div className="surprise-title">{SURPRISE_UI.titleFor(centerName)}</div>}
      {!centerId && <p className="meta surprise-lead">{SURPRISE_UI.lead}</p>}
      {rows.length === 0 ? (
        <Empty>{centerId ? SURPRISE_UI.emptyCompany : SURPRISE_UI.empty}</Empty>
      ) : (
        <>
          <div className="stack surprise-rows">
            {rows.map((s) => (
              <SurpriseRow key={s.edgeId} model={model} surprise={s} centerId={centerId} onPickCompany={pickCompany} selectEdge={pickEdge} setHoveredEdge={setHoveredEdge} />
            ))}
          </div>
          {/* 호버 안내는 포인터가 있는 화면에서만 */}
          {!narrow && <div className="meta surprise-hint">{SURPRISE_UI.hoverHint}</div>}
        </>
      )}
    </div>
  );

  if (narrow) {
    /* 모바일: 꼬리표는 제자리에 두고, 카드는 스크림과 함께 화면 아래에서 바텀시트로 올라온다 (surprise.css .surprise-sheet) */
    const body = (
      <>
        {tag}
        <div className="surprise-scrim" onClick={closeSheet} aria-hidden="true" />
        {card}
      </>
    );
    return inline ? body : <aside className="hud hud-surprise">{body}</aside>;
  }

  return <aside className="hud hud-surprise">{card}</aside>;
}

function SurpriseRow({
  model,
  surprise,
  centerId,
  onPickCompany,
  selectEdge,
  setHoveredEdge,
}: {
  model: SceneModel;
  surprise: Surprise;
  centerId: string | null;
  onPickCompany: (id: string) => void;
  selectEdge: (id: string | null) => void;
  setHoveredEdge: (id: string | null) => void;
}) {
  const edge = model.edgeById.get(surprise.edgeId);
  const a = edge ? model.nodeById.get(edge.source) : undefined;
  const b = edge ? model.nodeById.get(edge.target) : undefined;
  if (!edge || !a || !b) return null;

  const reasons = surprise.reasons.slice(0, 2);
  const enter = () => setHoveredEdge(edge.id);
  // 스토어를 구독하지 않고 getState 로 지금 값만 확인한다 — 다른 행의 호버로 이 레일 전체가 리렌더되지 않게 하기 위해서다
  const leave = () => {
    if (useGalaxy.getState().hoveredEdgeId === edge.id) setHoveredEdge(null);
  };
  const pick = (id: string) => (ev: React.MouseEvent) => {
    ev.stopPropagation();
    onPickCompany(id);
  };

  return (
    <div
      className="surprise-row"
      role="button"
      tabIndex={0}
      onMouseEnter={enter}
      onMouseLeave={leave}
      onFocus={enter}
      onBlur={leave}
      onClick={() => selectEdge(edge.id)}
      onKeyDown={(e) => {
        // 안쪽 기업 버튼에서 올라온 키는 그 버튼의 것 — 여기서 가로채면 Enter 가 기업 이동 대신 관계 패널을 연다
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          selectEdge(edge.id);
        }
      }}
      title={SURPRISE_UI.tooltip(reasons.map((r) => r.label))}
    >
      <div className="row between surprise-pair-row">
        <span className="surprise-pair">
          <button type="button" className={`surprise-co ${centerId === a.id ? "is-center" : ""}`} onClick={pick(a.id)} title={`${a.name} · 관계망으로`}>
            {a.name}
          </button>
          <EdgeGlyph directed={edge.directed} color={beamColorFor(edge.type)} />
          <button type="button" className={`surprise-co ${centerId === b.id ? "is-center" : ""}`} onClick={pick(b.id)} title={`${b.name} · 관계망으로`}>
            {b.name}
          </button>
        </span>
        <span className="num surprise-score">{fmtScore(edge.score)}</span>
      </div>
      <div className="row wrap surprise-reasons">
        <span className="meta">{relationshipMeta(edge.type).label}</span>
        {reasons.map((r) => (
          <span key={r.key} className="chip chip-sm" title={r.detail}>
            {r.label}
          </span>
        ))}
      </div>
    </div>
  );
}
