import { useEffect, useMemo, useRef } from "react";
import { useFrame, type ThreeEvent } from "@react-three/fiber";
import * as THREE from "three";
import type { SceneModel, SceneNode } from "@/lib/graph";
import { initials } from "@/lib/format";
import { logoUrl } from "@/lib/logos";
import { hashString } from "@/lib/rng";
import { CHIP_H, CHIP_PILL, CHIP_STEM, chipFragment, chipHit, chipVertex } from "./chipShader";
import type { Emphasis } from "./emphasis";
import { ATLAS_CELL_UV, cellUv, ensureLogoCell, logoAtlasTexture } from "./logoAtlas";
import { discFragment, discVertex } from "./logoDiscShader";
import { liveFor, morphState } from "./morph";
import { ensureNameCell, nameAtlasTexture, type NameCell } from "./nameAtlas";
import { animFor } from "./nodeAnim";
import { dotTexture, ringTexture } from "./textures";
import { planetGeometry, planetPaletteFor, toonGradient, type PlanetPalette } from "./toon";

interface Props {
  model: SceneModel;
  emphasis: Emphasis;
  hoveredId: string | null;
  onHover: (id: string | null) => void;
  onClick: (id: string) => void;
  /** 이름표 칩에 기업명을 펼칠지 (끄면 로고 원만 남는다). 강조로 고정된 노드는 항상 펼친다 */
  showLabels?: boolean;
  /** Shift+클릭 — 경로 기준 고정. 없으면 Shift 여부와 무관하게 onClick */
  onPin?: (id: string) => void;
}

type Variant = "land" | "ring" | "moon";
interface Planet {
  node: SceneNode;
  radius: number;
  palette: PlanetPalette;
  variant: Variant;
  seed: number;
  geo: THREE.BufferGeometry;
  moonGeo: THREE.BufferGeometry | null;
}

const tmpObj = new THREE.Object3D();
const TMP = new THREE.Vector3();
const TO_CAM = new THREE.Vector3();
const UPV = new THREE.Vector3();
const RGT = new THREE.Vector3();
const NDC = new THREE.Vector3();
/** 화면 버킷 → 그 버킷을 차지한 노드 인덱스. 프레임마다 비우고 채우는 스크래치라 모듈에 둔다 */
const BUCKETS = new Map<number, number>();
/**
 * 티어별 칩이 완전히 사라지는 카메라 거리 — 0.75 배 지점부터 페이드. 허브는 은하 기본 자세에서도 보인다.
 * 은하: 기본 자세(poseFor, 188개 목업 기준 카메라 거리 ≈ 265)에서 카메라를 향한 성단의 첫 껍질이 ≈ 151 까지 다가오므로
 *       tier 1 상한을 140 으로 두어 '기본 자세에서는 허브만' 을 지킨다 (페이드 105~140).
 * 기업 중심: 기본 자세(거리 ≈ 118)에서 depth 1 궤도가 96~142 에 걸치므로 200 을 유지해야 첫 궤도 칩이 다 보인다.
 */
const TIER_FAR: Record<SceneModel["kind"], number[]> = { galaxy: [520, 140, 110], system: [520, 200, 110] };
/** 화면 버킷(px) — 같은 버킷에 겹치는 칩은 우선순위 높은 하나만 남긴다 */
const BUCKET_W = 96;
const BUCKET_H = 28;
/** 칩 목표 화면 높이(px)와 월드 높이 클램프 — 코앞에서도 너무 작아지지 않고 멀리서도 행성을 덮지 않게 */
const CHIP_PX = 20;
const CHIP_MIN = 1.3;
const CHIP_MAX = 4.0;
/** 항성 점광 레이어 — 랩과 동일 */
const LIT = 1;
/** 피킹에서 무시하는 배지·칩 알파 — 이 아래는 보이지 않는데 포인터를 가로채면 안 된다 */
const PICK_ALPHA = 0.05;

