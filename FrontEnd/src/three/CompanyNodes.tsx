import { useEffect, useMemo, useRef } from "react";
import { useFrame, type ThreeEvent } from "@react-three/fiber";
import * as THREE from "three";
import type { SceneModel } from "@/lib/graph";
import { initials } from "@/lib/format";
import { logoUrl } from "@/lib/logos";
import type { Emphasis } from "./emphasis";
import { ATLAS_CELL_UV, cellUv, ensureLogoCell, logoAtlasTexture } from "./logoAtlas";
import { discFragment, discVertex } from "./logoDiscShader";
import { dotTexture, ringTexture } from "./textures";

interface Props {
  model: SceneModel;
  emphasis: Emphasis;
  hoveredId: string | null;
  onHover: (id: string | null) => void;
  onClick: (id: string) => void;
}

const tmpObj = new THREE.Object3D();

/**
 * 기업 노드 = 카메라를 바라보는 원형 로고 디스크.
 * - InstancedMesh 하나(드로우콜 1회)로 모든 노드를 그리고, 로고는 아틀라스에서 읽는다
 * - 인스턴스 행렬을 매 프레임 카메라 회전으로 맞춰 빌보드로 만든다 (레이캐스트도 같은 행렬을 쓴다)
 * - 호버/중심 시 크기·밝기를 프레임마다 목표값으로 lerp
 * - 멀리서도 위치가 보이도록 약한 헤일로 점광을 함께 둔다
 */
