import { useEffect, useRef } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import * as THREE from "three";
import { useGalaxy } from "@/store/galaxy";
import { cameraBusy } from "./cameraState";
import { codeOf, isTyping, modalOpen } from "./keyboard";

/** 이동 속도(월드 단위/초), Shift 가속 배율, 시선 감도 */
const SPEED = 55;
const BOOST = 3.2;
const LOOK = 0.0022;
/** 은하 밖으로 너무 멀리 나가지 않도록 하는 반지름 */
const BOUND = 900;

const FWD = new THREE.Vector3();
const RIGHT = new THREE.Vector3();
const UP = new THREE.Vector3(0, 1, 0);
const WISH = new THREE.Vector3();
const EULER = new THREE.Euler(0, 0, 0, "YXZ");

/**
 * 탐색(비행) 카메라 — 우주선을 타고 행성 사이를 직접 날아다니는 시점.
 *  - W/S 앞뒤, A/D 좌우, Q/E(또는 Space/C) 위아래, Shift 가속, 휠 전후 추진
 *  - 마우스 드래그로 시선 회전 (yaw·pitch), 클릭은 그대로 기업 선택으로 전달된다
 *  - F 키로 궤도 모드와 전환. 궤도로 돌아갈 때는 시선 앞의 지점을 궤도 중심으로 삼아 카메라가 튀지 않는다
 * 워프·인트로 애니메이션 중에는 입력을 받지 않는다.
 */
export default function FlyControls() {
  const camera = useThree((s) => s.camera);
  const gl = useThree((s) => s.gl);
  const controls = useThree((s) => s.controls) as unknown as { target: THREE.Vector3; update: () => void } | null;
  const navMode = useGalaxy((s) => s.navMode);

  const st = useRef({
    keys: new Set<string>(),
    vel: new THREE.Vector3(),
    yaw: 0,
    pitch: 0,
    dragging: false,
    lastX: 0,
    lastY: 0,
    wheel: 0,
  }).current;

  // 모드 전환: 비행 진입 시 현재 시선을 yaw·pitch 로 읽고, 궤도 복귀 시 시선 앞 지점을 궤도 중심으로 둔다
  useEffect(() => {
    if (navMode === "fly") {
      EULER.setFromQuaternion(camera.quaternion, "YXZ");
      st.yaw = EULER.y;
      st.pitch = EULER.x;
      st.vel.set(0, 0, 0);
    } else if (controls) {
      camera.getWorldDirection(FWD);
      const toOrigin = new THREE.Vector3().sub(camera.position);
      const ahead = Math.max(30, toOrigin.dot(FWD));
      controls.target.copy(camera.position).addScaledVector(FWD, ahead);
      controls.update();
    }
    st.keys.clear();
    st.dragging = false;
  }, [navMode, camera, controls, st]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      // 모달(로그인 등)이 떠 있으면 모드 전환·비행 키를 받지 않는다 — OrbitKeys 와 같은 규칙
      if (isTyping() || modalOpen() || useGalaxy.getState().searchOpen) return;
      const code = codeOf(e);
      if (code === "KeyF" && !e.repeat && !e.ctrlKey && !e.metaKey && !e.altKey) {
        useGalaxy.getState().toggleNavMode();
        e.preventDefault();
        return;
      }
      if (useGalaxy.getState().navMode !== "fly") return;
      st.keys.add(code);
      if (code === "Space") e.preventDefault();
    };
    const onKeyUp = (e: KeyboardEvent) => st.keys.delete(codeOf(e));
    const onBlur = () => st.keys.clear();
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
    };
  }, [st]);

  useEffect(() => {
    const el = gl.domElement;
    const onDown = (e: PointerEvent) => {
      if (useGalaxy.getState().navMode !== "fly" || (e.button !== 0 && e.button !== 2)) return;
      st.dragging = true;
      st.lastX = e.clientX;
      st.lastY = e.clientY;
    };
    const onMove = (e: PointerEvent) => {
      if (!st.dragging) return;
      const dx = e.clientX - st.lastX;
      const dy = e.clientY - st.lastY;
      st.lastX = e.clientX;
      st.lastY = e.clientY;
      st.yaw -= dx * LOOK;
      st.pitch = THREE.MathUtils.clamp(st.pitch - dy * LOOK, -Math.PI * 0.49, Math.PI * 0.49);
    };
    const onUp = () => {
      st.dragging = false;
    };
    const onWheel = (e: WheelEvent) => {
      if (useGalaxy.getState().navMode !== "fly") return;
      e.preventDefault();
      // 휠 한 틱(약 100)당 추진 1, 트랙패드 등 큰 delta 는 3 까지
      st.wheel -= THREE.MathUtils.clamp(e.deltaY / 100, -3, 3);
    };
    const onContext = (e: MouseEvent) => {
      if (useGalaxy.getState().navMode === "fly") e.preventDefault();
    };
    el.addEventListener("pointerdown", onDown);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    el.addEventListener("wheel", onWheel, { passive: false });
    el.addEventListener("contextmenu", onContext);
    return () => {
      el.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("contextmenu", onContext);
    };
  }, [gl, st]);

  useFrame((_, delta) => {
    const s = useGalaxy.getState();
    if (s.navMode !== "fly") return;
    const dt = Math.min(delta, 0.05);
    // 워프·재프레이밍 연출 중에는 연출 카메라에 맡기고, 끝난 시선을 그대로 이어받는다
    if (cameraBusy.anim) {
      st.vel.set(0, 0, 0);
      st.wheel = 0;
      EULER.setFromQuaternion(camera.quaternion, "YXZ");
      st.yaw = EULER.y;
      st.pitch = EULER.x;
      return;
    }

    // 시선
    EULER.set(st.pitch, st.yaw, 0, "YXZ");
    camera.quaternion.setFromEuler(EULER);

    // 이동 의도 (카메라 기준)
    camera.getWorldDirection(FWD);
    RIGHT.crossVectors(FWD, UP).normalize();
    WISH.set(0, 0, 0);
    const k = st.keys;
    if (k.has("KeyW") || k.has("ArrowUp")) WISH.add(FWD);
    if (k.has("KeyS") || k.has("ArrowDown")) WISH.sub(FWD);
    if (k.has("KeyD") || k.has("ArrowRight")) WISH.add(RIGHT);
    if (k.has("KeyA") || k.has("ArrowLeft")) WISH.sub(RIGHT);
    if (k.has("KeyE") || k.has("Space")) WISH.add(UP);
    if (k.has("KeyQ") || k.has("KeyC")) WISH.sub(UP);
    if (WISH.lengthSq() > 0) WISH.normalize();
    const boost = k.has("ShiftLeft") || k.has("ShiftRight") ? BOOST : 1;
    // 휠 추진: 한 틱마다 짧게 앞으로 미는 힘
    if (Math.abs(st.wheel) > 0.001) {
      WISH.addScaledVector(FWD, st.wheel * 4.0);
      st.wheel *= Math.pow(0.02, dt);
      if (Math.abs(st.wheel) < 0.01) st.wheel = 0;
    }

    // 관성: 목표 속도로 부드럽게 수렴
    const target = WISH.multiplyScalar(SPEED * boost);
    const damp = 1 - Math.pow(0.002, dt);
    st.vel.lerp(target, damp);
    camera.position.addScaledVector(st.vel, dt);

    // 은하 밖으로 무한히 나가지 않게 한다
    const d = camera.position.length();
    if (d > BOUND) camera.position.multiplyScalar(BOUND / d);
  });

  return null;
}
