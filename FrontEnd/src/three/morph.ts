import type { SceneModel, Vec3 } from "@/lib/graph";

/**
 * 노드의 "지금 그려지는" 위치·스케일 — 모델당 하나의 공유 버퍼.
 * `node.pos` 는 레이아웃 결과라 불변으로 두고(간선 from/to 가 참조), 워프 모프처럼 프레임마다 움직이는 값은
 * 여기에만 쓴다. 행성·칩·헤일로·격자가 모두 이 버퍼를 읽으므로 위치의 단일 소스가 된다.
 */
export interface LiveTransform {
  model: SceneModel;
  /** 노드 i 의 위치 (i*3 .. i*3+2) */
  pos: Float32Array;
  /** 노드 i 의 스케일 배율 (1 = 레이아웃 크기) */
  scale: Float32Array;
}

const LIVE = new WeakMap<SceneModel, LiveTransform>();

/** 모델의 live 버퍼. 없으면 node.pos 를 복사해 만든다 — 렌더 중에도 안전하게 호출할 수 있다 */
export function liveFor(model: SceneModel): LiveTransform {
  let live = LIVE.get(model);
  if (!live) {
    const n = model.nodes.length;
    const pos = new Float32Array(n * 3);
    const scale = new Float32Array(n).fill(1);
    model.nodes.forEach((node, i) => {
      pos[i * 3] = node.pos[0];
      pos[i * 3 + 1] = node.pos[1];
      pos[i * 3 + 2] = node.pos[2];
    });
    live = { model, pos, scale };
    LIVE.set(model, live);
  }
  return live;
}

/** 워프 모프 계획 — 원통 좌표로 보간할 출발·도착 상태 (planMorph 는 워프 모프 단계에서 채운다) */
export interface MorphPlan {
  src: SceneModel;
  dst: SceneModel;
  /** 진행도 0~1 */
  t: number;
  n: number;
  rA: Float32Array;
  thA: Float32Array;
  yA: Float32Array;
  rB: Float32Array;
  thB: Float32Array;
  yB: Float32Array;
  dth: Float32Array;
  delay: Float32Array;
  sizeRatio: Float32Array;
  spawn: Uint8Array;
  ghosts: { pos: Vec3; dir: Vec3; color: string; radius: number }[];
}

/**
 * 모프 진행 상태 — Director 가 쓰고 행성·칩·빔이 읽는다.
 * plan 이 null 이면 모프 중이 아니다(현재 팝인 폴백). fade/reveal 은 빔 게이트, landedAt 은 착지 시각(ms).
 */
export const morphState = { plan: null as MorphPlan | null, fade: 1, reveal: 1, landedAt: 0 };
