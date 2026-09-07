import { useMemo } from 'react';
import * as THREE from 'three';
import CompanyNode from './CompanyNode';
import RelationLines from './RelationLines';
import LabelSprite from './LabelSprite';
import { useGraph, useLiveUniverse } from './useGraph';
import { useGalaxy } from '../store/useGalaxy';
import { SECTORS, SECTOR_LIST } from '../data/universe';
import { liveNodeRadius, liveTypeMeta } from '../data/live-universe';
import { tossSecuritiesLogoUrl } from '../data/company-logo';

function seedFor(value) {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) hash = (hash * 31 + value.charCodeAt(index)) | 0;
  return Math.abs(hash % 1000) / 1000;
}

function EdgeHitTarget({ edge, onSelect, onHover }) {
  const geometry = useMemo(() => {
    const start = new THREE.Vector3().fromArray(edge.from);
    const end = new THREE.Vector3().fromArray(edge.to);
    const control = start.clone().add(end).multiplyScalar(0.5);
    const outward = control.clone();
    if (outward.lengthSq() < 1e-6) outward.set(0, 1, 0);
    outward.normalize().multiplyScalar(start.distanceTo(end) * 0.17);
    outward.y += start.distanceTo(end) * 0.05;
    control.add(outward);
    const curve = new THREE.QuadraticBezierCurve3(start, control, end);
    return new THREE.TubeGeometry(curve, 18, 0.72, 5, false);
  }, [edge.from, edge.to]);

  return (
    <mesh
      geometry={geometry}
      onClick={(event) => { event.stopPropagation(); onSelect(edge.key); }}
      onPointerOver={(event) => { event.stopPropagation(); onHover(edge.key); document.body.style.cursor = 'pointer'; }}
      onPointerOut={(event) => { event.stopPropagation(); onHover(null); document.body.style.cursor = ''; }}
    >
      <meshBasicMaterial transparent opacity={0} depthWrite={false} />
    </mesh>
  );
}

export default function LiveGalaxyView() {
  const hovered = useGalaxy((state) => state.hovered);
  const selectedEdge = useGalaxy((state) => state.selectedEdge);
  const showLabels = useGalaxy((state) => state.showLabels);
  const setHovered = useGalaxy((state) => state.setHovered);
  const selectEdge = useGalaxy((state) => state.selectEdge);
  const activeSector = useGalaxy((state) => state.activeSector);
  const warpTo = useGalaxy((state) => state.warpTo);
  const universe = useLiveUniverse();
  const edges = useGraph();

  const { lineEdges, connected } = useMemo(() => {
    const connectedNodes = new Set();
    const lines = edges.map((edge) => {
      const touchesHover = hovered && (edge.a === hovered || edge.b === hovered);
      const isSelected = selectedEdge === edge.key;
      if (touchesHover || isSelected) {
        connectedNodes.add(edge.a);
        connectedNodes.add(edge.b);
      }
      const base = edge.basis === 'mock'
        ? 0.1 + Math.min(1, Math.abs(edge.corr)) * 0.28
        : 0.15 + Math.min(1, Math.abs(edge.corr)) * 0.44;
      let alpha = base;
      if (selectedEdge) alpha = isSelected ? 1 : base * 0.1;
      else if (hovered) alpha = touchesHover ? 1 : base * 0.1;
      return {
        key: edge.key,
        from: universe.positions[edge.a],
        to: universe.positions[edge.b],
        color: edge.serendipity ? '#ff9de0' : liveTypeMeta(edge.type).color,
        alpha,
        seed: seedFor(edge.key),
      };
    });
    return { lineEdges: lines, connected: connectedNodes };
  }, [edges, hovered, selectedEdge, universe]);

  if (!universe) return null;
  const highlighted = Boolean(hovered || selectedEdge);
  const visibleCompanies = activeSector
    ? universe.companies.filter((company) => company.sector === activeSector)
    : universe.companies;
  const activeSectorIds = new Set(visibleCompanies.map((company) => company.sector));

  return (
    <group>
      <RelationLines edges={lineEdges} bow={0.17} />
      {lineEdges.map((edge) => (
        <EdgeHitTarget
          key={`hit-${edge.key}`}
          edge={edge}
          onSelect={selectEdge}
          onHover={(key) => {
            const hoveredEdge = key ? edges.find((item) => item.key === key) : null;
            setHovered(hoveredEdge?.a ?? null);
          }}
        />
      ))}

      {showLabels && SECTOR_LIST.filter((sector) => activeSectorIds.has(sector.id)).map((sector) => {
        const center = universe.sectorCenters[sector.id];
        return (
          <LabelSprite
            key={sector.id}
            text={sector.name}
            color={SECTORS[sector.id].color}
            size={4.4}
            weight={700}
            opacity={0.26}
            position={[center[0], center[1] + 21, center[2]]}
          />
        );
      })}

      {visibleCompanies.map((company) => {
        const hot = hovered === company.id || connected.has(company.id);
        const latestNews = universe.newsByCompany[company.id]?.[0];
        return (
          <CompanyNode
            key={company.id}
            company={company}
            position={universe.positions[company.id]}
            radius={liveNodeRadius(company)}
            label={company.name}
            logoUrl={tossSecuritiesLogoUrl(company)}
            brightness={hot ? 0.88 : 0.68}
            dim={highlighted ? (hot ? 1 : 0.2) : 1}
            active={hovered === company.id}
            news={latestNews ? { tone: latestNews.sentiment === '긍정' ? 1 : -1, ageDays: 1 } : null}
            showLabel={showLabels}
            onHover={setHovered}
            onClick={warpTo}
          />
        );
      })}
    </group>
  );
}
