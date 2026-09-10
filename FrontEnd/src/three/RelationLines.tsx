import { useEffect, useMemo } from "react";
import { useFrame, type ThreeEvent } from "@react-three/fiber";
import * as THREE from "three";
import type { SceneEdge } from "@/lib/graph";
import { bezier, controlPoint, radiusOf } from "./edgeCurve";
import type { Emphasis } from "./emphasis";
import { morphState } from "./morph";
import { LAB } from "./toon";

/** 튜브 길이 방향 분할, 둘레 분할, 간선당 빛 구슬 수 */
const SEGS = 22;
const RADIAL = 6;
const PULSES = 2;
const PICK_SEGS = 8;

const P = new THREE.Vector3();

interface Props {
  edges: SceneEdge[];
  emphasis: Emphasis;
  onHover?: (id: string | null) => void;
  onSelect?: (id: string) => void;
  /** 전체 밝기 배율 (은하 뷰처럼 간선이 많을 때 낮춰 노드·성단이 묻히지 않게) */
  intensity?: number;
  /** 호버 중인 간선 — 그 빔의 구슬이 2.2 배 빨리 흐르고 1.4 배 커져 방향이 한눈에 읽힌다 */
  hoveredEdgeId?: string | null;
  /**
   * 워프 연출(morphState)에 반응할지. 메인 씬만 true — 워프 가속 중 소멸(uFade)하고 착지 후 자라난다(uReveal).
   * 미니 씬은 워프와 무관하므로 끄고 항상 완전히 보인다.
   */
  warpFx?: boolean;
}
/** 호버 빔의 구슬 속도·크기 배율 */
const HOVER_SPEED = 2.2;
const HOVER_SIZE = 1.4;
/** 마운트 직후 빔이 자라나는 구간(초) — 워프 없이 모델이 바뀌었을 때도 툭 나타나지 않게 */
const AGE_IN = [0.35, 0.95];
/** 리빌 순서 폭 — 점수 낮은 빔일수록 이만큼 늦게 시작한다 (uReveal 과 셰이더 rv 식이 같아야 한다) */
const REVEAL_STAGGER = 0.35;
/** 피킹을 다시 허용하는 리빌 — 아직 자라는 중인 빔은 집히지 않는다 */
const PICK_REVEAL = 0.9;

/**
 * 인스턴스별 리빌 상태 — 마운트 시각(초)과 현재 리빌.
 * useRef 로 만든 객체를 useFrame 안에서 고치면 react-hooks/immutability 에 걸리므로(기준선 유지) 모듈 캐시에 둔다.
 * 키는 인스턴스마다 새로 만들어지는 built 객체라, 컴포넌트가 사라지면 항목도 함께 회수된다.
 */
const GATES = new WeakMap<object, { born: number; reveal: number }>();

const clamp01 = (x: number) => (x < 0 ? 0 : x > 1 ? 1 : x);
const smoothstep = (a: number, b: number, x: number) => {
  const t = clamp01((x - a) / (b - a));
  return t * t * (3 - 2 * t);
};
/** 셰이더 rv 와 같은 식 — 구슬도 빔과 같은 순서·같은 지점에서 나타난다 */
const revealAt = (reveal: number, score01: number) => clamp01((reveal - (1 - score01) * REVEAL_STAGGER) / (1 - REVEAL_STAGGER));

/**
 * 관계 빔 — 아트 디렉션 랩의 튜브 빔을 은하 전체에 적용한 것.
 * - 모든 간선을 3D 튜브 하나(드로우콜 1회)로 병합해 그린다. 굵기는 점수, 색은 주황 한 가지
 * - 같은 지오메트리를 법선 방향으로 부풀린 두 번째 패스가 부드러운 헤일로가 된다
 * - 빛 구슬(펄스)은 포인트 레이어 하나로 그리고, 매 프레임 베지어 위를 따라 움직인다.
 *   방향 있는 관계는 source → target 으로, 무방향 관계는 양방향으로 흐르며 점선 튜브를 쓴다
 * - 강조(emphasis) 값은 지오메트리를 다시 만들지 않고 속성 하나만 갱신한다
 */
