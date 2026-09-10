import { useEffect, useRef, useState, type RefObject } from "react";
import { ImpactBadge, IndustryBadge, Skeleton, TypeBadge } from "@/components/ui";
import { fmtScore } from "@/lib/format";
import type { SceneModel } from "@/lib/graph";
import { scoreBand } from "@/lib/meta";
import { useRelationship } from "@/lib/queries";
import { ROLE_META, roleColor, roleCounts } from "@/lib/roles";
import { useGalaxy } from "@/store/galaxy";

export interface PointerPos {
  /** 뷰포트 기준 clientX/clientY — 툴팁이 자기 부모(.galaxy) 사각형으로 바꿔 쓴다 */
  x: number;
  y: number;
}

interface Props {
  model: SceneModel | null;
  pointer: RefObject<PointerPos>;
}

const TIP_W = 260;
const OFF_X = 14;
const OFF_Y = 16;

/** 값이 ms 동안 바뀌지 않았을 때만 따라가는 값 — 빔을 스치듯 지나갈 때 관계 상세 요청이 쏟아지지 않게 */
function useDebounced<T>(value: T, ms: number) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/**
 * 씬 툴팁(DOM) — 빔 호버: 유형·방향·양끝·점수·영향 + (지연) 근거 건수·신뢰 / 노드 호버: 이름·산업·역할 요약.
 * 위치는 React 상태가 아니라 포인터 ref + rAF 로 옮겨 포인터 이동마다 리렌더하지 않는다. 워프 중에는 그리지 않는다.
 * 관계 상세는 RelationshipPanel 과 같은 useRelationship 키라, 툴팁이 뜬 뒤 빔을 누르면 패널이 스켈레톤 없이 렌더된다.
 */
export default function SceneTip({ model, pointer }: Props) {
  const hoveredId = useGalaxy((s) => s.hoveredId);
  const hoveredEdgeId = useGalaxy((s) => s.hoveredEdgeId);
  const phase = useGalaxy((s) => s.phase);
  const window_ = useGalaxy((s) => s.window);
  const el = useRef<HTMLDivElement>(null);

  // 노드 위에 칩·행성이 있으면 노드가 우선 (간선은 nodeAhead 로 물러난다)
  const node = model && hoveredId ? model.nodeById.get(hoveredId) : undefined;
  const edge = !node && model && hoveredEdgeId ? model.edgeById.get(hoveredEdgeId) : undefined;
  const open = phase !== "warp" && Boolean(node || edge);

  // 지연 필드 — 150ms 머문 간선만 요청한다
  const debouncedEdgeId = useDebounced(edge?.id ?? null, 150);
  const rel = useRelationship(open && edge ? debouncedEdgeId : null, window_);
  const detail = rel.data && rel.data.relationshipId === edge?.id ? rel.data : null;

  // 열려 있는 동안만 rAF 로 포인터를 따라간다. 스테이지 우·하단을 넘치면 좌·상으로 뒤집는다
  useEffect(() => {
    if (!open) return;
    let raf = 0;
    const tick = () => {
      const d = el.current;
      const host = d?.parentElement;
      if (d && host && pointer.current) {
        const rect = host.getBoundingClientRect();
        const px = pointer.current.x - rect.left;
        const py = pointer.current.y - rect.top;
        const h = d.offsetHeight || 80;
        const x = px + OFF_X + TIP_W > rect.width ? px - OFF_X - TIP_W : px + OFF_X;
        const y = py + OFF_Y + h > rect.height ? py - OFF_Y - h : py + OFF_Y;
        d.style.transform = `translate(${Math.max(0, Math.round(x))}px, ${Math.max(0, Math.round(y))}px)`;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [open, pointer]);

  if (!open || !model) return null;

  if (node) {
    const counts = roleCounts(model, node.id);
    const total = counts.reduce((s, c) => s + c.count, 0);
    return (
      <div ref={el} className="scene-tip" role="tooltip">
        <div className="tip-head">
          <b>{node.name}</b>
          {node.industry && <IndustryBadge name={node.industry} />}
        </div>
        <div className="tip-roles meta">
          <span>관계 {total}</span>
          {counts.map((rc) => (
            <span key={rc.role} className="tip-role">
              <i className="dot" style={{ background: roleColor(rc.role) }} />
              {ROLE_META[rc.role].label} {rc.count}
            </span>
          ))}
        </div>
        {/* Shift+클릭 발견성이 낮아 노드 툴팁에도 힌트를 남긴다 — PathStrip 이 이 기준으로 최단 경로를 켠다 */}
        <div className="tip-hint">Shift+클릭으로 경로 기준 고정</div>
      </div>
    );
  }

  if (!edge) return null;
  const src = model.nodeById.get(edge.source);
  const tgt = model.nodeById.get(edge.target);
  return (
    <div ref={el} className="scene-tip" role="tooltip">
      <div className="row between">
        <TypeBadge type={edge.type} directed={edge.directed} />
        <ImpactBadge value={edge.impact} />
      </div>
      <div className="tip-pair">
        <span>{src?.name ?? "?"}</span>
        <span className="arrow" aria-hidden="true">
          {edge.directed ? "→" : "↔"}
        </span>
        <span>{tgt?.name ?? "?"}</span>
      </div>
      <div className="tip-score">
        <b className="num">{fmtScore(edge.score)}</b>
        <span className="meta">{scoreBand(edge.score)} · 100점 기준</span>
      </div>
      {/* 지연 필드 — 고정 높이로 두어 도착해도 카드가 튀지 않는다 */}
      <div className="tip-ev meta num">
        {detail ? (
          <>
            근거 {detail.evidenceCount}건 · 신뢰 {Math.round(detail.confidence * 100)}%
          </>
        ) : (
          <Skeleton h={12} w={120} />
        )}
      </div>
    </div>
  );
}
