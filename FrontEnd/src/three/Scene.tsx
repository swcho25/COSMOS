import { useEffect, useMemo, useRef, type MutableRefObject } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { AdaptiveDpr, Html, OrbitControls } from "@react-three/drei";
import { Bloom, ChromaticAberration, EffectComposer, Vignette } from "@react-three/postprocessing";
import * as THREE from "three";
import type { SceneModel } from "@/lib/graph";
import { useGalaxy } from "@/store/galaxy";
import Backdrop from "./Backdrop";
import CompanyNodes from "./CompanyNodes";
import FlyControls from "./FlyControls";
import Labels from "./Labels";
import RelationLines from "./RelationLines";
import Warp from "./Warp";
import { cameraBusy } from "./cameraState";
import { computeEmphasis } from "./emphasis";
import { LAB } from "./toon";

type Pose = { pos: [number, number, number]; tgt: [number, number, number] };
const FALLBACK_POSE: Record<"galaxy" | "system", Pose> = {
  galaxy: { pos: [0, 90, 230], tgt: [0, 0, 0] },
  system: { pos: [0, 40, 118], tgt: [0, -2, 0] },
};
/** 모델의 바깥 반지름이 화면에 들어오도록 카메라 자세를 만든다 (fov 52 기준) */
function poseFor(model: SceneModel | null, kind: "galaxy" | "system", aspect = 1.6): Pose {
  if (!model || !model.nodes.length) return FALLBACK_POSE[kind];
  let r = 10;
  model.nodes.forEach((n) => {
    // 은하는 입체(구), 기업 중심 뷰는 바닥 궤도(x·z) 기준으로 바깥 반지름을 잡는다
    r = Math.max(r, (kind === "galaxy" ? Math.hypot(n.pos[0], n.pos[1], n.pos[2]) : Math.hypot(n.pos[0], n.pos[2])) + n.size * 2);
  });
  // 세로로 긴 화면은 가로가 먼저 잘리므로 종횡비만큼 물러난다
  const fit = Math.tan((52 * Math.PI) / 360) * Math.min(1, aspect);
  const dist = (r / fit) * (kind === "galaxy" ? 0.98 : 0.82);
  // 은하는 살짝 위에서 비스듬히 보아 앞뒤 성단이 겹겹이 보이게 한다
  const elev = kind === "galaxy" ? 0.3 : 0.34;
  return { pos: [0, dist * elev, dist * Math.sqrt(1 - elev * elev)], tgt: [0, kind === "galaxy" ? 0 : -2, 0] };
}
const WARP_COMMIT = 0.6;
const WARP_END = 1.45;
const easeOutCubic = (x: number) => 1 - Math.pow(1 - x, 3);
const easeInQuad = (x: number) => x * x;
const smoothstep = (a: number, b: number, x: number) => {
  const t = THREE.MathUtils.clamp((x - a) / (b - a), 0, 1);
  return t * t * (3 - 2 * t);
};

interface Anim {
  t: number;
  dur: number;
  fromPos: THREE.Vector3;
  fromTgt: THREE.Vector3;
  toPos: THREE.Vector3;
  toTgt: THREE.Vector3;
}