export default function RelationLines({ edges, emphasis, onHover, onSelect, intensity = 1, hoveredEdgeId = null, warpFx = false }: Props) {
  const built = useMemo(() => {
    const n = edges.length;
    const ctrls: THREE.Vector3[] = [];
    const radii = new Float32Array(n);
    const speeds = new Float32Array(n);
    // 구슬 위상 — 시각(t)×속도 대신 프레임마다 누적해, 호버로 속도가 바뀌어도 구슬이 점프하지 않는다
    const phases = new Float32Array(n);

    // 튜브 병합 버퍼
    const vPer = (SEGS + 1) * (RADIAL + 1);
    const iPer = SEGS * RADIAL * 6;
    const position = new Float32Array(n * vPer * 3);
    const normal = new Float32Array(n * vPer * 3);
    const aT = new Float32Array(n * vPer);
    /** 성장 방향 좌표 — 원점에 가까운 끝이 0 이라 리빌이 안쪽에서 바깥으로 번진다 */
    const aT2 = new Float32Array(n * vPer);
    /** 간선별 성장 방향 (1 = from 이 안쪽) — 구슬 게이트도 같은 방향을 쓴다 */
    const outward = new Uint8Array(n);
    const aScore = new Float32Array(n * vPer);
    const aRadius = new Float32Array(n * vPer);
    const aFlow = new Float32Array(n * vPer);
    const aNeg = new Float32Array(n * vPer);
    const aEmph = new Float32Array(n * vPer).fill(1);
    const index = new Uint32Array(n * iPer);
    const pickPos = new Float32Array(n * PICK_SEGS * 2 * 3);

    edges.forEach((e, ei) => {
      const ctrl = new THREE.Vector3();
      controlPoint(e.from, e.to, ctrl);
      ctrls.push(ctrl);
      const s = e.score / 100;
      const r = radiusOf(s);
      radii[ei] = r;
      speeds[ei] = 0.05 + s * 0.14;
      phases[ei] = (ei * 0.6180339887) % 1;

      const curve = new THREE.QuadraticBezierCurve3(new THREE.Vector3().fromArray(e.from), ctrl, new THREE.Vector3().fromArray(e.to));
      const tube = new THREE.TubeGeometry(curve, SEGS, r, RADIAL, false);
      const tp = tube.attributes.position.array as Float32Array;
      const tn = tube.attributes.normal.array as Float32Array;
      const tuv = tube.attributes.uv.array as Float32Array;
      const ti = tube.index!.array;
      const vBase = ei * vPer;
      position.set(tp, vBase * 3);
      normal.set(tn, vBase * 3);
      // 기업 중심 뷰의 중심도, 은하 핵 성단도 원점이라 "원점에 가까운 끝"이 곧 안쪽이다
      const inward = Math.hypot(e.from[0], e.from[1], e.from[2]) <= Math.hypot(e.to[0], e.to[1], e.to[2]) ? 1 : 0;
      outward[ei] = inward;
      for (let v = 0; v < vPer; v += 1) {
        aT[vBase + v] = tuv[v * 2]; // u = 길이 방향 0~1
        aT2[vBase + v] = inward ? tuv[v * 2] : 1 - tuv[v * 2];
        aScore[vBase + v] = s;
        aRadius[vBase + v] = r;
        aFlow[vBase + v] = e.directed ? 1 : 0;
        aNeg[vBase + v] = e.impact === "NEGATIVE" ? 1 : 0;
      }
      const iBase = ei * iPer;
      for (let k = 0; k < iPer; k += 1) index[iBase + k] = ti[k] + vBase;
      tube.dispose();

      // 피킹용 폴리라인
      for (let k = 0; k < PICK_SEGS; k += 1) {
        const o = (ei * PICK_SEGS + k) * 6;
        bezier(e.from, e.to, ctrl, k / PICK_SEGS, P);
        pickPos[o] = P.x;
        pickPos[o + 1] = P.y;
        pickPos[o + 2] = P.z;
        bezier(e.from, e.to, ctrl, (k + 1) / PICK_SEGS, P);
        pickPos[o + 3] = P.x;
        pickPos[o + 4] = P.y;
        pickPos[o + 5] = P.z;
      }
    });

    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(position, 3));
    geo.setAttribute("normal", new THREE.BufferAttribute(normal, 3));
    geo.setAttribute("aT", new THREE.BufferAttribute(aT, 1));
    geo.setAttribute("aT2", new THREE.BufferAttribute(aT2, 1));
    geo.setAttribute("aScore", new THREE.BufferAttribute(aScore, 1));
    geo.setAttribute("aRadius", new THREE.BufferAttribute(aRadius, 1));
    geo.setAttribute("aFlow", new THREE.BufferAttribute(aFlow, 1));
    geo.setAttribute("aNeg", new THREE.BufferAttribute(aNeg, 1));
    const emphAttr = new THREE.BufferAttribute(aEmph, 1);
    emphAttr.setUsage(THREE.DynamicDrawUsage);
    geo.setAttribute("aEmph", emphAttr);
    geo.setIndex(new THREE.BufferAttribute(index, 1));
    geo.computeBoundingSphere();

    const pick = new THREE.BufferGeometry();
    pick.setAttribute("position", new THREE.BufferAttribute(pickPos, 3));

    // 빛 구슬
    const pn = n * PULSES;
    const pulseGeo = new THREE.BufferGeometry();
    const pPos = new THREE.BufferAttribute(new Float32Array(pn * 3), 3);
    pPos.setUsage(THREE.DynamicDrawUsage);
    const pSize = new THREE.BufferAttribute(new Float32Array(pn), 1);
    pSize.setUsage(THREE.DynamicDrawUsage);
    const pAlpha = new THREE.BufferAttribute(new Float32Array(pn), 1);
    pAlpha.setUsage(THREE.DynamicDrawUsage);
    const pNeg = new Float32Array(pn);
    edges.forEach((e, ei) => {
      for (let j = 0; j < PULSES; j += 1) pNeg[ei * PULSES + j] = e.impact === "NEGATIVE" ? 1 : 0;
    });
    pulseGeo.setAttribute("position", pPos);
    pulseGeo.setAttribute("aSize", pSize);
    pulseGeo.setAttribute("aAlpha", pAlpha);
    pulseGeo.setAttribute("aNeg", new THREE.BufferAttribute(pNeg, 1));

    return { geo, emphAttr, pick, ctrls, radii, speeds, phases, outward, pulseGeo, pPos, pSize, pAlpha, vPer };
  }, [edges]);

  useEffect(
    () => () => {
      built.geo.dispose();
      built.pick.dispose();
      built.pulseGeo.dispose();
    },
    [built],
  );

  // 강조 값만 갱신 (지오메트리 재생성 없음)
  useEffect(() => {
    const arr = built.emphAttr.array as Float32Array;
    edges.forEach((e, ei) => {
      const v = emphasis.edge.get(e.id) ?? 1;
      arr.fill(v, ei * built.vPer, (ei + 1) * built.vPer);
    });
    built.emphAttr.needsUpdate = true;
  }, [edges, emphasis, built]);

  // uFade·uReveal 은 강조(uDim)와 완전히 분리된 워프 게이트다 — 필터·호버로 흐려지는 것과 섞이면 안 된다
  const tubeUniforms = useMemo(
    () => ({ uColor: { value: new THREE.Color(LAB.beam) }, uDim: { value: 1 }, uInflate: { value: 0 }, uHalo: { value: 0 }, uFade: { value: 1 }, uReveal: { value: 1 } }),
    [],
  );
  const haloUniforms = useMemo(
    () => ({ uColor: { value: new THREE.Color(LAB.beamGlow) }, uDim: { value: 1 }, uInflate: { value: 1.7 }, uHalo: { value: 1 }, uFade: { value: 1 }, uReveal: { value: 1 } }),
    [],
  );
  const pulseUniforms = useMemo(() => ({ uDim: { value: 1 }, uScale: { value: 600 } }), []);

  useFrame((state, delta) => {
    const dt = Math.min(delta, 0.05);
    tubeUniforms.uDim.value = intensity;
    haloUniforms.uDim.value = intensity;
    pulseUniforms.uDim.value = intensity;
    // 포인트 크기를 화면 높이에 맞춘다 (fov 고정 가정)
    pulseUniforms.uScale.value = state.size.height * 0.62;

    // 낙하 중(plan)은 완전히 숨기고, 착지 후에는 morphState.reveal(0.6s 램프)과 마운트 램프 중 느린 쪽을 따른다.
    // 두 입력이 이미 시간 램프라 여기서 한 번 더 감쇠하지 않는다 — 그러면 "착지 후 0.6s" 를 넘긴다
    let gate = GATES.get(built);
    if (!gate) {
      gate = { born: state.clock.elapsedTime, reveal: warpFx ? 0 : 1 };
      GATES.set(built, gate);
    }
    const reveal = (gate.reveal = !warpFx ? 1 : morphState.plan ? 0 : Math.min(morphState.reveal, smoothstep(AGE_IN[0], AGE_IN[1], state.clock.elapsedTime - gate.born)));
    const fade = warpFx ? morphState.fade : 1;
    tubeUniforms.uFade.value = fade;
    haloUniforms.uFade.value = fade;
    tubeUniforms.uReveal.value = reveal;
    haloUniforms.uReveal.value = reveal;

    const pos = built.pPos.array as Float32Array;
    const size = built.pSize.array as Float32Array;
    const alpha = built.pAlpha.array as Float32Array;
    edges.forEach((e, ei) => {
      const emph = emphasis.edge.get(e.id) ?? 1;
      const ctrl = built.ctrls[ei];
      const hot = e.id === hoveredEdgeId;
      const phase = (built.phases[ei] = (built.phases[ei] + dt * built.speeds[ei] * (hot ? HOVER_SPEED : 1)) % 1);
      // 아직 자라지 않은 구간의 구슬은 빔 밖에 뜬다 — 튜브와 같은 리빌 지점에서 잘라 낸다
      const rvE = reveal >= 1 ? 1 : revealAt(reveal, e.score / 100);
      for (let j = 0; j < PULSES; j += 1) {
        const k = ei * PULSES + j;
        let u = (phase + j / PULSES) % 1;
        // 무방향 관계는 구슬이 양쪽으로 오간다
        if (!e.directed && j % 2 === 1) u = 1 - u;
        bezier(e.from, e.to, ctrl, u, P);
        pos[k * 3] = P.x;
        pos[k * 3 + 1] = P.y;
        pos[k * 3 + 2] = P.z;
        const bell = Math.sin(u * Math.PI);
        size[k] = built.radii[ei] * (6.0 + bell * 3.6) * (hot ? HOVER_SIZE : 1);
        const grown = (built.outward[ei] ? u : 1 - u) <= rvE;
        alpha[k] = emph <= 0.003 || !grown ? 0 : (0.35 + 0.65 * bell) * Math.min(1, emph * 1.4) * fade;
      }
    });
    built.pPos.needsUpdate = true;
    built.pSize.needsUpdate = true;
    built.pAlpha.needsUpdate = true;
  });

  /** 지금 리빌 — 아직 자라는 중(또는 워프로 사라진) 빔은 보이지 않으므로 집히지도 않아야 한다 */
  const revealOf = () => GATES.get(built)?.reveal ?? 1;
  const edgeIndexOf = (e: ThreeEvent<PointerEvent | MouseEvent>) => {
    const idx = e.index ?? 0;
    const ei = Math.floor(idx / (PICK_SEGS * 2));
    return edges[ei];
  };
  /**
   * 같은 광선에 기업 노드가 걸려 있으면 간선은 뒤로 물러난다.
   * 배지·칩 InstancedMesh 는 CompanyNodes 가 raycast 를 감싸 보이는 원판·캡슐 안의 히트만 내보내므로 pick 표식만 보면 된다
   */
  const nodeAhead = (e: ThreeEvent<PointerEvent | MouseEvent>) => e.intersections.some((i) => i.object.userData?.pick === "node");

  return (
    <group>
      {/* 튜브 본체 */}
      <mesh geometry={built.geo} frustumCulled={false} raycast={() => null} renderOrder={2}>
        <shaderMaterial uniforms={tubeUniforms} vertexShader={TUBE_VERTEX} fragmentShader={TUBE_FRAGMENT} transparent depthWrite={false} toneMapped={false} />
      </mesh>
      {/* 헤일로 — 같은 지오메트리를 부풀려 가산 합성 */}
      <mesh geometry={built.geo} frustumCulled={false} raycast={() => null} renderOrder={1}>
        <shaderMaterial uniforms={haloUniforms} vertexShader={TUBE_VERTEX} fragmentShader={TUBE_FRAGMENT} transparent depthWrite={false} blending={THREE.AdditiveBlending} toneMapped={false} />
      </mesh>
      {/* 빛 구슬 */}
      <points geometry={built.pulseGeo} frustumCulled={false} raycast={() => null} renderOrder={3}>
        <shaderMaterial uniforms={pulseUniforms} transparent depthWrite={false} blending={THREE.AdditiveBlending} toneMapped={false} vertexShader={PULSE_VERTEX} fragmentShader={PULSE_FRAGMENT} />
      </points>
      {/* 피킹 전용 — 보이지 않는 선분 */}
      <lineSegments
        geometry={built.pick}
        frustumCulled={false}
        onPointerMove={(e) => {
          if (revealOf() < PICK_REVEAL) return;
          if (nodeAhead(e)) {
            onHover?.(null);
            return;
          }
          e.stopPropagation();
          const edge = edgeIndexOf(e);
          if (edge && (emphasis.edge.get(edge.id) ?? 1) > 0.003) onHover?.(edge.id);
        }}
        onPointerOut={() => onHover?.(null)}
        onClick={(e) => {
          if (revealOf() < PICK_REVEAL) return;
          if (nodeAhead(e)) return;
          e.stopPropagation();
          const edge = edgeIndexOf(e);
          if (edge && (emphasis.edge.get(edge.id) ?? 1) > 0.003) onSelect?.(edge.id);
        }}
      >
        <lineBasicMaterial transparent opacity={0} depthWrite={false} />
      </lineSegments>
    </group>
  );
}

