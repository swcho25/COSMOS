import { useMemo } from 'react';
import { Html } from '@react-three/drei';
import * as THREE from 'three';
import CompanyNode from './CompanyNode';
import RelationLines from './RelationLines';
import { useLiveUniverse } from './useGraph';
import { useGalaxy } from '../store/useGalaxy';
import { SECTORS } from '../data/universe';
import { liveNodeRadius, liveTypeMeta } from '../data/live-universe';
import { tickerCode, tossSecuritiesLogoUrl } from '../data/company-logo';

const SAMSUNG_ELECTRONICS_CODE = '005930';

function OrbitRing({ radius, color, opacity }) {
  return (
    <mesh rotation={[Math.PI / 2, 0, 0]}>
      <ringGeometry args={[radius - 0.035, radius + 0.035, 128]} />
      <meshBasicMaterial color={color} transparent opacity={opacity} side={THREE.DoubleSide} depthWrite={false} toneMapped={false} />
    </mesh>
  );
}

export default function LiveSystemView() {
  const focus = useGalaxy((state) => state.focus);
  const hovered = useGalaxy((state) => state.hovered);
  const selectedEdge = useGalaxy((state) => state.selectedEdge);
  const showLabels = useGalaxy((state) => state.showLabels);
  const setHovered = useGalaxy((state) => state.setHovered);
  const selectEdge = useGalaxy((state) => state.selectEdge);
  const warpTo = useGalaxy((state) => state.warpTo);
  const universe = useLiveUniverse();
  const company = universe?.companyById[focus];

  const layout = useMemo(() => {
    if (!universe || !focus) return [];
    const neighbors = universe.edges
      .filter((edge) => edge.a === focus || edge.b === focus)
      .slice(0, 14);
    return neighbors.map((edge, index) => {
      const id = edge.a === focus ? edge.b : edge.a;
      const strength = Math.abs(edge.corr);
      const radius = 34 - Math.min(0.9, strength) * 18;
      const angle = (index / Math.max(1, neighbors.length)) * Math.PI * 2 + 0.4;
      const y = (index % 2 === 0 ? 1 : -1) * (2 + (1 - strength) * 7);
      return {
        ...edge,
        id,
        radius,
        position: [Math.cos(angle) * radius, y, Math.sin(angle) * radius],
      };
    });
  }, [focus, universe]);

  if (!universe || !company) return null;

  // Keep this presentation treatment deliberately scoped to the Samsung Electronics
  // system requested for the planning demo. Other company views retain their current
  // data-provenance labels until their presentation treatment is approved.
  const isSamsungPresentation = tickerCode(company.ticker) === SAMSUNG_ELECTRONICS_CODE;

  const lineEdges = layout.map((neighbor) => {
    const hot = hovered === neighbor.id || selectedEdge === neighbor.key;
    const anyHot = Boolean(hovered || selectedEdge);
    return {
      key: neighbor.key,
      from: [0, 0, 0],
      to: neighbor.position,
      color: neighbor.serendipity ? '#ff9de0' : liveTypeMeta(neighbor.type).color,
      alpha: anyHot ? (hot ? 1 : 0.1) : neighbor.basis === 'mock' ? 0.22 + neighbor.corr * 0.3 : 0.35 + neighbor.corr * 0.48,
      seed: (neighbor.confidence ?? 55) / 100,
    };
  });
  const relatedNews = universe.newsByCompany[focus] ?? [];

  return (
    <group>
      <CompanyNode
        company={company}
        position={[0, 0, 0]}
        radius={3.2}
        label={`${company.name} · ${company.ticker}`}
        logoUrl={tossSecuritiesLogoUrl(company)}
        active
        focused
        showLabel={showLabels}
        onHover={setHovered}
      />
      <RelationLines edges={lineEdges} bow={0.1} />

      {layout.map((neighbor) => {
        const related = universe.companyById[neighbor.id];
        const hot = hovered === neighbor.id || selectedEdge === neighbor.key;
        const anyHot = Boolean(hovered || selectedEdge);
        if (!related) return null;
        return (
          <group key={neighbor.key}>
            <OrbitRing radius={neighbor.radius} color={SECTORS[related.sector].color} opacity={hot ? 0.28 : 0.07} />
            <CompanyNode
              company={related}
              position={neighbor.position}
              radius={Math.max(1.05, liveNodeRadius(related) * 0.65)}
              label={`${related.name}  +${neighbor.corr.toFixed(2)}`}
              logoUrl={tossSecuritiesLogoUrl(related)}
              dim={anyHot ? (hot ? 1 : 0.25) : 1}
              active={hovered === related.id}
              showLabel={showLabels}
              onHover={setHovered}
              onClick={() => selectEdge(neighbor.key)}
            />
            {hovered === related.id && (
              <Html position={[neighbor.position[0], neighbor.position[1] - 3.6, neighbor.position[2]]} center>
                <button className="warp-chip" onClick={(event) => { event.stopPropagation(); warpTo(related.id); }}>
                  이 기업으로 워프 →
                </button>
              </Html>
            )}
          </group>
        );
      })}

      {!isSamsungPresentation && (
        <Html position={[0, -31, 4]} center>
          <div className="live-ribbon">
            <span>{company.isMock ? 'INDUSTRY CONTEXT' : 'LIVE NEWS SIGNAL'}</span>
            <strong>{company.isMock ? `${company.name} 산업 관계 확장` : relatedNews[0]?.title ?? universe.activeNews?.title ?? '연결 뉴스 분석 중'}</strong>
            <small>{company.isMock ? '프로토타입 산업 관계 카탈로그 · 합성 강도' : `${relatedNews[0]?.source ?? universe.activeNews?.source ?? 'NAVER API HUB'} · 실제 기사 근거`}</small>
            <i>{company.isMock ? '실시간 기사 추출 관계와 구분되는 시연용 노드입니다.' : '주가 시계열 API 연결 전에는 뉴스 충격과 관계 강도만 표시합니다.'}</i>
          </div>
        </Html>
      )}
    </group>
  );
}
