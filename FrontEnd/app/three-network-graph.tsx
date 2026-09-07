"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { NodeRisk } from "../lib/risk-engine";
import type { RiskEdge, RiskNode, Scenario } from "../lib/risk-data";

type GraphMode = "news" | "aggregate" | "path" | "ownership";
type AnimatedPulse = { mesh: THREE.Mesh; curve: THREE.CatmullRomCurve3; speed: number; offset: number };
type AnimatedNode = {
  group: THREE.Group;
  ring: THREE.Mesh;
  shell: THREE.Mesh;
  badge: THREE.Mesh | null;
  radius: number;
  baseY: number;
  phase: number;
  selected: boolean;
};
type Props = {
  scenario: Scenario; risk: Map<string, NodeRisk>; selectedId: string; path: string[]; progress: number; mode: GraphMode;
  onSelect: (id: string) => void;
  logoPath: (node?: Pick<RiskNode, "code" | "kind">, nodeId?: string) => string | null;
  fallback: ReactNode;
};

const edgePalette = { direct: 0x40e58a, triple: 0x9d7cff, modeled: 0xffae57, path: 0xff655d, default: 0x4c77a8 };

export default function ThreeNetworkGraph({ scenario, risk, selectedId, path, progress, mode, onSelect, logoPath, fallback }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const defaultCameraDistanceRef = useRef(9.2);
  const [webglFailed, setWebglFailed] = useState(false);
  const focusedNode = scenario.nodes.find((node) => node.id === selectedId) ?? scenario.nodes[0];
  const focusedNeighbors = getFocusedNeighbors(scenario, focusedNode.id);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" }); }
    catch { window.queueMicrotask(() => setWebglFailed(true)); return; }

    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.08;
    renderer.domElement.className = "three-network-canvas";
    renderer.domElement.setAttribute("aria-label", `${scenario.title} Three.js 리스크 전이 네트워크`);
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x07111f, 0.075);
    const camera = new THREE.PerspectiveCamera(44, 1, 0.1, 80);
    camera.position.set(0, 0.3, 9.2);
    cameraRef.current = camera;
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true; controls.dampingFactor = 0.065; controls.enablePan = false; controls.zoomToCursor = false;
    controls.mouseButtons.LEFT = THREE.MOUSE.PAN; controls.mouseButtons.RIGHT = THREE.MOUSE.ROTATE;
    controls.touches.ONE = THREE.TOUCH.PAN; controls.touches.TWO = THREE.TOUCH.DOLLY_ROTATE;
    controls.minDistance = 4.2; controls.maxDistance = 18;
    controls.maxPolarAngle = Math.PI * 0.73; controls.minPolarAngle = Math.PI * 0.27;
    controlsRef.current = controls;

    scene.add(new THREE.HemisphereLight(0x9fd9ff, 0x101727, 1.7));
    const keyLight = new THREE.PointLight(0x5b9dff, 22, 20); keyLight.position.set(-3, 4, 6); scene.add(keyLight);
    const rimLight = new THREE.PointLight(0x7d5cff, 15, 18); rimLight.position.set(4, -3, 4); scene.add(rimLight);
    const starLayers = [
      createStarField(720, 22, 0.038, 0xaedcff, 0.78, 17),
      createStarField(220, 16, 0.055, 0xffd4a0, 0.42, 71),
    ];
    starLayers.forEach((layer) => scene.add(layer));
    const grid = new THREE.GridHelper(18, 34, 0x19365a, 0x10263f);
    grid.rotation.x = Math.PI / 2; grid.position.z = -1.65;
    (grid.material as THREE.Material).transparent = true; (grid.material as THREE.Material).opacity = 0.13; scene.add(grid);

    const byId = new Map(scenario.nodes.map((node) => [node.id, node]));
    const { positions: nodePositions, cameraDistance, innerRadius, outerRadius, adjacentCount, remainingCount } = createFocusedLayout(scenario, risk, selectedId);
    defaultCameraDistanceRef.current = cameraDistance;
    camera.position.set(0, 0.3, cameraDistance);
    if (adjacentCount) scene.add(createOrbitLine(innerRadius, innerRadius * 0.62, -0.04, 0x63d7ff, 0.13));
    if (remainingCount) scene.add(createOrbitLine(outerRadius, outerRadius * 0.58, -0.34, 0xff9de0, 0.07));
    const clickable: THREE.Object3D[] = [];
    const pulses: AnimatedPulse[] = [];
    const animatedNodes: AnimatedNode[] = [];
    const pathEdges = new Set(path.slice(1).map((id, index) => `${path[index]}:${id}`));
    const reachedDepth = Math.max(0, Math.ceil(progress * (scenario.maxDepth + 1)));
    scenario.edges.forEach((edge, index) => {
      const a = nodePositions.get(edge.source); const b = nodePositions.get(edge.target);
      if (!byId.has(edge.source) || !byId.has(edge.target) || !a || !b) return;
      const style = edgeStyle(edge, mode, pathEdges);
      const reached = (risk.get(edge.target)?.depth ?? 99) <= reachedDepth;
      const midpoint = a.clone().lerp(b, 0.5); midpoint.z += 0.5 + a.distanceTo(b) * 0.055;
      const curve = new THREE.CatmullRomCurve3([a, midpoint, b]);
      scene.add(new THREE.Mesh(
        new THREE.TubeGeometry(curve, 28, 0.012 + edge.weight * 0.018, 7, false),
        new THREE.MeshBasicMaterial({ color: style.color, transparent: true, opacity: style.relevant ? (reached ? 0.62 : 0.14) : 0.045, blending: THREE.AdditiveBlending, depthWrite: false }),
      ));
      if (!style.relevant || !reached) return;
      const particle = new THREE.Mesh(new THREE.SphereGeometry(0.045 + edge.weight * 0.025, 12, 12), new THREE.MeshBasicMaterial({ color: style.color, blending: THREE.AdditiveBlending }));
      scene.add(particle); pulses.push({ mesh: particle, curve, speed: 0.065 + edge.weight * 0.06, offset: (index * 0.173) % 1 });
      const arrowDirection = curve.getTangentAt(0.92).normalize();
      const arrow = new THREE.Mesh(new THREE.ConeGeometry(0.07 + edge.weight * 0.03, 0.24, 12), new THREE.MeshBasicMaterial({ color: style.color, transparent: true, opacity: 0.88 }));
      arrow.position.copy(curve.getPointAt(0.92)); arrow.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), arrowDirection); scene.add(arrow);
    });

    const glowTexture = createGlowTexture();
    const textureLoader = new THREE.TextureLoader();
    scenario.nodes.forEach((node, nodeIndex) => {
      const position = nodePositions.get(node.id)!; const score = risk.get(node.id)?.score ?? 0;
      const selected = node.id === selectedId; const origin = node.id === scenario.shockSource;
      const radius = selected ? 0.4 : origin ? 0.34 : node.kind === "material" || node.kind === "shareholder" ? 0.29 : 0.25;
      const color = nodeColor(node, origin, score); const group = new THREE.Group(); group.position.copy(position); scene.add(group);
      const geometry = new THREE.IcosahedronGeometry(radius, node.kind === "event" ? 2 : 3);
      const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color, roughness: 0.24, metalness: 0.16, emissive: color, emissiveIntensity: selected || origin ? 0.88 : 0.42 }));
      mesh.userData.nodeId = node.id; group.add(mesh); clickable.push(mesh);
      const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTexture, color: selected ? 0x68a8ff : color, transparent: true, opacity: selected ? 0.82 : 0.28 + Math.min(score, 90) / 260, blending: THREE.AdditiveBlending, depthWrite: false }));
      glow.scale.setScalar(radius * (selected ? 4.8 : 3.4)); group.add(glow);
      const shell = new THREE.Mesh(
        new THREE.IcosahedronGeometry(radius * 1.34, 1),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: selected ? 0.42 : 0.16, wireframe: true, blending: THREE.AdditiveBlending, depthWrite: false }),
      );
      group.add(shell);
      const ring = new THREE.Mesh(
        new THREE.TorusGeometry(radius * (selected ? 1.78 : 1.62), radius * 0.045, 8, 72),
        new THREE.MeshBasicMaterial({ color: selected ? 0xf1fbff : color, transparent: true, opacity: selected ? 0.92 : 0.28, blending: THREE.AdditiveBlending, depthWrite: false }),
      );
      ring.rotation.set(Math.PI / 2.55 + (nodeIndex % 3) * 0.09, 0, nodeIndex * 0.73); group.add(ring);
      if (selected || origin) {
        const shock = new THREE.Mesh(
          new THREE.SphereGeometry(radius * (selected ? 2.45 : 2.1), 24, 16),
          new THREE.MeshBasicMaterial({ color: selected ? 0x63d7ff : color, transparent: true, opacity: selected ? 0.14 : 0.09, wireframe: true, blending: THREE.AdditiveBlending, depthWrite: false }),
        );
        group.add(shock);
      }
      const animatedNode: AnimatedNode = {
        group,
        ring,
        shell,
        badge: null,
        radius,
        baseY: position.y,
        phase: nodeIndex * 0.83,
        selected,
      };
      animatedNodes.push(animatedNode);
      const logo = logoPath(node, node.id);
      if (logo) textureLoader.load(logo, (texture) => {
        texture.colorSpace = THREE.SRGBColorSpace;
        const badge = new THREE.Mesh(
          new THREE.CircleGeometry(radius * 0.76, 40),
          new THREE.MeshBasicMaterial({ map: texture, transparent: true, toneMapped: false, depthWrite: false }),
        );
        badge.renderOrder = 4;
        animatedNode.badge = badge;
        group.add(badge);
      }, undefined, () => undefined);
      const label = new THREE.Sprite(new THREE.SpriteMaterial({ map: createLabelTexture(node.label, score, origin), transparent: true, depthWrite: false }));
      label.position.set(0, -radius - 0.29, 0); label.scale.set(selected ? 1.82 : 1.55, selected ? 0.49 : 0.42, 1); group.add(label);
    });

    const raycaster = new THREE.Raycaster(); const pointer = new THREE.Vector2();
    const swipeNeighbors = getFocusedNeighbors(scenario, byId.has(selectedId) ? selectedId : scenario.nodes[0]?.id ?? selectedId);
    const gesture = { pointerId: -1, startX: 0, startY: 0, startAt: 0, moved: 0, ignoreClick: false };
    const pick = (event: PointerEvent, select: boolean) => {
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1; pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera); const hit = raycaster.intersectObjects(clickable, false)[0];
      renderer.domElement.style.cursor = hit ? "pointer" : "grab";
      if (select && hit?.object.userData.nodeId) onSelect(hit.object.userData.nodeId as string);
    };
    const handlePointerDown = (event: PointerEvent) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      gesture.pointerId = event.pointerId; gesture.startX = event.clientX; gesture.startY = event.clientY; gesture.startAt = performance.now(); gesture.moved = 0; gesture.ignoreClick = false;
      renderer.domElement.setPointerCapture?.(event.pointerId);
      renderer.domElement.style.cursor = "grabbing";
    };
    const handlePointerMove = (event: PointerEvent) => {
      if (event.pointerId === gesture.pointerId) gesture.moved = Math.max(gesture.moved, Math.hypot(event.clientX - gesture.startX, event.clientY - gesture.startY));
      pick(event, false);
    };
    const handlePointerUp = (event: PointerEvent) => {
      if (event.pointerId !== gesture.pointerId) return;
      const deltaX = event.clientX - gesture.startX; const deltaY = event.clientY - gesture.startY;
      const isHorizontalSwipe = Math.abs(deltaX) >= 48 && Math.abs(deltaX) > Math.abs(deltaY) * 1.25 && performance.now() - gesture.startAt < 1100;
      gesture.ignoreClick = gesture.moved > 7;
      if (renderer.domElement.hasPointerCapture?.(event.pointerId)) renderer.domElement.releasePointerCapture(event.pointerId);
      gesture.pointerId = -1; renderer.domElement.style.cursor = "grab";
      if (!isHorizontalSwipe || !swipeNeighbors.length) return;
      const requestedSide = deltaX < 0 ? 1 : -1;
      const projectedNeighbors = swipeNeighbors.map((node) => ({ node, screenX: nodePositions.get(node.id)?.clone().project(camera).x ?? 0 }));
      const sideCandidates = projectedNeighbors.filter((candidate) => candidate.screenX * requestedSide > 0.04)
        .sort((left, right) => Math.abs(left.screenX) - Math.abs(right.screenX));
      const nextFocus = sideCandidates[0] ?? projectedNeighbors.sort((left, right) => Math.abs(left.screenX) - Math.abs(right.screenX))[0];
      if (nextFocus) onSelect(nextFocus.node.id);
    };
    const handlePointerCancel = (event: PointerEvent) => {
      if (event.pointerId !== gesture.pointerId) return;
      gesture.pointerId = -1; gesture.ignoreClick = true; renderer.domElement.style.cursor = "grab";
    };
    const handleClick = (event: PointerEvent) => {
      if (gesture.ignoreClick) { gesture.ignoreClick = false; return; }
      pick(event, true);
    };
    renderer.domElement.addEventListener("pointerdown", handlePointerDown);
    renderer.domElement.addEventListener("pointermove", handlePointerMove);
    renderer.domElement.addEventListener("pointercancel", handlePointerCancel);
    renderer.domElement.addEventListener("click", handleClick);
    window.addEventListener("pointerup", handlePointerUp);
    const resize = () => { const rect = host.getBoundingClientRect(); if (!rect.width || !rect.height) return; renderer.setSize(rect.width, rect.height, false); camera.aspect = rect.width / rect.height; camera.updateProjectionMatrix(); };
    const observer = new ResizeObserver(resize); observer.observe(host); resize();
    const clock = new THREE.Clock();
    const badgeDirection = new THREE.Vector3();
    let frame = 0;
    const animate = () => {
      frame = window.requestAnimationFrame(animate);
      const elapsed = clock.getElapsedTime();
      pulses.forEach((pulse) => pulse.mesh.position.copy(pulse.curve.getPointAt((elapsed * pulse.speed + pulse.offset) % 1)));
      animatedNodes.forEach((item) => {
        item.group.position.y = item.baseY + Math.sin(elapsed * 0.62 + item.phase) * (item.selected ? 0.035 : 0.055);
        item.ring.rotation.z += item.selected ? 0.006 : 0.0025;
        item.shell.rotation.y += item.selected ? 0.004 : 0.0018;
        item.shell.rotation.x += 0.0009;
        if (item.badge) {
          // Keep the logo on the camera-to-planet ray. A fixed local Z offset causes
          // off-centre parallax as nodes move toward the edge of the viewport.
          badgeDirection.copy(camera.position).sub(item.group.position).normalize();
          item.badge.position.copy(badgeDirection).multiplyScalar(item.radius + 0.008);
          item.badge.quaternion.copy(camera.quaternion);
        }
      });
      starLayers[0].rotation.z = elapsed * 0.0018;
      starLayers[1].rotation.y = -elapsed * 0.0024;
      controls.update(); renderer.render(scene, camera);
    };
    animate();
    return () => {
      window.cancelAnimationFrame(frame); observer.disconnect(); controls.dispose();
      renderer.domElement.removeEventListener("pointerdown", handlePointerDown); renderer.domElement.removeEventListener("pointermove", handlePointerMove); renderer.domElement.removeEventListener("pointercancel", handlePointerCancel); renderer.domElement.removeEventListener("click", handleClick);
      window.removeEventListener("pointerup", handlePointerUp);
      scene.traverse((object) => {
        if (!(object instanceof THREE.Mesh || object instanceof THREE.Sprite || object instanceof THREE.Line || object instanceof THREE.Points)) return;
        object.geometry?.dispose(); const materials = Array.isArray(object.material) ? object.material : [object.material];
        materials.forEach((item) => { if ("map" in item && item.map instanceof THREE.Texture && item.map !== glowTexture) item.map.dispose(); item.dispose(); });
      });
      glowTexture.dispose(); renderer.dispose(); renderer.domElement.remove(); cameraRef.current = null; controlsRef.current = null;
    };
  }, [logoPath, mode, onSelect, path, progress, risk, scenario, selectedId]);

  if (webglFailed) return fallback;
  const resetCamera = () => { const camera = cameraRef.current; const controls = controlsRef.current; if (!camera || !controls) return; camera.position.set(0, 0.3, defaultCameraDistanceRef.current); controls.target.set(0, 0, 0); controls.update(); };
  return <div className="three-network-shell" ref={hostRef}>
    <div className="three-network-status"><i /><span>3D RISK MAP</span><b>{scenario.nodes.length} NODES</b></div>
    <div className="three-focus-card" aria-live="polite">
      <button type="button" aria-label="이전 연결 기업으로 이동" disabled={!focusedNeighbors.length} onClick={() => focusedNeighbors.length && onSelect(focusedNeighbors.at(-1)!.id)}>‹</button>
      <div><span>MAIN FOCUS</span><strong>{focusedNode.label}</strong><small>직접 연결 {focusedNeighbors.length}개</small></div>
      <button type="button" aria-label="다음 연결 기업으로 이동" disabled={!focusedNeighbors.length} onClick={() => focusedNeighbors.length && onSelect(focusedNeighbors[0].id)}>›</button>
    </div>
    <button className="three-camera-reset" onClick={resetCamera}>뷰 초기화</button>
    <div className="three-control-hint">가로 드래그 연결 기업 이동 · 휠 전체망 확대/축소 · 노드 클릭</div>
  </div>;
}