const TUBE_VERTEX = /* glsl */ `
  attribute float aT;
  attribute float aT2;
  attribute float aScore;
  attribute float aRadius;
  attribute float aFlow;
  attribute float aNeg;
  attribute float aEmph;
  uniform float uInflate;
  varying float vT;
  varying float vT2;
  varying float vScore;
  varying float vFlow;
  varying float vNeg;
  varying float vEmph;
  varying vec3 vNormalV;
  void main(){
    vT = aT; vT2 = aT2; vScore = aScore; vFlow = aFlow; vNeg = aNeg; vEmph = aEmph;
    // 헤일로 패스는 법선 방향으로 부풀린다
    vec3 p = position + normal * aRadius * uInflate;
    vNormalV = normalize(normalMatrix * normal);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
  }
`;
const TUBE_FRAGMENT = /* glsl */ `
  uniform vec3 uColor;
  uniform float uDim;
  uniform float uHalo;
  uniform float uFade;
  uniform float uReveal;
  varying float vT;
  varying float vT2;
  varying float vScore;
  varying float vFlow;
  varying float vNeg;
  varying float vEmph;
  varying vec3 vNormalV;
  void main(){
    if (vEmph <= 0.003 || uFade <= 0.003) discard;
    // 리빌: 점수 높은 빔이 먼저(rv), 각 빔은 원점에 가까운 끝(vT2=0)에서 바깥으로 자란다
    float rv = clamp((uReveal - (1.0 - vScore) * 0.35) / 0.65, 0.0, 1.0);
    float grow = 1.0 - smoothstep(rv - 0.06, rv, vT2);
    if (grow <= 0.003) discard;
    // 무방향 관계는 점선
    float dash = vFlow > 0.5 ? 1.0 : mix(0.3, 1.0, step(0.5, fract(vT * 18.0)));
    // 끝점은 행성에 묻히도록 살짝 페이드
    float ends = smoothstep(0.0, 0.05, vT) * smoothstep(1.0, 0.95, vT);
    vec3 col = mix(uColor, vec3(1.0, 0.42, 0.46), vNeg * 0.55);
    if (uHalo > 0.5) {
      float a = 0.14 * (0.5 + 0.5 * vScore) * vEmph * vEmph * dash * ends * uDim * uFade * grow;
      gl_FragColor = vec4(col, a);
      return;
    }
    // 플랫 컬러 + 살짝의 원통 음영 (툰 느낌)
    float shade = 0.8 + 0.2 * max(0.0, dot(normalize(vNormalV), normalize(vec3(-0.45, 0.75, 0.6))));
    col *= shade;
    // 흐린 간선은 색을 탁하게 하지 않고 투명도만 낮춘다 (갈색 띠 방지)
    col *= mix(0.85, 1.0, vEmph);
    float a = (0.55 + 0.45 * vScore) * vEmph * vEmph * dash * ends * mix(0.55, 1.0, uDim) * uFade * grow;
    gl_FragColor = vec4(col, a);
  }
`;
const PULSE_VERTEX = /* glsl */ `
  attribute float aSize;
  attribute float aAlpha;
  attribute float aNeg;
  uniform float uScale;
  varying float vAlpha;
  varying float vNeg;
  void main(){
    vAlpha = aAlpha; vNeg = aNeg;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = aSize * (uScale / -mv.z);
    gl_Position = projectionMatrix * mv;
  }
`;
const PULSE_FRAGMENT = /* glsl */ `
  uniform float uDim;
  varying float vAlpha;
  varying float vNeg;
  void main(){
    if (vAlpha <= 0.002) discard;
    float d = length(gl_PointCoord - 0.5) * 2.0;
    if (d > 1.0) discard;
    // 밝은 크림색 심 + 주황 글로우
    float core = smoothstep(0.36, 0.18, d);
    float glow = exp(-d * d * 3.2) * 0.55;
    vec3 warm = mix(vec3(1.0, 0.64, 0.3), vec3(1.0, 0.4, 0.45), vNeg);
    vec3 col = mix(warm, vec3(1.0, 0.95, 0.86), core);
    gl_FragColor = vec4(col * (core + glow), (core + glow) * vAlpha * uDim);
  }
`;
