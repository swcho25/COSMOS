import { useEffect, useMemo, useRef, type MutableRefObject } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { ORBIT, type SceneModel } from "@/lib/graph";
import { hashString, seededRandom } from "@/lib/rng";
import { useGalaxy } from "@/store/galaxy";
import { buildWellField, isLowEnd, MAX_WELLS, writeWells, type WellField } from "./gravity";
import { dragState, liveFor, morphState } from "./morph";
import { LAB } from "./toon";

interface Props {
  model: SceneModel;
  /** 워프 강도 0~1 (Scene 의 Director 가 채운다) — 격자를 어둡게 하고 목표 지점에 잔물결을 낸다 */
  warpRef: MutableRefObject<number>;
  hoveredId: string | null;
}

/** 평평한 면이 패여 들어가는 시간 (마운트·모델 교체 기준) */
const SETTLE = 0.9;
/** 궤도 링 한 바퀴의 점 수 */
const RING_PTS = 200;
/** 궤도 링 점선 칸 수 — aRand 를 이 주기로 톱니를 만들어 프래그먼트에서 잘라 낸다 */
const RING_DASH = 20;
/** 호버한 기업의 딤플 깊이 배율 */
const HOVER_DEEP = 1.6;

const easeOutCubic = (x: number) => 1 - Math.pow(1 - x, 3);

/**
 * 중력 우물 격자 — 은하 아래에 깔리는 점 바닥.
 * 정점 셰이더가 우물 텍스처를 훑어 깊이(아래로 패임)와 기울기(우물 쪽으로 쓸림)를 구하고, 프래그먼트가 깊이색·안개·가장자리
 * 페이드를 입힌다. 기업 중심 뷰에서는 궤도 반지름(ORBIT)에 점선 링을 얹어 같은 변위로 휘게 해 사면이 계단처럼 읽힌다.
 * 위치는 liveFor(model) 을 읽으므로 워프 모프 중에도, 행성을 끌어 옮겼을 때도 우물이 함께 움직인다.
 */