/** 카메라 연출 + 워프 타이밍. 스토어는 getState 로 읽어 리렌더를 만들지 않는다. */
function Director({ warpRef, galaxy, system }: { warpRef: MutableRefObject<number>; galaxy: SceneModel | null; system: SceneModel | null }) {
  const camera = useThree((s) => s.camera);
  const controls = useThree((s) => s.controls) as unknown as { target: THREE.Vector3; update: () => void; enabled: boolean } | null;
  const rig = useRef({
    lastPhase: "" as string,
    anim: null as Anim | null,
    warpDir: new THREE.Vector3(),
    warpAim: new THREE.Vector3(),
    speed: 0,
  }).current;

  const startAnim = (to: Pose, dur: number, fromPos?: THREE.Vector3) => {
    const tgt = controls ? controls.target : new THREE.Vector3();
    rig.anim = {
      t: 0,
      dur,
      fromPos: (fromPos ?? camera.position).clone(),
      fromTgt: tgt.clone(),
      toPos: new THREE.Vector3().fromArray(to.pos),
      toTgt: new THREE.Vector3().fromArray(to.tgt),
    };
    if (fromPos) camera.position.copy(fromPos);
  };

  // 뷰가 바뀌지 않았는데 모델(산업 필터·기업 그래프 로딩)이 갱신되면 카메라를 다시 맞춘다
  const fitted = useRef<SceneModel | null>(null);
  // 은하 모델이 처음 준비되면 바깥에서 안으로 날아드는 인트로
  const introDone = useRef(false);
  useEffect(() => {
    if (introDone.current || !galaxy) return;
    introDone.current = true;
    fitted.current = galaxy;
    const pose = poseFor(galaxy, "galaxy", (camera as THREE.PerspectiveCamera).aspect);
    camera.position.set(...pose.pos).multiplyScalar(1.7);
    startAnim(pose, 2.4);
    rig.lastPhase = "galaxy";
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [galaxy, camera]);

  useFrame((_, delta) => {
    const dt = Math.min(delta, 0.05);
    const s = useGalaxy.getState();
    const now = performance.now();
    let intensity = 0;

    if (s.phase === "warp") {
      const el = (now - s.warpStartedAt) / 1000;
      intensity = smoothstep(0, 0.22, el) * (1 - smoothstep(WARP_END - 0.6, WARP_END, el));
      if (el < WARP_COMMIT) {
        if (rig.speed === 0) {
          const aim = s.warpTarget && s.focusId === null ? galaxy?.nodeById.get(s.warpTarget)?.pos : null;
          if (aim) rig.warpAim.fromArray(aim);
          else rig.warpAim.set(0, 0, 0);
          rig.warpDir.copy(rig.warpAim).sub(camera.position).normalize();
        }
        rig.speed = THREE.MathUtils.lerp(rig.speed, 150, dt * 3.2);
        camera.position.addScaledVector(rig.warpDir, rig.speed * dt * easeInQuad(el / WARP_COMMIT));
        if (controls) {
          controls.target.lerp(rig.warpAim, dt * 3);
          controls.update();
        }
      } else if (!s.warpCommitted) {
        s.commitWarp();
        const aspect = (camera as THREE.PerspectiveCamera).aspect;
        const pose = s.warpTarget ? poseFor(system, "system", aspect) : poseFor(galaxy, "galaxy", aspect);
        fitted.current = s.warpTarget ? system : galaxy;
        const from = new THREE.Vector3().fromArray(pose.pos).multiplyScalar(1.7);
        if (controls) controls.target.fromArray(pose.tgt);
        startAnim(pose, WARP_END - WARP_COMMIT - 0.05, from);
        rig.speed = 0;
        rig.lastPhase = "warp-committed";
      }
      if (el >= WARP_END) {
        s.completeWarp();
        rig.lastPhase = s.warpTarget ? "system" : "galaxy";
      }
    } else {
      rig.speed = 0;
      if (rig.lastPhase !== s.phase && rig.lastPhase !== "warp-committed" && s.navMode !== "fly") {
        startAnim(s.phase === "system" ? poseFor(system, "system", (camera as THREE.PerspectiveCamera).aspect) : poseFor(galaxy, "galaxy", (camera as THREE.PerspectiveCamera).aspect), 1.4);
      }
      if (rig.lastPhase !== s.phase) rig.lastPhase = s.phase;
      // 워프 중에 그래프가 도착했거나 필터로 모델이 바뀐 경우: 진행 중인 애니메이션이 끝난 뒤 현재 모델에 다시 맞춘다
      const model = s.phase === "system" ? system : galaxy;
      if (model && fitted.current !== model && !rig.anim && introDone.current && s.navMode !== "fly") {
        fitted.current = model;
        startAnim(poseFor(model, s.phase === "system" ? "system" : "galaxy", (camera as THREE.PerspectiveCamera).aspect), 1.0);
      }
    }
    warpRef.current = intensity;

    if (rig.anim) {
      const a = rig.anim;
      a.t = Math.min(a.dur, a.t + dt);
      const k = easeOutCubic(a.t / a.dur);
      camera.position.lerpVectors(a.fromPos, a.toPos, k);
      if (controls) {
        controls.target.lerpVectors(a.fromTgt, a.toTgt, k);
        controls.update();
      }
      if (a.t >= a.dur) rig.anim = null;
    }

    cameraBusy.anim = !!rig.anim || s.phase === "warp";
    if (controls) {
      const free = s.phase !== "warp" && !rig.anim && s.navMode !== "fly";
      if (controls.enabled !== free) controls.enabled = free;
    }
  });
  return null;
}

function Effects({ warpRef }: { warpRef: MutableRefObject<number> }) {
  const ca = useRef<{ offset: THREE.Vector2 } | null>(null);
  const offset = useMemo(() => new THREE.Vector2(0.0005, 0.0003), []);
  useFrame(() => {
    if (!ca.current) return;
    const k = warpRef.current;
    ca.current.offset.set(0.0004 + k * 0.003, 0.0003 + k * 0.002);
  });
  return (
    <EffectComposer multisampling={0}>
      <Bloom mipmapBlur intensity={0.35} luminanceThreshold={0.88} luminanceSmoothing={0.2} radius={0.5} />
      {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
      <ChromaticAberration ref={ca as any} offset={offset} radialModulation modulationOffset={0.35} />
      <Vignette eskil={false} offset={0.22} darkness={0.82} />
    </EffectComposer>
  );
}

export interface Callout {
  companyId: string;
  kicker: string;
  title: string;
  meta: string;
  onClose?: () => void;
}

interface ContentProps {
  galaxy: SceneModel | null;
  system: SceneModel | null;
  callout: Callout | null;
  warpRef: MutableRefObject<number>;
}

function Content({ galaxy, system, callout, warpRef }: ContentProps) {
  const focusId = useGalaxy((s) => s.focusId);
  const hoveredId = useGalaxy((s) => s.hoveredId);
  const hoveredEdgeId = useGalaxy((s) => s.hoveredEdgeId);
  const selectedEdgeId = useGalaxy((s) => s.selectedEdgeId);
  const minScore = useGalaxy((s) => s.minScore);
  const activeTypes = useGalaxy((s) => s.activeTypes);
  const showLabels = useGalaxy((s) => s.showLabels);
  const edgeBudget = useGalaxy((s) => s.edgeBudget);
  const setHovered = useGalaxy((s) => s.setHovered);
  const setHoveredEdge = useGalaxy((s) => s.setHoveredEdge);
  const selectEdge = useGalaxy((s) => s.selectEdge);
  const clearSelection = useGalaxy((s) => s.clearSelection);
  const warpTo = useGalaxy((s) => s.warpTo);

  const model = focusId ? system : galaxy;
  const emphasis = useMemo(
    () => (model ? computeEmphasis(model, { hoveredId, hoveredEdgeId, selectedEdgeId, minScore, activeTypes: activeTypes as Set<string>, budget: edgeBudget }) : null),
    [model, hoveredId, hoveredEdgeId, selectedEdgeId, minScore, activeTypes, edgeBudget],
  );

  const navMode = useGalaxy((s) => s.navMode);
  useEffect(() => {
    document.body.style.cursor = hoveredId || hoveredEdgeId ? "pointer" : navMode === "fly" ? "crosshair" : "";
    return () => {
      document.body.style.cursor = "";
    };
  }, [hoveredId, hoveredEdgeId, navMode]);

  const calloutNode = callout && model ? model.nodeById.get(callout.companyId) : undefined;

  return (
    <>
      <Director warpRef={warpRef} galaxy={galaxy} system={system} />
      <FlyControls />
      <OrbitControls makeDefault enablePan={false} enableDamping dampingFactor={0.07} rotateSpeed={0.45} zoomSpeed={0.7} minDistance={22} maxDistance={900} maxPolarAngle={Math.PI * 0.62} />
      <fog attach="fog" args={[LAB.bg, 520, 1600]} />
      <Backdrop radius={1700} stars={900} />
      {/* 장난감 우주 조명 — 랩과 동일한 구성 */}
      <ambientLight intensity={0.55} color="#c9d4ff" />
      <hemisphereLight args={["#9ab3ff", "#3a1f5c", 0.6]} />
      <directionalLight position={[-60, 80, 70]} intensity={2.4} color="#fff3e0" />
      <group onPointerMissed={(e) => e.button === 0 && clearSelection()}>
        {model && emphasis && (
          <group key={model.kind + (model.centerId ?? "")}>
            <RelationLines edges={model.edges} emphasis={emphasis} onHover={setHoveredEdge} onSelect={selectEdge} intensity={model.kind === "galaxy" ? 0.85 : 1} />
            <CompanyNodes model={model} emphasis={emphasis} hoveredId={hoveredId} onHover={setHovered} onClick={warpTo} />
            <Labels model={model} emphasis={emphasis} visible={showLabels} />
            {callout && calloutNode && (
              <Html position={[calloutNode.pos[0], calloutNode.pos[1] - calloutNode.size * 3.2, calloutNode.pos[2]]} center zIndexRange={[5, 0]} style={{ pointerEvents: "none", transform: "translateY(50%)" }}>
                <div className="scene-callout" style={{ pointerEvents: "auto" }}>
                  <div className="row between">
                    <span className="kick">{callout.kicker}</span>
                    <button type="button" className="callout-close" onClick={callout.onClose} aria-label="뉴스 알림 닫기">
                      ×
                    </button>
                  </div>
                  <strong>{callout.title}</strong>
                  <span className="meta">{callout.meta}</span>
                </div>
              </Html>
            )}
          </group>
        )}
      </group>
      <Warp intensityRef={warpRef} />
      <Effects warpRef={warpRef} />
      <AdaptiveDpr />
    </>
  );
}

export default function Scene(props: Omit<ContentProps, "warpRef">) {
  const warpRef = useRef(0);
  return (
    <Canvas
      dpr={[1, 1.75]}
      gl={{ antialias: false, powerPreference: "high-performance", alpha: false }}
      camera={{ fov: 52, near: 0.5, far: 3000, position: FALLBACK_POSE.galaxy.pos }}
      raycaster={{ params: { Line: { threshold: 1.0 }, Points: { threshold: 1 }, Mesh: {}, LOD: {}, Sprite: {} } }}
      onCreated={(state) => {
        // 플랫 컬러가 그대로 나오도록 톤매핑은 끈다 (랩과 동일)
        state.gl.toneMapping = THREE.NoToneMapping;
        state.gl.setClearColor(LAB.bg, 1);
      }}
    >
      <Content {...props} warpRef={warpRef} />
    </Canvas>
  );
}