function getFocusedNeighbors(scenario: Scenario, focusedId: string) {
  const byId = new Map(scenario.nodes.map((node) => [node.id, node]));
  const ids = scenario.edges.flatMap((edge) => edge.source === focusedId ? [edge.target] : edge.target === focusedId ? [edge.source] : []);
  const unique = [...new Set(ids)].map((id) => byId.get(id)).filter((node): node is RiskNode => Boolean(node));
  const companies = unique.filter((node) => node.kind === "company");
  return (companies.length ? companies : unique).sort((a, b) => a.label.localeCompare(b.label, "ko"));
}

function createFocusedLayout(scenario: Scenario, risk: Map<string, NodeRisk>, selectedId: string) {
  const byId = new Map(scenario.nodes.map((node) => [node.id, node]));
  const focusId = byId.has(selectedId) ? selectedId : scenario.nodes[0]?.id;
  const adjacentIds = [...new Set(scenario.edges.flatMap((edge) => edge.source === focusId ? [edge.target] : edge.target === focusId ? [edge.source] : []))];
  adjacentIds.sort((left, right) => {
    const leftNode = byId.get(left); const rightNode = byId.get(right);
    const companyOrder = Number(rightNode?.kind === "company") - Number(leftNode?.kind === "company");
    return companyOrder || (risk.get(right)?.score ?? 0) - (risk.get(left)?.score ?? 0) || (leftNode?.label ?? "").localeCompare(rightNode?.label ?? "", "ko");
  });
  const adjacent = new Set(adjacentIds);
  const remainingIds = scenario.nodes.map((node) => node.id).filter((id) => id !== focusId && !adjacent.has(id));
  const innerRadius = Math.min(3.35, 2.3 + adjacentIds.length * 0.12);
  const outerRadius = Math.min(4.6, innerRadius + 1.15);
  const positions = new Map<string, THREE.Vector3>();
  if (focusId) positions.set(focusId, new THREE.Vector3(0, 0, 0.72));
  adjacentIds.forEach((id, index) => {
    const angle = index * (Math.PI * 2 / Math.max(adjacentIds.length, 1));
    const z = Math.sin(index * 1.4) * 0.28;
    positions.set(id, new THREE.Vector3(Math.cos(angle) * innerRadius, Math.sin(angle) * innerRadius * 0.62, z));
  });
  remainingIds.forEach((id, index) => {
    const angle = -Math.PI / 2 + (index + 0.5) * (Math.PI * 2 / Math.max(remainingIds.length, 1));
    const z = -0.35 + Math.cos(index * 1.7) * 0.25;
    positions.set(id, new THREE.Vector3(Math.cos(angle) * outerRadius, Math.sin(angle) * outerRadius * 0.58, z));
  });
  return {
    positions,
    innerRadius,
    outerRadius,
    adjacentCount: adjacentIds.length,
    remainingCount: remainingIds.length,
    cameraDistance: Math.min(12.6, 8.8 + Math.max(adjacentIds.length - 4, 0) * 0.35),
  };
}