export default function GravityGrid({ model, warpRef, hoveredId }: Props) {
  const mat = useRef<THREE.ShaderMaterial>(null);

  const built = useMemo(() => {
    const field = buildWellField(model);
    const low = isLowEnd();
    const n = model.kind === "galaxy" ? (low ? 96 : 128) : low ? 72 : 96;
    const half = field.half;
    // 기업 중심 뷰만 궤도 링 — 은하의 반지름은 질량 랭크라 눈금이 될 기준 원이 없다
    const rings = model.kind === "system" ? ORBIT.slice(1) : [];
    const total = n * n + rings.length * RING_PTS;
    const position = new Float32Array(total * 3);
    const rand = new Float32Array(total);
    const ring = new Float32Array(total);
    const rng = seededRandom(hashString(model.kind + (model.centerId ?? "")));
    let o = 0;
    for (let ix = 0; ix < n; ix += 1) {
      for (let iz = 0; iz < n; iz += 1) {
        position[o * 3] = -half + (2 * half * ix) / (n - 1);
        position[o * 3 + 2] = -half + (2 * half * iz) / (n - 1);
        rand[o] = rng();
        o += 1;
      }
    }
    // 링은 layoutSystem 과 같은 타원(z 를 0.82 배)이라 궤도 위 행성과 정확히 겹친다
    rings.forEach((r) => {
      for (let j = 0; j < RING_PTS; j += 1) {
        const a = (j / RING_PTS) * Math.PI * 2;
        position[o * 3] = Math.cos(a) * r;
        position[o * 3 + 2] = Math.sin(a) * r * 0.82;
        rand[o] = ((j * RING_DASH) / RING_PTS) % 1;
        ring[o] = 1;
        o += 1;
      }
    });

    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(position, 3));
    geo.setAttribute("aRand", new THREE.BufferAttribute(rand, 1));
    geo.setAttribute("aRing", new THREE.BufferAttribute(ring, 1));

    const uniforms = {
      uWellTex: { value: field.tex },
      uCount: { value: field.count },
      uFloorY: { value: field.floorY },
      uHalf: { value: half },
      uMaxDepth: { value: field.maxDepth },
      uDotPx: { value: 2.2 },
      uDpr: { value: 1 },
      uScale: { value: 600 },
      uSettle: { value: 0 },
      uFade: { value: 1 },
      uWarp: { value: 0 },
      uWarpXZ: { value: new THREE.Vector2() },
      uTime: { value: 0 },
      uColor: { value: new THREE.Color(LAB.grid) },
      uColorDeep: { value: new THREE.Color(LAB.gridDeep) },
    };
    return { field, geo, uniforms };
  }, [model]);

  useEffect(
    () => () => {
      built.geo.dispose();
      built.field.tex.dispose();
    },
    [built],
  );

  /**
   * 프레임 상태. useMemo 값에 쓰면 react-hooks 불변 규칙에 걸리므로 갱신되는 값은 모두 여기(ref)에 둔다.
   * scale 은 슬롯별 질량 배율(호버 딤플), sync 는 모델 교체 후 유니폼을 한 번 다시 맞추라는 표식이다.
   */
  const frame = useRef({ field: null as WellField | null, settle: 0, scale: new Float32Array(0), sync: true, dirty: true, dragVersion: dragState.version });

  useFrame((state, delta) => {
    const dt = Math.min(delta, 0.05);
    const f = frame.current;
    const field = built.field;
    if (f.field !== field) {
      f.field = field;
      f.settle = 0;
      f.scale = new Float32Array(field.nodeCount).fill(1);
      f.sync = true;
      f.dirty = true;
    }
    f.settle = Math.min(1, f.settle + dt / SETTLE);

    // 호버 딤플 — 호버한 기업의 우물만 깊어진다. 0.15s 안에 거의 도달
    const hoverSlot = hoveredId ? field.slotOf.get(hoveredId) : undefined;
    const k = 1 - Math.pow(0.0001, dt * 2);
    let moving = false;
    for (let s = 0; s < f.scale.length; s += 1) {
      const target = s === hoverSlot ? HOVER_DEEP : 1;
      const cur = f.scale[s];
      if (Math.abs(target - cur) > 1e-3) {
        f.scale[s] = cur + (target - cur) * k;
        moving = true;
      } else if (cur !== target) {
        f.scale[s] = target;
        moving = true;
      }
    }
    // 우물 텍스처는 바뀔 때만 올린다 — 정적인 장면에서는 프레임당 CPU 비용이 0 이다
    if (f.dirty || moving || morphState.plan !== null || f.dragVersion !== dragState.version) {
      f.dragVersion = dragState.version;
      writeWells(field, liveFor(model), f.scale);
      f.dirty = false;
    }

    const u = mat.current?.uniforms;
    if (!u) return;
    if (f.sync) {
      f.sync = false;
      u.uWellTex.value = field.tex;
      u.uCount.value = field.count;
      u.uFloorY.value = field.floorY;
      u.uHalf.value = field.half;
      u.uMaxDepth.value = field.maxDepth;
    }
    u.uSettle.value = easeOutCubic(f.settle);
    u.uTime.value = state.clock.elapsedTime;
    u.uScale.value = state.size.height * 0.62;
    // AdaptiveDpr 로 DPR 이 바뀌어도 점의 화면 크기가 유지되도록 보정한다
    u.uDpr.value = state.viewport.dpr;
    const warp = warpRef.current;
    u.uWarp.value = warp;
    if (warp > 0.01) {
      // 워프 목표 지점에서 잔물결이 퍼진다. 목표가 없으면(은하 복귀) 원점
      const targetId = useGalaxy.getState().warpTarget;
      const node = targetId ? model.nodeById.get(targetId) : undefined;
      const i = node ? model.nodes.indexOf(node) : -1;
      const live = liveFor(model);
      const w = u.uWarpXZ.value as THREE.Vector2;
      if (i >= 0) w.set(live.pos[i * 3], live.pos[i * 3 + 2]);
      else w.set(0, 0);
    }
  });

  return (
    <points geometry={built.geo} frustumCulled={false} raycast={() => null} renderOrder={0}>
      {/* 가산 합성이면 겹친 점이 하얗게 뭉쳐 플랫 컬러가 깨진다 — 격자는 일반 합성 + depthWrite off */}
      <shaderMaterial
        ref={mat}
        uniforms={built.uniforms}
        vertexShader={GRID_VERTEX}
        fragmentShader={GRID_FRAGMENT}
        transparent
        depthWrite={false}
        depthTest
        blending={THREE.NormalBlending}
        toneMapped={false}
      />
    </points>
  );
}

