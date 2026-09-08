import { useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import type { SceneModel } from "@/lib/graph";
import type { Emphasis } from "./emphasis";
import { labelTexture } from "./textures";

interface Props {
  model: SceneModel;
  emphasis: Emphasis;
  visible: boolean;
}

/**
 * 기업명 라벨. 한글 글리프를 위해 캔버스 텍스처 스프라이트를 쓴다.
 * 혼잡을 줄이기 위해 연결이 적은 기업은 카메라가 가까워질 때만, 강조 상태에서는 항상 보인다.
 */
export default function Labels({ model, emphasis, visible }: Props) {
  const refs = useRef<(THREE.Sprite | null)[]>([]);
  const cam = useMemo(() => new THREE.Vector3(), []);

  const entries = useMemo(
    () =>
      model.nodes.map((n) => {
        const tex = labelTexture(n.name, n.depth === 0 && model.kind === "system" ? "#FFFFFF" : "#E4EBF7", n.depth === 0 && model.kind === "system" ? 700 : 600);
        const h = 1.55 + n.size * 0.32;
        return { node: n, tex, h, w: h * tex.aspect, important: model.kind === "system" ? n.depth <= 1 : n.size >= 1.75 || n.degree >= 5 };
      }),
    [model],
  );

  useFrame((state) => {
    cam.copy(state.camera.position);
    entries.forEach((e, i) => {
      const s = refs.current[i];
      if (!s) return;
      const emph = emphasis.node.get(e.node.id) ?? 1;
      const pinned = emphasis.pinned.has(e.node.id);
      const dist = cam.distanceTo(s.position);
      const near = THREE.MathUtils.clamp((110 - dist) / 60, 0, 1);
      let target = !visible ? 0 : pinned ? 1 : e.important ? 0.9 : near * 0.9;
      target *= 0.15 + 0.85 * emph;
      const mat = s.material as THREE.SpriteMaterial;
      mat.opacity += (target - mat.opacity) * 0.12;
      s.visible = mat.opacity > 0.02;
    });
  });

  return (
    <group>
      {entries.map((e, i) => (
        <sprite
          key={e.node.id}
          ref={(el) => {
            refs.current[i] = el;
          }}
          position={[e.node.pos[0], e.node.pos[1] + e.node.size * 2.6 + 1.0, e.node.pos[2]]}
          scale={[e.w, e.h, 1]}
          raycast={() => null}
          renderOrder={4}
        >
          <spriteMaterial map={e.tex.tex} transparent opacity={0} depthWrite={false} depthTest={false} toneMapped={false} />
        </sprite>
      ))}
    </group>
  );
}