function edgeStyle(edge: RiskEdge, mode: GraphMode, pathEdges: Set<string>) {
  const isPath = pathEdges.has(`${edge.source}:${edge.target}`); const isDirect = edge.id.startsWith("article-direct-");
  const isTriple = edge.id.startsWith("article-triple-"); const isModeled = edge.id.startsWith("article-model-"); const isArticle = edge.id.startsWith("article-");
  const relevant = mode === "ownership" ? edge.relation === "OWNS_STAKE" : mode === "path" ? isPath : mode === "news" ? isArticle : true;
  const color = isPath && mode === "path" ? edgePalette.path : isDirect ? edgePalette.direct : isTriple ? edgePalette.triple : isModeled ? edgePalette.modeled : edgePalette.default;
  return { relevant, color };
}

function nodeColor(node: RiskNode, origin: boolean, score: number) {
  if (origin) return 0x3d8bff; if (score >= 70) return 0xff635e; if (node.kind === "event") return 0x5a8fff;
  if (node.kind === "material") return 0x9b73ff; if (node.kind === "shareholder") return 0xd4a66b; return 0x5f7898;
}

function createGlowTexture() {
  const canvas = document.createElement("canvas"); canvas.width = 128; canvas.height = 128; const context = canvas.getContext("2d")!;
  const gradient = context.createRadialGradient(64, 64, 2, 64, 64, 62); gradient.addColorStop(0, "rgba(255,255,255,1)"); gradient.addColorStop(0.22, "rgba(255,255,255,.55)"); gradient.addColorStop(1, "rgba(255,255,255,0)");
  context.fillStyle = gradient; context.fillRect(0, 0, 128, 128); return new THREE.CanvasTexture(canvas);
}