const GRID_VERTEX = /* glsl */ `
  attribute float aRand;
  attribute float aRing;
  uniform sampler2D uWellTex;
  uniform int uCount;
  uniform float uFloorY;
  uniform float uHalf;
  uniform float uMaxDepth;
  uniform float uDotPx;
  uniform float uDpr;
  uniform float uScale;
  uniform float uSettle;
  uniform float uWarp;
  uniform vec2 uWarpXZ;
  uniform float uTime;
  varying float vDepth;
  varying float vEdge;
  varying float vRing;
  varying float vRand;
  varying float vDist;
  void main(){
    vec2 p = position.xz;
    // 우물 합과 기울기. 루프 상한은 텍스처 폭 고정, 실제 개수는 uCount 에서 끊는다.
    // GLSL ES 1.00 은 루프 상한이 상수여야 하므로 MAX_WELLS 를 문자열로 박아 넣는다 — 상수 리터럴은 유지하면서
    // gravity.ts 의 값이 바뀌면 셰이더도 같이 바뀌어 '한쪽만 바꿔 조용히 우물이 잘리는' 결합을 없앤다
    float d = 0.0;
    vec2 grad = vec2(0.0);
    for (int i = 0; i < ${MAX_WELLS}; i++) {
      if (i >= uCount) break;
      vec4 w = texture2D(uWellTex, vec2((float(i) + 0.5) / ${MAX_WELLS}.0, 0.5));
      vec2 q = p - w.xy;
      float s2 = max(w.w * w.w, 1e-3);
      float g = w.z * exp(-dot(q, q) / (2.0 * s2));
      d += g;
      grad += g * (-q / s2);
    }
    // 고무판처럼 점이 우물 쪽으로 살짝 쓸려 모인다 (깊이는 쓸리기 전 자리에서 잰다)
    p += 0.3 * grad * uSettle;
    float rd = length(p - uWarpXZ);
    float ripple = uWarp * 1.5 * sin(rd * 0.3 - uTime * 6.0) * exp(-rd / 80.0);
    float y = uFloorY - d * uSettle + ripple;
    vDepth = clamp(d / uMaxDepth, 0.0, 1.0);
    // 가장자리는 72% 지점부터 사라져 격자의 네모난 끝이 보이지 않는다
    vEdge = 1.0 - smoothstep(0.72, 1.0, length(position.xz) / uHalf);
    vRing = aRing;
    vRand = aRand;
    vec4 mv = modelViewMatrix * vec4(p.x, y, p.y, 1.0);
    vDist = -mv.z;
    // 상·하한 모두 uDpr 배 = CSS px 기준이라 AdaptiveDpr 로 DPR 이 바뀌어도 점의 화면 크기가 그대로다.
    // 상한만 보정하면 고DPR 에서 먼 점(하한에 걸리는 점)이 절반 크기로 쪼그라들어 격자 끝이 갑자기 옅어진다.
    // 하한은 그래도 최소 1 device px — 서브픽셀 점은 아예 안 그려지는 GPU 가 있다
    gl_PointSize = clamp(uDotPx * uDpr * (1.0 + 0.9 * vDepth) * (uScale / -mv.z), max(1.0, uDpr), 6.0 * uDpr);
    gl_Position = projectionMatrix * mv;
  }
`;
const GRID_FRAGMENT = /* glsl */ `
  uniform float uFade;
  uniform float uWarp;
  uniform vec3 uColor;
  uniform vec3 uColorDeep;
  varying float vDepth;
  varying float vEdge;
  varying float vRing;
  varying float vRand;
  varying float vDist;
  void main(){
    float r = length(gl_PointCoord - 0.5) * 2.0;
    float a = smoothstep(1.0, 0.55, r);
    // 깊을수록 청백으로 — 분지 바닥이 은은히 빛난다
    vec3 col = mix(uColor, uColorDeep, pow(vDepth, 1.5));
    float fog = 1.0 - smoothstep(500.0, 1400.0, vDist);
    float alpha = a * (0.16 + 0.5 * vDepth) * vEdge * fog * uFade * (1.0 - 0.6 * uWarp);
    // 궤도 링은 점선
    if (vRing > 0.5) alpha *= 0.8 * step(0.35, vRand);
    if (alpha < 0.004) discard;
    gl_FragColor = vec4(col, alpha);
  }
`;