export default function CompanyNodes({ model, emphasis, hoveredId, onHover, onClick }: Props) {
  const mesh = useRef<THREE.InstancedMesh>(null);
  const ring = useRef<THREE.Sprite>(null);
  const n = model.nodes.length;

  const anim = useMemo(() => ({ scale: new Float32Array(n).fill(0.001), bright: new Float32Array(n).fill(1) }), [n]);

  const built = useMemo(() => {
    const color = new Float32Array(n * 3);
    const bright = new Float32Array(n).fill(1);
    const emph = new Float32Array(n).fill(1);
    const cell = new Float32Array(n * 2);
    const pos = new Float32Array(n * 3);
    const size = new Float32Array(n);
    const alpha = new Float32Array(n).fill(1);
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
    geo.setAttribute("aCell", new THREE.InstancedBufferAttribute(cell, 2));
    const brightAttr = new THREE.InstancedBufferAttribute(bright, 1);
    brightAttr.setUsage(THREE.DynamicDrawUsage);
    geo.setAttribute("aBright", brightAttr);
    const emphAttr = new THREE.InstancedBufferAttribute(emph, 1);
    emphAttr.setUsage(THREE.DynamicDrawUsage);
    geo.setAttribute("aEmph", emphAttr);

    const haloGeo = new THREE.BufferGeometry();
    haloGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    haloGeo.setAttribute("color", new THREE.BufferAttribute(color, 3));
    const haloSize = new THREE.BufferAttribute(size, 1);
    haloSize.setUsage(THREE.DynamicDrawUsage);
    const haloAlpha = new THREE.BufferAttribute(alpha, 1);
    haloAlpha.setUsage(THREE.DynamicDrawUsage);
    haloGeo.setAttribute("aSize", haloSize);
    haloGeo.setAttribute("aAlpha", haloAlpha);
    return { geo, brightAttr, emphAttr, haloGeo, haloSize, haloAlpha };
  }, [model, n]);

  useEffect(
    () => () => {
      built.geo.dispose();
      built.haloGeo.dispose();
    },
    [built],
  );

  useEffect(() => {
    const m = mesh.current;
    if (!m) return;
    m.userData.pick = "node";
    model.nodes.forEach((node, i) => {
      tmpObj.position.set(node.pos[0], node.pos[1], node.pos[2]);
      tmpObj.quaternion.identity();
      tmpObj.scale.setScalar(0.001);
      tmpObj.updateMatrix();
      m.setMatrixAt(i, tmpObj.matrix);
    });
    m.instanceMatrix.needsUpdate = true;
    m.computeBoundingSphere();
  }, [model]);

  const discUniforms = useMemo(() => ({ uAtlas: { value: logoAtlasTexture() }, uCell: { value: ATLAS_CELL_UV } }), []);
  const haloUniforms = useMemo(() => ({ uMap: { value: dotTexture() } }), []);

  useFrame((state, dt) => {
    const m = mesh.current;
    if (!m) return;
    const k = 1 - Math.pow(0.001, dt);
    const t = state.clock.elapsedTime;
    // 모든 디스크를 카메라 회전에 맞춘다 — 화면 어디서나 정면 원으로 보인다
    tmpObj.quaternion.copy(state.camera.quaternion);

    model.nodes.forEach((node, i) => {
      const emph = emphasis.node.get(node.id) ?? 1;
      const isHover = node.id === hoveredId;
      const isCenter = node.id === model.centerId;
      const targetScale = node.size * 1.3 * (isHover ? 1.18 : 1) * (isCenter ? 1.1 : 1) * (0.6 + 0.4 * emph);
      const targetBright = (isHover ? 1.12 : isCenter ? 1.04 : 1.0) * (0.4 + 0.6 * emph);
      const s = (anim.scale[i] += (targetScale - anim.scale[i]) * k);
      const br = (anim.bright[i] += (targetBright - anim.bright[i]) * k);
      tmpObj.position.set(node.pos[0], node.pos[1], node.pos[2]);
      tmpObj.scale.setScalar(s);
      tmpObj.updateMatrix();
      m.setMatrixAt(i, tmpObj.matrix);
      (built.brightAttr.array as Float32Array)[i] = br;
      (built.emphAttr.array as Float32Array)[i] = emph;
      (built.haloSize.array as Float32Array)[i] = s * 3.6 * (0.5 + 0.5 * emph);
      (built.haloAlpha.array as Float32Array)[i] = (0.15 + 0.85 * emph) * 0.32;
    });
    m.instanceMatrix.needsUpdate = true;
    built.brightAttr.needsUpdate = true;
    built.emphAttr.needsUpdate = true;
    built.haloSize.needsUpdate = true;
    built.haloAlpha.needsUpdate = true;

    const r = ring.current;
    if (r) {
      const target = hoveredId ? model.nodeById.get(hoveredId) : undefined;
      if (target) {
        const i = model.nodes.indexOf(target);
        r.visible = true;
        r.position.set(target.pos[0], target.pos[1], target.pos[2]);
        r.scale.setScalar(anim.scale[i] * 2.5);
        const mat = r.material as THREE.SpriteMaterial;
        mat.opacity = 0.3 + Math.sin(t * 3) * 0.08;
        mat.color.set(target.color);
        mat.rotation = t * 0.4;
      } else r.visible = false;
    }
  });

  /** 사각 쿼드 중 원 안쪽을 맞췼을 때만 해당 노드 id 를 돌려준다 */
  const idOf = (e: ThreeEvent<PointerEvent | MouseEvent>) => {
    if (e.instanceId === undefined) return null;
    if (e.uv && Math.hypot(e.uv.x - 0.5, e.uv.y - 0.5) > 0.5) return null;
    return model.nodes[e.instanceId]?.id ?? null;
  };

  return (
    <group>
      <instancedMesh
        ref={mesh}
        args={[built.geo, undefined, n]}
        frustumCulled={false}
        onPointerMove={(e) => {
          const id = idOf(e);
          if (!id) return;
          e.stopPropagation();
          if ((emphasis.node.get(id) ?? 1) > 0.05) onHover(id);
        }}
        onPointerOut={() => onHover(null)}
        onClick={(e) => {
          const id = idOf(e);
          if (!id) return;
          e.stopPropagation();
          onClick(id);
        }}
      >
        <shaderMaterial vertexShader={discVertex} fragmentShader={discFragment} uniforms={discUniforms} transparent toneMapped={false} />
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