function createStarField(count: number, radius: number, size: number, color: number, opacity: number, initialSeed: number) {
  let seed = initialSeed >>> 0;
  const random = () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 4294967296;
  };
  const positions = new Float32Array(count * 3);
  for (let index = 0; index < count; index += 1) {
    const theta = random() * Math.PI * 2;
    const phi = Math.acos(2 * random() - 1);
    const distance = radius * (0.38 + random() * 0.62);
    positions[index * 3] = Math.sin(phi) * Math.cos(theta) * distance;
    positions[index * 3 + 1] = Math.sin(phi) * Math.sin(theta) * distance;
    positions[index * 3 + 2] = Math.cos(phi) * distance - 3;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  return new THREE.Points(geometry, new THREE.PointsMaterial({ color, size, transparent: true, opacity, sizeAttenuation: true, depthWrite: false, blending: THREE.AdditiveBlending }));
}

function createOrbitLine(radiusX: number, radiusY: number, z: number, color: number, opacity: number) {
  const points = Array.from({ length: 129 }, (_, index) => {
    const angle = index / 128 * Math.PI * 2;
    return new THREE.Vector3(Math.cos(angle) * radiusX, Math.sin(angle) * radiusY, z);
  });
  return new THREE.LineLoop(
    new THREE.BufferGeometry().setFromPoints(points),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false }),
  );
}

function createLabelTexture(label: string, score: number, origin: boolean) {
  const canvas = document.createElement("canvas"); canvas.width = 512; canvas.height = 132; const context = canvas.getContext("2d")!;
  context.fillStyle = "rgba(6,15,28,.86)"; context.strokeStyle = origin ? "rgba(79,150,255,.9)" : "rgba(128,169,216,.38)"; context.lineWidth = 3;
  context.beginPath(); context.roundRect(6, 8, 500, 116, 26); context.fill(); context.stroke();
  context.fillStyle = "#f5f9ff"; context.font = "700 30px 'Malgun Gothic', sans-serif"; context.textAlign = "center"; context.fillText(label.length > 17 ? `${label.slice(0, 17)}…` : label, 256, 60);
  context.fillStyle = origin ? "#67a9ff" : score >= 60 ? "#ff827c" : "#8da9ca"; context.font = "700 22px Inter, sans-serif"; context.fillText(origin ? "LIVE SHOCK" : `RISK ${score}`, 256, 96);
  const texture = new THREE.CanvasTexture(canvas); texture.colorSpace = THREE.SRGBColorSpace; return texture;
}
