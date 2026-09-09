import { useEffect, useMemo, useRef } from "react";
import { useFrame, useThree, type ThreeEvent } from "@react-three/fiber";
import * as THREE from "three";
import type { SceneEdge, Vec3 } from "@/lib/graph";
import type { Emphasis } from "./emphasis";
import { LAB } from "./toon";

const SEGS = 24;
const PICK_SEGS = 8;
const BOW = 0.16;

const A = new THREE.Vector3();
const B = new THREE.Vector3();
const OUT = new THREE.Vector3();

/** 두 노드를 잇는 2차 베지어 아크의 제어점 — 원점 바깥으로 살짝 부풀린다 */
function controlPoint(from: Vec3, to: Vec3, out: THREE.Vector3) {
  A.fromArray(from);
  B.fromArray(to);
  const dist = A.distanceTo(B);
  out.addVectors(A, B).multiplyScalar(0.5);
  OUT.copy(out);
  if (OUT.lengthSq() < 1e-6) OUT.set(0, 1, 0);
  OUT.normalize().multiplyScalar(dist * BOW);
  OUT.y += dist * BOW * 0.35;
  out.add(OUT);
  return dist;
}
function bezier(from: Vec3, to: Vec3, ctrl: THREE.Vector3, t: number, out: THREE.Vector3) {
  const it = 1 - t;
  out.set(0, 0, 0);
  out.addScaledVector(A.fromArray(from), it * it);
  out.addScaledVector(ctrl, 2 * it * t);
  out.addScaledVector(B.fromArray(to), t * t);
  return out;
}

interface Props {
  edges: SceneEdge[];
  emphasis: Emphasis;
  onHover?: (id: string | null) => void;
  onSelect?: (id: string) => void;
  /** 전체 밝기·폭 배율 (은하 뷰처럼 간선이 많을 때 낮춰 노드·성단이 묻히지 않게) */
  intensity?: number;
}

/**
 * 관계 간선 전체를 메시 하나로 그린다.
 * - 2차 베지어 아크를 카메라를 향해 빌보드된 리본으로 확장 → 화면 기준 두께 (점수에 비례)
 * - 프래그먼트에서 간선을 따라 흐르는 펄스 → 방향(공급·투자)과 생동감. 무방향 관계는 양쪽에서 흐른다.
 * - 강조(emphasis) 값은 지오메트리를 다시 만들지 않고 속성 하나만 갱신한다.
 */