/**
 * InstancedMesh 레이캐스트를 감싸 accept 를 통과한 히트만 남긴다.
 * R3F 는 핸들러가 stopPropagation 을 부르지 않아도 히트 인스턴스를 hovered 맵에 등록하고(이후 onPointerOut 발생),
 * 한 번 stop 된 인스턴스 위에서는 핸들러 전에 전파를 다시 막는다. 그래서 투명한 스템·모서리를 핸들러에서 걸러내는 것으로는
 * 부족하고, 애초에 레이캐스트 결과에서 빼야 뒤의 행성·빔·다른 칩이 정상적으로 호버된다.
 */
function filteredRaycast(accept: (instanceId: number, uv: THREE.Vector2) => boolean) {
  const tmp: THREE.Intersection[] = [];
  return function raycast(this: THREE.InstancedMesh, rc: THREE.Raycaster, out: THREE.Intersection[]) {
    tmp.length = 0;
    THREE.InstancedMesh.prototype.raycast.call(this, rc, tmp);
    for (const h of tmp) if (h.instanceId !== undefined && h.uv && accept(h.instanceId, h.uv)) out.push(h);
  };
}

/**
 * 기업 노드 = 저폴리 툰 행성 (장난감 우주 스타일).
 * - 산업색에서 만든 팔레트로 면 단위 바다·대륙을 칠하고, 대륙형·고리형·위성형을 기업 id 시드로 섞는다
 *   (은하 뷰와 기업 중심 뷰에서 같은 기업이 같은 행성으로 보인다)
 * - 크기는 연결 가중치(node.size), 호버·중심·강조로 크기와 밝기를 프레임마다 lerp. 위치는 live 버퍼(morph.ts)에서 읽는다
 * - 행성 표면은 비워 두고, 로고·기업명은 행성 위 깃발 칩(InstancedMesh 1회)에 놓는다. 칩은 화면상 약 20px 높이로
 *   항상 카메라를 향하고, 티어(허브·첫 껍질·나머지)별 거리 LOD 와 화면 버킷으로 혼잡을 억제한다
 * - 정면 배지(흰 로고 원판)는 emphasis.badges — 호버·중심·선택 간선 양끝 — 에서만 카메라→행성 시선 위에 떠오른다
 * - 멀리서도 위치가 보이도록 아주 약한 헤일로 점광을 함께 둔다
 */
