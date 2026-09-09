import { useEffect, useMemo } from "react";
import { useFrame, type ThreeEvent } from "@react-three/fiber";
import * as THREE from "three";
import type { SceneEdge } from "@/lib/graph";
import { bezier, controlPoint, radiusOf } from "./edgeCurve";
import type { Emphasis } from "./emphasis";
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
}

/**
 * 관계 빔 — 아트 디렉션 랩의 튜브 빔을 은하 전체에 적용한 것.
 * - 모든 간선을 3D 튜브 하나(드로우콜 1회)로 병합해 그린다. 굵기는 점수, 색은 주황 한 가지
 * - 같은 지오메트리를 법선 방향으로 부풀린 두 번째 패스가 부드러운 헤일로가 된다
 * - 빛 구슬(펄스)은 포인트 레이어 하나로 그리고, 매 프레임 베지어 위를 따라 움직인다.
 *   방향 있는 관계는 source → target 으로, 무방향 관계는 양방향으로 흐르며 점선 튜브를 쓴다
 * - 강조(emphasis) 값은 지오메트리를 다시 만들지 않고 속성 하나만 갱신한다
 */
export default function RelationLines({ edges, emphasis, onHover, onSelect, intensity = 1 }: Props) {
  const built = useMemo(() => {
    const n = edges.length;
    const ctrls: THREE.Vector3[] = [];
    const radii = new Float32Array(n);
    const speeds = new Float32Array(n);
    const seeds = new Float32Array(n);

    // 튜브 병합 버퍼
    const vPer = (SEGS + 1) * (RADIAL + 1);
    const iPer = SEGS * RADIAL * 6;
    const position = new Float32Array(n * vPer * 3);
    const normal = new Float32Array(n * vPer * 3);
    const aT = new Float32Array(n * vPer);
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
      seeds[ei] = (ei * 0.6180339887) % 1;

      const curve = new THREE.QuadraticBezierCurve3(new THREE.Vector3().fromArray(e.from), ctrl, new THREE.Vector3().fromArray(e.to));
      const tube = new THREE.TubeGeometry(curve, SEGS, r, RADIAL, false);
      const tp = tube.attributes.position.array as Float32Array;
      const tn = tube.attributes.normal.array as Float32Array;
      const tuv = tube.attributes.uv.array as Float32Array;
      const ti = tube.index!.array;
      const vBase = ei * vPer;
      position.set(tp, vBase * 3);
      normal.set(tn, vBase * 3);
      for (let v = 0; v < vPer; v += 1) {
        aT[vBase + v] = tuv[v * 2]; // u = 길이 방향 0~1
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

    return { geo, emphAttr, pick, ctrls, radii, speeds, seeds, pulseGeo, pPos, pSize, pAlpha, vPer };
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

  const tubeUniforms = useMemo(() => ({ uColor: { value: new THREE.Color(LAB.beam) }, uDim: { value: 1 }, uInflate: { value: 0 }, uHalo: { value: 0 } }), []);
  const haloUniforms = useMemo(() => ({ uColor: { value: new THREE.Color(LAB.beamGlow) }, uDim: { value: 1 }, uInflate: { value: 1.7 }, uHalo: { value: 1 } }), []);
  const pulseUniforms = useMemo(() => ({ uDim: { value: 1 }, uScale: { value: 600 } }), []);

  useFrame((state) => {
    const t = state.clock.elapsedTime;
    tubeUniforms.uDim.value = intensity;
    haloUniforms.uDim.value = intensity;
    pulseUniforms.uDim.value = intensity;
    // 포인트 크기를 화면 높이에 맞춘다 (fov 고정 가정)
    pulseUniforms.uScale.value = state.size.height * 0.62;

    const pos = built.pPos.array as Float32Array;
    const size = built.pSize.array as Float32Array;
    const alpha = built.pAlpha.array as Float32Array;
    edges.forEach((e, ei) => {
      const emph = emphasis.edge.get(e.id) ?? 1;
      const ctrl = built.ctrls[ei];
      for (let j = 0; j < PULSES; j += 1) {
        const k = ei * PULSES + j;
        let u = (t * built.speeds[ei] + built.seeds[ei] + j / PULSES) % 1;
        // 무방향 관계는 구슬이 양쪽으로 오간다
        if (!e.directed && j % 2 === 1) u = 1 - u;
        bezier(e.from, e.to, ctrl, u, P);
        pos[k * 3] = P.x;
        pos[k * 3 + 1] = P.y;
        pos[k * 3 + 2] = P.z;
        const fade = Math.sin(u * Math.PI);
        size[k] = built.radii[ei] * (6.0 + fade * 3.6);
        alpha[k] = emph <= 0.003 ? 0 : (0.35 + 0.65 * fade) * Math.min(1, emph * 1.4);
      }
    });
    built.pPos.needsUpdate = true;
    built.pSize.needsUpdate = true;
    built.pAlpha.needsUpdate = true;
  });

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
  attribute float aScore;
  attribute float aRadius;
  attribute float aFlow;
  attribute float aNeg;
  attribute float aEmph;
  uniform float uInflate;
  varying float vT;
  varying float vScore;
  varying float vFlow;
  varying float vNeg;
  varying float vEmph;
  varying vec3 vNormalV;
  void main(){
    vT = aT; vScore = aScore; vFlow = aFlow; vNeg = aNeg; vEmph = aEmph;
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
  varying float vT;
  varying float vScore;
  varying float vFlow;
  varying float vNeg;
  varying float vEmph;
  varying vec3 vNormalV;
  void main(){
    if (vEmph <= 0.003) discard;
    // 무방향 관계는 점선
    float dash = vFlow > 0.5 ? 1.0 : mix(0.3, 1.0, step(0.5, fract(vT * 18.0)));
    // 끝점은 행성에 묻히도록 살짝 페이드
    float ends = smoothstep(0.0, 0.05, vT) * smoothstep(1.0, 0.95, vT);
    vec3 col = mix(uColor, vec3(1.0, 0.42, 0.46), vNeg * 0.55);
    if (uHalo > 0.5) {
      float a = 0.14 * (0.5 + 0.5 * vScore) * vEmph * vEmph * dash * ends * uDim;
      gl_FragColor = vec4(col, a);
      return;
    }
    // 플랫 컬러 + 살짝의 원통 음영 (툰 느낌)
    float shade = 0.8 + 0.2 * max(0.0, dot(normalize(vNormalV), normalize(vec3(-0.45, 0.75, 0.6))));
    col *= shade;
    // 흐린 간선은 색을 탁하게 하지 않고 투명도만 낮춘다 (갈색 띠 방지)
    col *= mix(0.85, 1.0, vEmph);
    float a = (0.55 + 0.45 * vScore) * vEmph * vEmph * dash * ends * mix(0.55, 1.0, uDim);
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