export default function RelationLines({ edges, emphasis, onHover, onSelect, intensity = 1 }: Props) {
  const mat = useRef<THREE.ShaderMaterial>(null);
  const size = useThree((s) => s.size);
  const camera = useThree((s) => s.camera);

  const { geometry, emphAttr, pick } = useMemo(() => {
    const n = edges.length;
    const pts = SEGS + 1;
    const vCount = n * pts * 2;
    const position = new Float32Array(vCount * 3);
    const aDir = new Float32Array(vCount * 3);
    const aColor = new Float32Array(vCount * 3);
    const aT = new Float32Array(vCount);
    const aSide = new Float32Array(vCount);
    const aSeed = new Float32Array(vCount);
    const aWidth = new Float32Array(vCount);
    const aAlpha = new Float32Array(vCount);
    const aSpeed = new Float32Array(vCount);
    const aFlow = new Float32Array(vCount);
    const aNeg = new Float32Array(vCount);
    const aEmph = new Float32Array(vCount).fill(1);
    const index = new Uint32Array(n * SEGS * 6);
    const pickPos = new Float32Array(n * PICK_SEGS * 2 * 3);

    const col = new THREE.Color();
    const ctrl = new THREE.Vector3();
    const p = new THREE.Vector3();
    const prev = new THREE.Vector3();
    const next = new THREE.Vector3();
    let vi = 0;
    let ii = 0;

    edges.forEach((e, ei) => {
      controlPoint(e.from, e.to, ctrl);
      // 관계 빔은 한 가지 주황 — 유형은 색이 아니라 흐름(방향)·점선(무방향)으로 구분한다
      col.set(LAB.beam);
      const s = e.score / 100;
      const seed = (ei * 0.6180339887) % 1;
      const width = 1.1 + s * s * 4.2; // px
      const alpha = 0.16 + s * s * 0.7;
      const speed = 0.06 + s * 0.2;
      const base = vi;
      for (let k = 0; k <= SEGS; k += 1) {
        const t = k / SEGS;
        bezier(e.from, e.to, ctrl, t, p);
        bezier(e.from, e.to, ctrl, Math.max(0, t - 1 / SEGS), prev);
        bezier(e.from, e.to, ctrl, Math.min(1, t + 1 / SEGS), next);
        const dx = next.x - prev.x;
        const dy = next.y - prev.y;
        const dz = next.z - prev.z;
        const len = Math.hypot(dx, dy, dz) || 1;
        for (let side = 0; side < 2; side += 1) {
          position[vi * 3] = p.x;
          position[vi * 3 + 1] = p.y;
          position[vi * 3 + 2] = p.z;
          aDir[vi * 3] = dx / len;
          aDir[vi * 3 + 1] = dy / len;
          aDir[vi * 3 + 2] = dz / len;
          aColor[vi * 3] = col.r;
          aColor[vi * 3 + 1] = col.g;
          aColor[vi * 3 + 2] = col.b;
          aT[vi] = t;
          aSide[vi] = side === 0 ? 1 : -1;
          aSeed[vi] = seed;
          aWidth[vi] = width;
          aAlpha[vi] = alpha;
          aSpeed[vi] = speed;
          aFlow[vi] = e.directed ? 1 : 0;
          aNeg[vi] = e.impact === "NEGATIVE" ? 1 : 0;
          vi += 1;
        }
      }
      for (let k = 0; k < SEGS; k += 1) {
        const a = base + k * 2;
        index[ii++] = a;
        index[ii++] = a + 1;
        index[ii++] = a + 2;
        index[ii++] = a + 1;
        index[ii++] = a + 3;
        index[ii++] = a + 2;
      }
      // 피킹용 폴리라인
      for (let k = 0; k < PICK_SEGS; k += 1) {
        const o = (ei * PICK_SEGS + k) * 6;
        bezier(e.from, e.to, ctrl, k / PICK_SEGS, p);
        pickPos[o] = p.x;
        pickPos[o + 1] = p.y;
        pickPos[o + 2] = p.z;
        bezier(e.from, e.to, ctrl, (k + 1) / PICK_SEGS, p);
        pickPos[o + 3] = p.x;
        pickPos[o + 4] = p.y;
        pickPos[o + 5] = p.z;
      }
    });

    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(position, 3));
    g.setAttribute("aDir", new THREE.BufferAttribute(aDir, 3));
    g.setAttribute("aColor", new THREE.BufferAttribute(aColor, 3));
    g.setAttribute("aT", new THREE.BufferAttribute(aT, 1));
    g.setAttribute("aSide", new THREE.BufferAttribute(aSide, 1));
    g.setAttribute("aSeed", new THREE.BufferAttribute(aSeed, 1));
    g.setAttribute("aWidth", new THREE.BufferAttribute(aWidth, 1));
    g.setAttribute("aAlpha", new THREE.BufferAttribute(aAlpha, 1));
    g.setAttribute("aSpeed", new THREE.BufferAttribute(aSpeed, 1));
    g.setAttribute("aFlow", new THREE.BufferAttribute(aFlow, 1));
    g.setAttribute("aNeg", new THREE.BufferAttribute(aNeg, 1));
    const emphAttr = new THREE.BufferAttribute(aEmph, 1);
    emphAttr.setUsage(THREE.DynamicDrawUsage);
    g.setAttribute("aEmph", emphAttr);
    g.setIndex(new THREE.BufferAttribute(index, 1));
    g.computeBoundingSphere();

    const pick = new THREE.BufferGeometry();
    pick.setAttribute("position", new THREE.BufferAttribute(pickPos, 3));
    return { geometry: g, emphAttr, pick };
  }, [edges]);

  useEffect(() => () => {
    geometry.dispose();
    pick.dispose();
  }, [geometry, pick]);

  // 강조 값만 갱신 (지오메트리 재생성 없음)
  useEffect(() => {
    const arr = emphAttr.array as Float32Array;
    const pts = (SEGS + 1) * 2;
    edges.forEach((e, ei) => {
      const v = emphasis.edge.get(e.id) ?? 1;
      arr.fill(v, ei * pts, (ei + 1) * pts);
    });
    emphAttr.needsUpdate = true;
  }, [edges, emphasis, emphAttr]);

  const uniforms = useMemo(() => ({ uTime: { value: 0 }, uPixel: { value: 0.002 }, uDim: { value: 1 } }), []);

  useFrame((state) => {
    if (!mat.current) return;
    mat.current.uniforms.uTime.value = state.clock.elapsedTime;
    mat.current.uniforms.uDim.value = intensity;
    const cam = camera as THREE.PerspectiveCamera;
    const fov = (cam.fov * Math.PI) / 180;
    mat.current.uniforms.uPixel.value = (2 * Math.tan(fov / 2)) / Math.max(1, size.height);
  });

  const edgeIndexOf = (e: ThreeEvent<PointerEvent | MouseEvent>) => {
    const idx = e.index ?? 0;
    const ei = Math.floor(idx / (PICK_SEGS * 2));
    return edges[ei];
  };
  /** 같은 광선에 기업 노드가 걸려 있으면 간선은 뒤로 물러난다 — 간선이 촘촘해도 기업을 먼저 고를 수 있게 */
  // 노드(행성 또는 이름표 원판 안쪽)가 앞에 있으면 간선은 양보한다 — 이름표 쿼드의 모서리는 노드로 치지 않는다
  const nodeAhead = (e: ThreeEvent<PointerEvent | MouseEvent>) =>
    e.intersections.some((i) => i.object.userData?.pick === "node" && (!i.object.userData?.disc || !i.uv || Math.hypot(i.uv.x - 0.5, i.uv.y - 0.5) <= 0.5));

  return (
    <group>
      <mesh geometry={geometry} frustumCulled={false} raycast={() => null} renderOrder={2}>
        <shaderMaterial
          ref={mat}
          uniforms={uniforms}
          transparent
          depthWrite={false}
          side={THREE.DoubleSide}
          blending={THREE.NormalBlending}
          toneMapped={false}
          vertexShader={/* glsl */ `
            attribute vec3 aDir;
            attribute vec3 aColor;
            attribute float aT;
            attribute float aSide;
            attribute float aSeed;
            attribute float aWidth;
            attribute float aAlpha;
            attribute float aSpeed;
            attribute float aFlow;
            attribute float aNeg;
            attribute float aEmph;
            uniform float uPixel;
            uniform float uDim;
            varying vec3 vColor;
            varying float vT;
            varying float vSide;
            varying float vSeed;
            varying float vAlpha;
            varying float vSpeed;
            varying float vFlow;
            varying float vNeg;
            varying float vEmph;
            void main(){
              vColor = aColor; vT = aT; vSide = aSide; vSeed = aSeed; vAlpha = aAlpha;
              vSpeed = aSpeed; vFlow = aFlow; vNeg = aNeg; vEmph = aEmph;
              vec4 mv = modelViewMatrix * vec4(position, 1.0);
              vec3 tan = normalize((modelViewMatrix * vec4(aDir, 0.0)).xyz);
              vec3 viewDir = normalize(-mv.xyz);
              vec3 side = cross(tan, viewDir);
              float l = length(side);
              side = l > 0.0001 ? side / l : vec3(1.0, 0.0, 0.0);
              float w = aWidth * mix(0.75, 1.7, aEmph) * mix(0.55, 1.0, uDim);
              mv.xyz += side * aSide * w * 0.5 * uPixel * (-mv.z);
              gl_Position = projectionMatrix * mv;
            }
          `}
          fragmentShader={/* glsl */ `
            uniform float uTime;
            uniform float uDim;
            varying vec3 vColor;
            varying float vT;
            varying float vSide;
            varying float vSeed;
            varying float vAlpha;
            varying float vSpeed;
            varying float vFlow;
            varying float vNeg;
            varying float vEmph;
            void main(){
              if (vEmph <= 0.003) discard;
              float edge = 1.0 - abs(vSide);
              float soft = smoothstep(0.0, 0.6, edge) * 0.6 + 0.4;
              float head = fract(uTime * vSpeed + vSeed);
              float d1 = abs(fract(vT - head + 0.5) - 0.5);
              float d2 = abs(fract((1.0 - vT) - head + 0.5) - 0.5);
              float pulse = vFlow > 0.5 ? exp(-d1 * d1 * 170.0) : max(exp(-d1 * d1 * 260.0), exp(-d2 * d2 * 260.0)) * 0.85;
              // 펄스는 크림색, 부정 영향은 붉게
              vec3 hot = mix(vec3(1.0, 0.95, 0.86), vec3(1.0, 0.42, 0.46), vNeg * 0.8);
              vec3 col = mix(vColor, hot, pulse);
              col *= mix(0.45, 1.0, vEmph);
              // 무방향 관계는 점선 — 흐름 없이도 방향이 없다는 것이 읽힌다
              float dash = vFlow > 0.5 ? 1.0 : mix(0.45, 1.0, step(0.5, fract(vT * 16.0)));
              // 끝점은 노드에 묻히도록 살짝 페이드
              float ends = smoothstep(0.0, 0.06, vT) * smoothstep(1.0, 0.94, vT);
              float a = vAlpha * vEmph * soft * dash * (0.75 + 0.25 * pulse) * (0.7 + 0.3 * ends) * uDim;
              gl_FragColor = vec4(col, a);
            }
          `}
        />
      </mesh>
      {/* 피킹 전용 — 보이지 않는 선분 */}
      <lineSegments
        geometry={pick}
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
