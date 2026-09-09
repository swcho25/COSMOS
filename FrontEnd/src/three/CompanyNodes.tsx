import { useEffect, useMemo, useRef } from "react";
import { useFrame, type ThreeEvent } from "@react-three/fiber";
import * as THREE from "three";
import type { SceneModel, SceneNode } from "@/lib/graph";
import { initials } from "@/lib/format";
import { logoUrl } from "@/lib/logos";
import type { Emphasis } from "./emphasis";
import { ATLAS_CELL_UV, cellUv, ensureLogoCell, logoAtlasTexture } from "./logoAtlas";
import { discFragment, discVertex } from "./logoDiscShader";
import { dotTexture, ringTexture } from "./textures";
import { planetGeometry, planetPaletteFor, toonGradient, type PlanetPalette } from "./toon";

interface Props {
  model: SceneModel;
  emphasis: Emphasis;
  hoveredId: string | null;
  onHover: (id: string | null) => void;
  onClick: (id: string) => void;
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
const CAM_RIGHT = new THREE.Vector3();
const CAM_UP = new THREE.Vector3();
const TMP = new THREE.Vector3();
/** 로고 이름표가 보이기 시작하는 거리와 완전히 보이는 거리 (월드 단위) */
const TAG_FAR = 150;
const TAG_NEAR = 70;
/** 항성 점광 레이어 — 랩과 동일 */
const LIT = 1;

/**
 * 기업 노드 = 저폴리 툰 행성 (장난감 우주 스타일).
 * - 산업색에서 만든 팔레트로 면 단위 바다·대륙을 칠하고, 대륙형·고리형·위성형을 시드로 섞는다
 * - 크기는 연결 가중치(node.size), 호버·중심·강조로 크기와 밝기를 프레임마다 lerp
 * - 로고 이름표는 행성 곁에 떠 있는 흰 원판(InstancedMesh 1회)으로, 가까이 가면 나타나고 멀어지면 사라진다
 * - 멀리서도 위치가 보이도록 아주 약한 헤일로 점광을 함께 둔다
 */
export default function CompanyNodes({ model, emphasis, hoveredId, onHover, onClick }: Props) {
  const tags = useRef<THREE.InstancedMesh>(null);
  const ring = useRef<THREE.Sprite>(null);
  const groups = useRef<(THREE.Group | null)[]>([]);
  const bodies = useRef<(THREE.Mesh | null)[]>([]);
  const moons = useRef<(THREE.Group | null)[]>([]);
  const n = model.nodes.length;

  const anim = useMemo(() => ({ scale: new Float32Array(n).fill(0.001), bright: new Float32Array(n).fill(1), tagA: new Float32Array(n) }), [n]);

  const planets = useMemo<Planet[]>(
    () =>
      model.nodes.map((node, i) => {
        const h = ((i * 0.6180339887 + 0.37) % 1 + 1) % 1;
        const variant: Variant = h < 0.15 ? "ring" : h < 0.27 ? "moon" : "land";
        const radius = node.size * 1.3;
        const palette = planetPaletteFor(node.color);
        const seed = 0.5 + h * 9;
        const geo = planetGeometry(radius, palette, seed, radius >= 2.0 ? 2 : 1, variant === "ring" ? 0.3 : 0.45);
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

  // 이름표(아틀라스 셀)와 헤일로 버퍼
  const built = useMemo(() => {
    const color = new Float32Array(n * 3);
    const bright = new Float32Array(n).fill(1);
    const emph = new Float32Array(n).fill(1);
    const alpha = new Float32Array(n);
    const cell = new Float32Array(n * 2);
    const pos = new Float32Array(n * 3);
    const size = new Float32Array(n);
    const haloA = new Float32Array(n).fill(1);
    const c = new THREE.Color();
    model.nodes.forEach((node, i) => {
      c.set(node.color);
      color[i * 3] = c.r;
      color[i * 3 + 1] = c.g;
      color[i * 3 + 2] = c.b;
      pos[i * 3] = node.pos[0];
      pos[i * 3 + 1] = node.pos[1];
      pos[i * 3 + 2] = node.pos[2];
      size[i] = node.size;
      const idx = ensureLogoCell(`${node.stockCode}|${node.name}`, initials(node.name), node.color, logoUrl(node.stockCode));
      const [u, v] = cellUv(idx);
      cell[i * 2] = u;
      cell[i * 2 + 1] = v;
    });
    const geo = new THREE.PlaneGeometry(2, 2);
    geo.setAttribute("aColor", new THREE.InstancedBufferAttribute(color, 3));
    geo.setAttribute("aCell", new THREE.InstancedBufferAttribute(cell, 2));
    const brightAttr = new THREE.InstancedBufferAttribute(bright, 1);
    brightAttr.setUsage(THREE.DynamicDrawUsage);
    geo.setAttribute("aBright", brightAttr);
    const emphAttr = new THREE.InstancedBufferAttribute(emph, 1);
    emphAttr.setUsage(THREE.DynamicDrawUsage);
    geo.setAttribute("aEmph", emphAttr);
    const alphaAttr = new THREE.InstancedBufferAttribute(alpha, 1);
    alphaAttr.setUsage(THREE.DynamicDrawUsage);
    geo.setAttribute("aAlpha", alphaAttr);

    const haloGeo = new THREE.BufferGeometry();
    haloGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    haloGeo.setAttribute("color", new THREE.BufferAttribute(color.slice(), 3));
    const haloSize = new THREE.BufferAttribute(size, 1);
    haloSize.setUsage(THREE.DynamicDrawUsage);
    const haloAlpha = new THREE.BufferAttribute(haloA, 1);
    haloAlpha.setUsage(THREE.DynamicDrawUsage);
    haloGeo.setAttribute("aSize", haloSize);
    haloGeo.setAttribute("aAlpha", haloAlpha);
    return { geo, brightAttr, emphAttr, alphaAttr, haloGeo, haloSize, haloAlpha };
  }, [model, n]);
  useEffect(
    () => () => {
      built.geo.dispose();
      built.haloGeo.dispose();
    },
    [built],
  );

  useEffect(() => {
    const m = tags.current;
    if (!m) return;
    m.userData.pick = "node";
    m.userData.disc = true;
    for (let i = 0; i < n; i += 1) {
      tmpObj.position.set(0, 0, 0);
      tmpObj.quaternion.identity();
      tmpObj.scale.setScalar(0.001);
      tmpObj.updateMatrix();
      m.setMatrixAt(i, tmpObj.matrix);
    }
    m.instanceMatrix.needsUpdate = true;
  }, [model, n]);

  const discUniforms = useMemo(() => ({ uAtlas: { value: logoAtlasTexture() }, uCell: { value: ATLAS_CELL_UV } }), []);
  const haloUniforms = useMemo(() => ({ uMap: { value: dotTexture() } }), []);
  const grad = toonGradient(3);

  useFrame((state, dt) => {
    const k = 1 - Math.pow(0.001, dt);
    const t = state.clock.elapsedTime;
    const cam = state.camera;
    CAM_RIGHT.set(1, 0, 0).applyQuaternion(cam.quaternion);
    CAM_UP.set(0, 1, 0).applyQuaternion(cam.quaternion);
    tmpObj.quaternion.copy(cam.quaternion);
    const tagMesh = tags.current;

    planets.forEach((p, i) => {
      const { node } = p;
      const emph = emphasis.node.get(node.id) ?? 1;
      const isHover = node.id === hoveredId;
      const isCenter = node.id === model.centerId;
      const targetScale = (isHover ? 1.18 : 1) * (isCenter ? 1.1 : 1) * (0.7 + 0.3 * emph);
      const targetBright = (isHover ? 1.15 : isCenter ? 1.06 : 1.0) * (0.42 + 0.58 * emph);
      const s = (anim.scale[i] += (targetScale - anim.scale[i]) * k);
      const br = (anim.bright[i] += (targetBright - anim.bright[i]) * k);

      const g = groups.current[i];
      if (g) g.scale.setScalar(s);
      const body = bodies.current[i];
      if (body) {
        body.rotation.y = t * 0.12 + p.seed;
        (body.material as THREE.MeshToonMaterial).color.setScalar(br);
      }
      const moon = moons.current[i];
      if (moon) moon.rotation.y = t * 0.5 + p.seed;

      // 이름표: 카메라 기준 오른쪽 위에 띄우고 거리로 페이드. 호버·중심은 항상 보인다
      const dist = TMP.set(node.pos[0], node.pos[1], node.pos[2]).distanceTo(cam.position);
      let ta = 1 - THREE.MathUtils.smoothstep(dist, TAG_NEAR, TAG_FAR);
      if (isHover || isCenter) ta = 1;
      const a = (anim.tagA[i] += (ta - anim.tagA[i]) * k);
      if (tagMesh) {
        const r = p.radius * s;
        tmpObj.position.set(node.pos[0], node.pos[1], node.pos[2]).addScaledVector(CAM_RIGHT, r * 0.95).addScaledVector(CAM_UP, r * 1.05);
        tmpObj.scale.setScalar(Math.max(0.001, r * 0.72));
        tmpObj.updateMatrix();
        tagMesh.setMatrixAt(i, tmpObj.matrix);
        (built.brightAttr.array as Float32Array)[i] = isHover ? 1.05 : 1;
        (built.emphAttr.array as Float32Array)[i] = emph;
        (built.alphaAttr.array as Float32Array)[i] = a;
      }
      (built.haloSize.array as Float32Array)[i] = p.radius * s * 3.2 * (0.5 + 0.5 * emph);
      (built.haloAlpha.array as Float32Array)[i] = (0.15 + 0.85 * emph) * 0.22;
    });
    if (tagMesh) {
      tagMesh.instanceMatrix.needsUpdate = true;
      built.brightAttr.needsUpdate = true;
      built.emphAttr.needsUpdate = true;
      built.alphaAttr.needsUpdate = true;
    }
    built.haloSize.needsUpdate = true;
    built.haloAlpha.needsUpdate = true;

    const r = ring.current;
    if (r) {
      const target = hoveredId ? model.nodeById.get(hoveredId) : undefined;
      if (target) {
        const i = model.nodes.indexOf(target);
        r.visible = true;
        r.position.set(target.pos[0], target.pos[1], target.pos[2]);
        r.scale.setScalar(planets[i].radius * anim.scale[i] * 3.0);
        const mat = r.material as THREE.SpriteMaterial;
        mat.opacity = 0.35 + Math.sin(t * 3) * 0.08;
        mat.color.set(target.color);
        mat.rotation = t * 0.4;
      } else r.visible = false;
    }
  });

  const pickable = (id: string) => (emphasis.node.get(id) ?? 1) > 0.05;
  const tagIdOf = (e: ThreeEvent<PointerEvent | MouseEvent>) => {
    if (e.instanceId === undefined) return null;
    if (e.uv && Math.hypot(e.uv.x - 0.5, e.uv.y - 0.5) > 0.5) return null;
    return model.nodes[e.instanceId]?.id ?? null;
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
            onPointerOut={() => onHover(null)}
            onClick={(e) => {
              if (!pickable(p.node.id)) return;
              e.stopPropagation();
              onClick(p.node.id);
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

      {/* 로고 이름표 — 드로우콜 1회 */}
      <instancedMesh
        ref={tags}
        args={[built.geo, undefined, n]}
        frustumCulled={false}
        renderOrder={3}
        onPointerMove={(e) => {
          const id = tagIdOf(e);
          if (!id || !pickable(id)) return;
          e.stopPropagation();
          onHover(id);
        }}
        onPointerOut={() => onHover(null)}
        onClick={(e) => {
          const id = tagIdOf(e);
          if (!id || !pickable(id)) return;
          e.stopPropagation();
          onClick(id);
        }}
      >
        <shaderMaterial vertexShader={discVertex} fragmentShader={discFragment} uniforms={discUniforms} transparent depthWrite={false} toneMapped={false} />
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