export default function CompanyNodes({ model, emphasis, hoveredId, onHover, onClick, showLabels = true, onPin }: Props) {
  const tags = useRef<THREE.InstancedMesh>(null);
  const chips = useRef<THREE.InstancedMesh>(null);
  const ring = useRef<THREE.Sprite>(null);
  const groups = useRef<(THREE.Group | null)[]>([]);
  const bodies = useRef<(THREE.Mesh | null)[]>([]);
  const moons = useRef<(THREE.Group | null)[]>([]);
  const n = model.nodes.length;
  const live = liveFor(model);

  const planets = useMemo<Planet[]>(
    () =>
      model.nodes.map((node) => {
        // 배열 인덱스가 아니라 기업 id 로 시드를 만들어 뷰가 바뀌어도 외형이 유지된다
        const h = (hashString(node.id) % 1000) / 1000;
        const variant: Variant = h < 0.15 ? "ring" : h < 0.27 ? "moon" : "land";
        const radius = node.size * 1.3;
        const palette = planetPaletteFor(node.color);
        const seed = 0.5 + h * 9;
        // 세분화는 반지름과 무관하게 고정한다 — 뷰마다 size 가 달라도(기업 중심의 중심 3.4 등) 같은 면 분할·대륙 모양이 나오도록.
        // 188개 × 320면이라 비용은 무시할 수준
        const geo = planetGeometry(radius, palette, seed, 2, variant === "ring" ? 0.3 : 0.45);
        const moonGeo = variant === "moon" ? planetGeometry(radius * 0.3, palette, seed + 5, 1, 0.2) : null;
        return { node, radius, palette, variant, seed, geo, moonGeo };
      }),
    [model],
  );
  useEffect(
    () => () => {
      planets.forEach((p) => {
        p.geo.dispose();
        p.moonGeo?.dispose();
      });
    },
    [planets],
  );

  // 모프 없는 모델 교체(필터·폴백)는 기존처럼 팝인하고, 워프 모프 중 마운트는 이어받은 상태를 보존한다
  useEffect(() => {
    if (morphState.plan) return;
    model.nodes.forEach((node) => {
      animFor(node.id).scale = 0.001;
    });
  }, [model]);

  // 배지·칩·헤일로 버퍼
  const built = useMemo(() => {
    const color = new Float32Array(n * 3);
    const bright = new Float32Array(n).fill(1);
    const emph = new Float32Array(n).fill(1);
    const alpha = new Float32Array(n);
    const cell = new Float32Array(n * 2);
    const size = new Float32Array(n);
    const haloA = new Float32Array(n).fill(1);
    const nameCells: (NameCell | null)[] = [];
    const name = new Float32Array(n * 4);
    const c = new THREE.Color();
    const center = new THREE.Vector3();
    model.nodes.forEach((node, i) => {
      c.set(node.color);
      color[i * 3] = c.r;
      color[i * 3 + 1] = c.g;
      color[i * 3 + 2] = c.b;
      size[i] = node.size;
      center.x += node.pos[0];
      center.y += node.pos[1];
      center.z += node.pos[2];
      const idx = ensureLogoCell(`${node.stockCode}|${node.name}`, initials(node.name), node.color, logoUrl(node.stockCode));
      const [u, v] = cellUv(idx);
      cell[i * 2] = u;
      cell[i * 2 + 1] = v;
      const nc = ensureNameCell(node.name);
      nameCells.push(nc);
      if (nc) {
        name[i * 4] = nc.u;
        name[i * 4 + 1] = nc.v;
        name[i * 4 + 2] = nc.w;
        name[i * 4 + 3] = nc.h;
      }
    });
    // 레이캐스트용 경계구 — InstancedMesh 는 첫 레이캐스트 때의 행렬로 한 번만 계산하므로 여유를 두고 직접 준다
    if (n > 0) center.multiplyScalar(1 / n);
    let reach = 0;
    model.nodes.forEach((node) => {
      reach = Math.max(reach, TMP.fromArray(node.pos).distanceTo(center) + node.size * 4);
    });
    const bounds = new THREE.Sphere(center, reach + 40);

    const dyn = (arr: Float32Array, itemSize: number) => {
      const a = new THREE.InstancedBufferAttribute(arr, itemSize);
      a.setUsage(THREE.DynamicDrawUsage);
      return a;
    };

    const geo = new THREE.PlaneGeometry(2, 2);
    geo.setAttribute("aColor", new THREE.InstancedBufferAttribute(color, 3));
    geo.setAttribute("aCell", new THREE.InstancedBufferAttribute(cell, 2));
    const brightAttr = dyn(bright, 1);
    const emphAttr = dyn(emph, 1);
    const alphaAttr = dyn(alpha, 1);
    geo.setAttribute("aBright", brightAttr);
    geo.setAttribute("aEmph", emphAttr);
    geo.setAttribute("aAlpha", alphaAttr);

    // 칩: 로컬 y ∈ [-0.35, 1] (아래 0.35 가 스템). x 폭은 인스턴스 행렬로 aspect 배 늘린다
    const chipGeo = new THREE.PlaneGeometry(1, CHIP_H);
    chipGeo.translate(0, CHIP_H / 2 - CHIP_STEM, 0);
    chipGeo.setAttribute("aColor", new THREE.InstancedBufferAttribute(color, 3));
    chipGeo.setAttribute("aCell", new THREE.InstancedBufferAttribute(cell, 2));
    chipGeo.setAttribute("aName", new THREE.InstancedBufferAttribute(name, 4));
    const aspectArr = new Float32Array(n).fill(1);
    const chipAspect = dyn(aspectArr, 1);
    // (alpha, emph, bright, nameOn) 을 vec4 하나로 — 정점 속성 수를 16 개 한도 아래로 유지한다 (chipShader 주석 참고)
    const chipStateArr = new Float32Array(n * 4);
    for (let i = 0; i < n; i += 1) {
      chipStateArr[i * 4 + 1] = 1;
      chipStateArr[i * 4 + 2] = 1;
      chipStateArr[i * 4 + 3] = 1;
    }
    const chipState = dyn(chipStateArr, 4);
    const chipRole = dyn(new Float32Array(n * 4), 4);
    const chipRoleColor = dyn(new Float32Array(n * 3), 3);
    chipGeo.setAttribute("aAspect", chipAspect);
    chipGeo.setAttribute("aState", chipState);
    chipGeo.setAttribute("aRole", chipRole);
    chipGeo.setAttribute("aRoleColor", chipRoleColor);

    // 헤일로는 live 버퍼를 그대로 정점 위치로 쓴다 (모프 중에는 프레임마다 업로드)
    const haloGeo = new THREE.BufferGeometry();
    const haloPos = new THREE.BufferAttribute(liveFor(model).pos, 3);
    haloPos.setUsage(THREE.DynamicDrawUsage);
    haloGeo.setAttribute("position", haloPos);
    haloGeo.setAttribute("color", new THREE.BufferAttribute(color.slice(), 3));
    const haloSize = new THREE.BufferAttribute(size, 1);
    haloSize.setUsage(THREE.DynamicDrawUsage);
    const haloAlpha = new THREE.BufferAttribute(haloA, 1);
    haloAlpha.setUsage(THREE.DynamicDrawUsage);
    haloGeo.setAttribute("aSize", haloSize);
    haloGeo.setAttribute("aAlpha", haloAlpha);
    return { geo, brightAttr, emphAttr, alphaAttr, chipGeo, chipAspect, chipState, chipRole, aspectArr, nameCells, bounds, haloGeo, haloPos, haloSize, haloAlpha };
  }, [model, n]);
  useEffect(
    () => () => {
      built.geo.dispose();
      built.chipGeo.dispose();
      built.haloGeo.dispose();
    },
    [built],
  );

  // 프레임 스크래치 — 칩 높이·목표 알파 (버킷 판정이 끝난 뒤 두 번째 루프에서 쓴다)
  const scratch = useMemo(() => ({ h: new Float32Array(n), target: new Float32Array(n) }), [n]);

  useEffect(() => {
    const init = (m: THREE.InstancedMesh | null, ud: Record<string, unknown>) => {
      if (!m) return;
      Object.assign(m.userData, ud);
      m.boundingSphere = built.bounds.clone();
      // 행렬을 프레임마다 덮어쓰므로 usage 힌트도 동적으로 (기본은 STATIC_DRAW)
      m.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      for (let i = 0; i < n; i += 1) {
        tmpObj.position.set(0, 0, 0);
        tmpObj.quaternion.identity();
        tmpObj.scale.setScalar(0.001);
        tmpObj.updateMatrix();
        m.setMatrixAt(i, tmpObj.matrix);
      }
      m.instanceMatrix.needsUpdate = true;
    };
    // 두 메시 모두 raycast 를 감싸 원판·캡슐 안의 보이는 히트만 남기므로, 간선 피킹(RelationLines.nodeAhead)은 pick 만 보면 된다
    init(tags.current, { pick: "node" });
    init(chips.current, { pick: "node" });
  }, [model, n, built]);

  const discUniforms = useMemo(() => ({ uAtlas: { value: logoAtlasTexture() }, uCell: { value: ATLAS_CELL_UV } }), []);
  const chipUniforms = useMemo(
    () => ({ uAtlas: { value: logoAtlasTexture() }, uCell: { value: ATLAS_CELL_UV }, uNames: { value: nameAtlasTexture() }, uPill: { value: new THREE.Color(CHIP_PILL) } }),
    [],
  );
  const haloUniforms = useMemo(() => ({ uMap: { value: dotTexture() } }), []);
  const grad = toonGradient(3);

  useFrame((state, dt) => {
    const k = 1 - Math.pow(0.001, dt);
    // 배지·칩 페이드는 더 빠르게 (0.3s 안에 거의 끝난다)
    const kFast = 1 - Math.pow(0.0001, dt);
    const t = state.clock.elapsedTime;
    const cam = state.camera;
    const tagMesh = tags.current;
    const chipMesh = chips.current;
    const morphing = morphState.plan !== null;

    // 칩 배치 기준: 카메라 상·우 방향, 화면 20px 에 해당하는 월드 높이 계수
    UPV.set(0, 1, 0).applyQuaternion(cam.quaternion);
    RGT.set(1, 0, 0).applyQuaternion(cam.quaternion);
    const persp = cam as THREE.PerspectiveCamera;
    const fov = persp.isPerspectiveCamera ? persp.fov : 52;
    const pxK = (2 * Math.tan(THREE.MathUtils.degToRad(fov) / 2) * CHIP_PX) / Math.max(1, state.size.height);
    const W = state.size.width;
    const H = state.size.height;
    const tierFar = TIER_FAR[model.kind];
    // 버킷 우선순위: 호버 > 중심 기업 > 강조 고정 > 낮은 티어. 호버·중심에 별도 등급이 없으면 같은 버킷의 큰 pinned 이웃이
    // 호버한 기업의 칩을 밀어내 숨긴다
    const prioOf = (nd: SceneNode) => (nd.id === hoveredId ? 5 : nd.id === model.centerId ? 4 : emphasis.pinned.has(nd.id) ? 3 : 2 - nd.tier);
    BUCKETS.clear();

    planets.forEach((p, i) => {
      const { node } = p;
      const a = animFor(node.id);
      const emph = emphasis.node.get(node.id) ?? 1;
      const isHover = node.id === hoveredId;
      const isCenter = node.id === model.centerId;
      const pinned = emphasis.pinned.has(node.id);
      const targetScale = (isHover ? 1.18 : 1) * (isCenter ? 1.1 : 1) * (0.7 + 0.3 * emph);
      const targetBright = (isHover ? 1.15 : isCenter ? 1.06 : 1.0) * (0.42 + 0.58 * emph);
      const s = (a.scale += (targetScale - a.scale) * k);
      const br = (a.bright += (targetBright - a.bright) * k);

      TMP.fromArray(live.pos, i * 3);
      const g = groups.current[i];
      if (g) {
        g.position.copy(TMP);
        g.scale.setScalar(s * live.scale[i]);
      }
      const body = bodies.current[i];
      if (body) {
        body.rotation.y = t * 0.12 + p.seed;
        (body.material as THREE.MeshToonMaterial).color.setScalar(br);
      }
      const moon = moons.current[i];
      if (moon) moon.rotation.y = t * 0.5 + p.seed;

      TO_CAM.copy(cam.position).sub(TMP);
      const dist = TO_CAM.length();
      TO_CAM.multiplyScalar(1 / Math.max(1e-6, dist));
      const r = p.radius * s * live.scale[i];

      // 정면 배지: badges 에 든 노드만. 행성 중심에서 카메라 쪽으로 반지름만큼 나온 지점에 빌보드로 놓아 항상 한가운데
      const tagTarget = emphasis.badges.has(node.id) && (!morphing || isCenter) ? 1 : 0;
      const ta = (a.tagA += (tagTarget - a.tagA) * kFast);
      if (tagMesh) {
        tmpObj.position.copy(TMP).addScaledVector(TO_CAM, r * 1.04);
        tmpObj.quaternion.copy(cam.quaternion);
        tmpObj.scale.setScalar(ta < 0.01 ? 0.001 : r * 0.62 * (0.6 + 0.4 * ta));
        tmpObj.updateMatrix();
        tagMesh.setMatrixAt(i, tmpObj.matrix);
        (built.brightAttr.array as Float32Array)[i] = isHover ? 1.05 : 1;
        (built.emphAttr.array as Float32Array)[i] = emph;
        (built.alphaAttr.array as Float32Array)[i] = ta;
      }
      (built.haloSize.array as Float32Array)[i] = r * 3.2 * (0.5 + 0.5 * emph);
      (built.haloAlpha.array as Float32Array)[i] = (0.15 + 0.85 * emph) * 0.22;

      // 칩 목표 알파: 티어별 거리 LOD × 강조, 고정 노드는 항상. 모프 중에는 중심만
      scratch.h[i] = THREE.MathUtils.clamp(dist * pxK, CHIP_MIN, CHIP_MAX);
      const far = tierFar[Math.min(node.tier, tierFar.length - 1)];
      let ct = pinned ? 1 : 1 - THREE.MathUtils.smoothstep(dist, far * 0.75, far);
      ct *= 0.15 + 0.85 * emph;
      if (morphing && !isCenter) ct = 0;
      if (ct > 0.01) {
        // 카메라 뒤는 제외, 앞이면 화면 버킷으로 겹침을 억제한다 (우선순위: prioOf, 동률은 큰 행성)
        NDC.copy(TMP).applyMatrix4(cam.matrixWorldInverse);
        if (NDC.z >= -1e-3) ct = 0;
        else {
          NDC.applyMatrix4(cam.projectionMatrix);
          const cx = Math.floor(((NDC.x + 1) * 0.5 * W) / BUCKET_W);
          const cy = Math.floor(((1 - NDC.y) * 0.5 * H) / BUCKET_H);
          const key = cy * 4096 + cx;
          const prio = prioOf(node);
          const cur = BUCKETS.get(key);
          if (cur === undefined) BUCKETS.set(key, i);
          else {
            const other = planets[cur].node;
            const otherPrio = prioOf(other);
            if (prio > otherPrio || (prio === otherPrio && node.size > other.size)) {
              scratch.target[cur] = 0;
              BUCKETS.set(key, i);
            } else ct = 0;
          }
        }
      }
      scratch.target[i] = ct;
    });
    if (tagMesh) {
      tagMesh.instanceMatrix.needsUpdate = true;
      built.brightAttr.needsUpdate = true;
      built.emphAttr.needsUpdate = true;
      built.alphaAttr.needsUpdate = true;
    }
    built.haloSize.needsUpdate = true;
    built.haloAlpha.needsUpdate = true;
    if (morphing) built.haloPos.needsUpdate = true;

    // 칩 — 버킷 판정이 끝난 뒤 알파를 lerp 하고 행렬·속성을 쓴다
    if (chipMesh) {
      const stateArr = built.chipState.array as Float32Array;
      const roleArr = built.chipRole.array as Float32Array;
      for (let i = 0; i < n; i += 1) {
        const p = planets[i];
        const { node } = p;
        const a = animFor(node.id);
        const ca = (a.chipA += (scratch.target[i] - a.chipA) * kFast);
        const pinned = emphasis.pinned.has(node.id);
        a.nameOn += ((showLabels || pinned ? 1 : 0) - a.nameOn) * kFast;
        const roleW = roleArr[i * 4 + 3] > 0 ? (roleArr[i * 4 + 2] / roleArr[i * 4 + 3]) * 0.8 : 0;
        const aspect = 1 + a.nameOn * ((built.nameCells[i]?.aspect ?? 0) * 0.8 + 0.3) + (roleW > 0 ? roleW + 0.15 : 0);
        built.aspectArr[i] = aspect;
        stateArr[i * 4] = ca;
        stateArr[i * 4 + 1] = emphasis.node.get(node.id) ?? 1;
        stateArr[i * 4 + 2] = node.id === hoveredId ? 1.1 : 1;
        stateArr[i * 4 + 3] = a.nameOn;
        if (ca < 0.01) {
          // 안 보이는 칩은 접어 두어 레이캐스트에도 걸리지 않게 한다
          tmpObj.position.set(0, 0, 0);
          tmpObj.scale.setScalar(0.001);
        } else {
          const h = scratch.h[i];
          TMP.fromArray(live.pos, i * 3);
          TO_CAM.copy(cam.position).sub(TMP).normalize();
          const r = p.radius * a.scale * live.scale[i];
          // 로고 원(x=0.5)이 행성 바로 위에 오도록 캡슐 중심을 오른쪽으로 밀고, 살짝 카메라 쪽으로 빼 행성에 가리지 않게 한다
          tmpObj.position
            .copy(TMP)
            .addScaledVector(UPV, r * 1.05 + CHIP_STEM * h)
            .addScaledVector(RGT, (aspect * 0.5 - 0.5) * h)
            .addScaledVector(TO_CAM, r * 0.2);
          tmpObj.quaternion.copy(cam.quaternion);
          tmpObj.scale.set(h * aspect, h, 1);
        }
        tmpObj.updateMatrix();
        chipMesh.setMatrixAt(i, tmpObj.matrix);
      }
      chipMesh.instanceMatrix.needsUpdate = true;
      built.chipAspect.needsUpdate = true;
      built.chipState.needsUpdate = true;
    }

    const rg = ring.current;
    if (rg) {
      const target = hoveredId ? model.nodeById.get(hoveredId) : undefined;
      if (target) {
        const i = model.nodes.indexOf(target);
        rg.visible = true;
        rg.position.fromArray(live.pos, i * 3);
        rg.scale.setScalar(planets[i].radius * animFor(target.id).scale * live.scale[i] * 3.0);
        const mat = rg.material as THREE.SpriteMaterial;
        mat.opacity = 0.35 + Math.sin(t * 3) * 0.08;
        mat.color.set(target.color);
        mat.rotation = t * 0.4;
      } else rg.visible = false;
    }
  });

  // 배지는 보이는 원판 안, 칩은 보이는 캡슐 안만 히트로 남긴다 (스템·투명 모서리·접힌 칩은 레이캐스트 단계에서 제외)
  const tagRaycast = useMemo(
    () =>
      filteredRaycast((i, uv) => {
        const node = model.nodes[i];
        return !!node && animFor(node.id).tagA >= PICK_ALPHA && Math.hypot(uv.x - 0.5, uv.y - 0.5) <= 0.5;
      }),
    [model],
  );
  const chipRaycast = useMemo(
    () =>
      filteredRaycast((i, uv) => {
        const node = model.nodes[i];
        return !!node && animFor(node.id).chipA >= PICK_ALPHA && chipHit(built.aspectArr[i], uv);
      }),
    [model, built],
  );

  const pickable = (id: string) => !morphState.plan && (emphasis.node.get(id) ?? 1) > 0.05;
  /** 인스턴스 히트 → 기업 id. 원판·캡슐 판정은 raycast 래퍼가 이미 끝냈다 */
  const idOf = (e: ThreeEvent<PointerEvent | MouseEvent>) => (e.instanceId === undefined ? null : (model.nodes[e.instanceId]?.id ?? null));
  /**
   * 포인터가 빠져나갈 때 호버를 푼다 — 단, 그 노드가 현재 호버 주인일 때만.
   * pickable 이 아니어서 stopPropagation 없이 지나간 인스턴스도 R3F 는 hovered 로 등록하므로, 무조건 null 을 보내면
   * 그 뒤에서 호버 중이던 다른 행성의 강조가 끊긴다 (그 행성은 hovered 맵에 남아 다시 over 가 오지 않는다).
   */
  const release = (id: string | null) => {
    if (id && id === hoveredId) onHover(null);
  };
  const activate = (e: ThreeEvent<MouseEvent>, id: string) => {
    if (e.nativeEvent.shiftKey && onPin) onPin(id);
    else onClick(id);
  };

  return (
    <group>
      {planets.map((p, i) => (
        <group
          key={p.node.id}
          ref={(el) => {
            groups.current[i] = el;
          }}
          position={p.node.pos}
          scale={0.001}
        >
          <mesh
            ref={(el) => {
              bodies.current[i] = el;
            }}
            geometry={p.geo}
            onUpdate={(m) => {
              m.layers.enable(LIT);
              m.userData.pick = "node";
            }}
            onPointerOver={(e) => {
              if (!pickable(p.node.id)) return;
              e.stopPropagation();
              onHover(p.node.id);
            }}
            onPointerOut={() => release(p.node.id)}
            onClick={(e) => {
              if (!pickable(p.node.id)) return;
              e.stopPropagation();
              activate(e, p.node.id);
            }}
          >
            <meshToonMaterial vertexColors gradientMap={grad} />
          </mesh>
          {p.variant === "ring" && (
            <mesh rotation={[Math.PI / 2 - 0.42, 0.25, 0]} raycast={() => null} onUpdate={(m) => m.layers.enable(LIT)}>
              <ringGeometry args={[p.radius * 1.45, p.radius * 2.05, 40, 1]} />
              <meshToonMaterial color={p.palette.ring} gradientMap={grad} side={THREE.DoubleSide} transparent opacity={0.9} />
            </mesh>
          )}
          {p.variant === "moon" && p.moonGeo && (
            <group
              ref={(el) => {
                moons.current[i] = el;
              }}
              rotation={[0.3, 0, 0.15]}
            >
              <mesh geometry={p.moonGeo} position={[p.radius * 2.0, 0, 0]} raycast={() => null} onUpdate={(m) => m.layers.enable(LIT)}>
                <meshToonMaterial vertexColors gradientMap={grad} />
              </mesh>
            </group>
          )}
        </group>
      ))}

      {/* 정면 배지 — 호버·중심·선택 간선 양끝에만, 드로우콜 1회 */}
      <instancedMesh
        ref={tags}
        args={[built.geo, undefined, n]}
        frustumCulled={false}
        renderOrder={3}
        raycast={tagRaycast}
        onPointerMove={(e) => {
          const id = idOf(e);
          if (!id || !pickable(id)) return;
          e.stopPropagation();
          onHover(id);
        }}
        onPointerOut={(e) => release(idOf(e))}
        onClick={(e) => {
          const id = idOf(e);
          if (!id || !pickable(id)) return;
          e.stopPropagation();
          activate(e, id);
        }}
      >
        <shaderMaterial vertexShader={discVertex} fragmentShader={discFragment} uniforms={discUniforms} transparent depthWrite={false} toneMapped={false} />
      </instancedMesh>

      {/* 이름표 칩 — 로고 원 + 기업명, 드로우콜 1회. 행성보다 앞이면 그려지고 다른 행성 뒤면 가려진다.
          히트는 캡슐 안으로 제한되므로 onPointerOut 이 곧 캡슐 이탈이다 */}
      <instancedMesh
        ref={chips}
        args={[built.chipGeo, undefined, n]}
        frustumCulled={false}
        renderOrder={4}
        raycast={chipRaycast}
        onPointerMove={(e) => {
          const id = idOf(e);
          if (!id || !pickable(id)) return;
          e.stopPropagation();
          onHover(id);
        }}
        onPointerOut={(e) => release(idOf(e))}
        onClick={(e) => {
          const id = idOf(e);
          if (!id || !pickable(id)) return;
          e.stopPropagation();
          activate(e, id);
        }}
      >
        <shaderMaterial vertexShader={chipVertex} fragmentShader={chipFragment} uniforms={chipUniforms} transparent depthWrite={false} depthTest toneMapped={false} />
      </instancedMesh>

      <points geometry={built.haloGeo} frustumCulled={false} raycast={() => null} renderOrder={1}>
        <shaderMaterial
          transparent
          depthWrite={false}
          blending={THREE.AdditiveBlending}
          vertexColors
          toneMapped={false}
          uniforms={haloUniforms}
          vertexShader={/* glsl */ `
            attribute float aSize;
            attribute float aAlpha;
            varying vec3 vColor;
            varying float vAlpha;
            void main(){
              vColor = color; vAlpha = aAlpha;
              vec4 mv = modelViewMatrix * vec4(position, 1.0);
              gl_PointSize = aSize * (300.0 / -mv.z);
              gl_Position = projectionMatrix * mv;
            }
          `}
          fragmentShader={/* glsl */ `
            uniform sampler2D uMap;
            varying vec3 vColor;
            varying float vAlpha;
            void main(){
              vec4 t = texture2D(uMap, gl_PointCoord);
              gl_FragColor = vec4(vColor * t.a, t.a * vAlpha);
            }
          `}
        />
      </points>
      <sprite ref={ring} visible={false} raycast={() => null} renderOrder={4}>
        <spriteMaterial map={ringTexture()} transparent depthWrite={false} toneMapped={false} blending={THREE.AdditiveBlending} />
      </sprite>
    </group>
  );
}
