import { useMemo, useState } from "react";
import type { LatestGraph } from "@/api/types";
import { industryColor, relationshipMeta } from "@/lib/meta";
import { Empty } from "@/components/ui";

interface Props {
  graph: LatestGraph | null;
  onPick: (companyId: string) => void;
}

const W = 1000;
const ROW = 58;

/**
 * 투자·지분 관계를 지배 방향(보유 → 피보유)의 트리로 그린다.
 * 지주사(보유 기업)를 고르면 그 기업이 보유한 기업들과, 그 기업들이 다시 보유한 기업까지 2단계를 펼친다.
 */
export default function OwnershipView({ graph, onPick }: Props) {
  const invest = useMemo(() => graph?.edges.filter((e) => e.relationshipType === "INVEST") ?? [], [graph]);
  const info = useMemo(() => new Map(graph?.nodes.map((n) => [n.companyId, n]) ?? []), [graph]);
  const holders = useMemo(() => {
    const m = new Map<string, number>();
    invest.forEach((e) => m.set(e.sourceCompanyId, (m.get(e.sourceCompanyId) ?? 0) + 1));
    return [...m.entries()].sort((a, b) => b[1] - a[1]).map(([id, n]) => ({ id, n, name: info.get(id)?.name ?? id }));
  }, [invest, info]);
  const [holder, setHolder] = useState<string | null>(null);
  const root = holder ?? holders[0]?.id ?? null;

  const tree = useMemo(() => {
    if (!root) return null;
    const level1 = invest.filter((e) => e.sourceCompanyId === root).sort((a, b) => b.score - a.score);
    const rows: { id: string; depth: number; parent: string; score: number; y: number }[] = [];
    let y = 0;
    level1.forEach((e) => {
      const l2 = invest.filter((x) => x.sourceCompanyId === e.targetCompanyId && x.targetCompanyId !== root).sort((a, b) => b.score - a.score);
      const start = y;
      rows.push({ id: e.targetCompanyId, depth: 1, parent: root, score: e.score, y: 0 });
      const idx = rows.length - 1;
      if (l2.length) {
        l2.forEach((x) => {
          rows.push({ id: x.targetCompanyId, depth: 2, parent: e.targetCompanyId, score: x.score, y: y * ROW });
          y += 1;
        });
        rows[idx].y = ((start + y - 1) / 2) * ROW;
      } else {
        rows[idx].y = y * ROW;
        y += 1;
      }
    });
    return { rows, height: Math.max(1, y) * ROW };
  }, [invest, root]);

  if (!graph) return <div className="own-wrap">그래프 스냅샷을 불러오는 중…</div>;
  if (!holders.length) return <Empty>스냅샷에 투자·지분 유형 관계가 없습니다.</Empty>;

  const meta = relationshipMeta("INVEST");
  const colX = [40, 380, 720];
  const boxW = 240;
  const boxH = 40;
  const rootY = tree ? tree.height / 2 : 0;
  const rootNode = root ? info.get(root) : undefined;

  return (
    <div className="own-wrap">
      <div className="row wrap" style={{ gap: 8 }}>
        <span className="lab" style={{ marginRight: 4 }}>
          보유 기업
        </span>
        {holders.slice(0, 14).map((h) => (
          <button key={h.id} type="button" className={`chip chip-sm ${root === h.id ? "on" : ""}`} onClick={() => setHolder(h.id)}>
            {h.name} <b className="num">{h.n}</b>
          </button>
        ))}
      </div>
      <div className="own-stage">
        {tree && rootNode && (
          <svg viewBox={`0 0 ${W} ${tree.height + 40}`} height={Math.min(720, tree.height + 40)} role="img" aria-label="지분 관계 트리">
            <defs>
              <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill={meta.color} />
              </marker>
            </defs>
            {tree.rows.map((r) => {
              const parent = r.depth === 1 ? { x: colX[0] + boxW, y: rootY + 20 } : { x: colX[1] + boxW, y: (tree.rows.find((p) => p.id === r.parent && p.depth === 1)?.y ?? 0) + 20 };
              const x2 = colX[r.depth];
              const y2 = r.y + 20;
              const mx = (parent.x + x2) / 2;
              return (
                <g key={`${r.parent}-${r.id}`}>
                  <path d={`M${parent.x},${parent.y + 20} C${mx},${parent.y + 20} ${mx},${y2 + 20} ${x2},${y2 + 20}`} fill="none" stroke={meta.color} strokeOpacity={0.35 + (r.score / 100) * 0.6} strokeWidth={1 + (r.score / 100) * 3} markerEnd="url(#arrow)" />
                  <text x={mx} y={(parent.y + y2) / 2 + 16} fontSize="11" fill="#AAB3C2" textAnchor="middle" fontVariant="tabular-nums">
                    {Math.round(r.score)}
                  </text>
                </g>
              );
            })}
            <Node x={colX[0]} y={rootY + 20} w={boxW} h={boxH} name={rootNode.name} sub={`${rootNode.industryName} · ${rootNode.stockCode}`} color={industryColor(rootNode.industryName)} root onClick={() => onPick(rootNode.companyId)} />
            {tree.rows.map((r) => {
              const n = info.get(r.id);
              return <Node key={`n-${r.parent}-${r.id}`} x={colX[r.depth]} y={r.y + 20} w={boxW} h={boxH} name={n?.name ?? r.id} sub={n ? `${n.industryName} · ${n.stockCode}` : ""} color={industryColor(n?.industryName)} onClick={() => onPick(r.id)} />;
            })}
          </svg>
        )}
      </div>
      <div className="row wrap mt-12" style={{ gap: 16 }}>
        <span className="meta row" style={{ gap: 6 }}>
          <i className="dot" style={{ background: meta.color }} /> 화살표 = 지배 방향 · 굵기와 숫자 = 관계 점수
        </span>
        <span className="meta">노드를 누르면 전체 은하의 기업 패널로 이동합니다. 지분율은 백엔드 관계 상세에 포함되면 표시됩니다.</span>
      </div>
    </div>
  );
}

function Node({ x, y, w, h, name, sub, color, root, onClick }: { x: number; y: number; w: number; h: number; name: string; sub: string; color: string; root?: boolean; onClick: () => void }) {
  return (
    <g className="own-node" transform={`translate(${x},${y})`} onClick={onClick} role="button" tabIndex={0}>
      <rect width={w} height={h} rx={12} strokeWidth={root ? 1.5 : 1} style={root ? { stroke: color } : undefined} />
      <rect x={0} y={0} width={5} height={h} rx={2} fill={color} />
      <text x={16} y={17} fontSize="13" fontWeight={700}>
        {name}
      </text>
      <text x={16} y={32} fontSize="11" fill="#8B95A1">
        {sub}
      </text>
    </g>
  );
}
