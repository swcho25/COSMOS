import type { SceneModel } from "./graph";

/**
 * 펼치기(spread) — 하단 도크의 슬라이더 하나로 은하를 세 단계로 본다 (Human Atlas 의 explode 와 같은 구조).
 *   0    은하  : 레이아웃 그대로 (나선 원반 + 주가 고도)
 *   0.5  평면  : 고도·원반 두께를 0 으로 눌러 평면에 놓고 반지름을 살짝 벌린다 — 성단이 갈라져 보이고 간선은 아직 살아 있다
 *   1    정렬  : 행성 크기(시가총액·연결 가중치) 내림차순 격자. 격자 위의 선은 관계가 아니라 잡음이라 간선은 사라진다
 * 좌표는 여기서 계산만 하고, 실제로 옮기는 것은 three/SpreadDriver 다 (live 버퍼 — morph.ts 규칙).
 * 모두 결정론적이라 같은 모델·같은 t 면 같은 자리다.
 */
export type SpreadStage = "galaxy" | "flat" | "sorted";

/** 평면 단계의 슬라이더 값 — 두 구간(은하→평면, 평면→정렬)의 경계 */
export const SPREAD_FLAT = 0.5;
/** 평면 단계에서 반지름을 벌리는 배율 — 카메라 프레이밍(Scene.poseFor)이 같은 배율만큼 물러난다 */
export const FLAT_SCALE = 1.12;
/** 격자의 가로/세로 셀 수 비 — 화면 종횡비(대개 1.6 안팎)에 맞춘다 */
const GRID_ASPECT = 1.6;
/** 셀 한 칸은 가장 큰 행성 지름의 이만큼 — 이웃과 겹치지 않는다 */
const CELL_PER_SIZE = 2.6;

export function spreadStage(t: number): SpreadStage {
  if (t < 0.25) return "galaxy";
  if (t < 0.75) return "flat";
  return "sorted";
}

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
const smoothstep = (a: number, b: number, x: number) => {
  const k = clamp01((x - a) / (b - a));
  return k * k * (3 - 2 * k);
};
const easeInOut = (x: number) => (x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2);

/** 간선·화살촉·글자·기준 링이 남는 비율 — 평면을 지나 정렬로 가는 동안 사라진다 */
export function spreadEdgeAlpha(t: number) {
  return 1 - smoothstep(0.55, 0.85, t);
}

/** 카메라가 물러나야 하는 배율 — 평면 단계의 반지름 벌림을 프레임에 담는다 */
export function spreadRadiusScale(t: number) {
  return 1 + (FLAT_SCALE - 1) * smoothstep(0, SPREAD_FLAT, t);
}

/* ---- 정렬 격자 (모델당 한 번) ------------------------------------------ */
const GRID = new WeakMap<SceneModel, Float32Array>();

/**
 * 크기 내림차순 격자 좌표 (노드 i 의 x·y·z 가 i*3..). y 는 0.
 * 격자 폭은 은하 바깥 반지름(2R) 안에 맞춘다 — 위에서 보는 카메라 프리셋이 그 반지름을 기준으로 프레이밍하기 때문.
 * 첫 행(가장 큰 행성)은 -z 쪽, 위에서 보는 시점에서 화면 위다.
 */
function gridFor(model: SceneModel): Float32Array {
  let grid = GRID.get(model);
  if (grid) return grid;
  const n = model.nodes.length;
  grid = new Float32Array(n * 3);
  if (n === 0) {
    GRID.set(model, grid);
    return grid;
  }
  const order = model.nodes.map((_, i) => i).sort((a, b) => model.nodes[b].size - model.nodes[a].size || (model.nodes[a].id < model.nodes[b].id ? -1 : 1));
  const R = Math.max(20, model.extent.maxR);
  const maxSize = model.nodes.reduce((m, nd) => Math.max(m, nd.size), 0.5);
  const minCell = maxSize * CELL_PER_SIZE;
  let cols = Math.ceil(Math.sqrt(n * GRID_ASPECT));
  cols = Math.max(1, Math.min(cols, Math.floor((2 * R) / minCell) || 1));
  const rows = Math.ceil(n / cols);
  const cell = Math.max(minCell, (2 * R) / cols);
  const w = cols * cell;
  const h = rows * cell;
  order.forEach((i, k) => {
    const c = k % cols;
    const r = Math.floor(k / cols);
    grid![i * 3] = -w / 2 + cell * (c + 0.5);
    grid![i * 3 + 1] = 0;
    grid![i * 3 + 2] = -h / 2 + cell * (r + 0.5);
  });
  GRID.set(model, grid);
  return grid;
}

/**
 * 슬라이더 값 t 에서 모든 노드의 목표 좌표를 out 에 쓴다.
 * base 는 t=0 의 좌표(node.pos 에 주가 고도 y 를 더한 것) — 고도가 켜져 있어도 0 으로 돌아오면 정확히 그 자리다.
 */
export function spreadTargets(model: SceneModel, t: number, base: Float32Array, out: Float32Array) {
  const n = model.nodes.length;
  if (t <= 0) {
    out.set(base.subarray(0, n * 3));
    return;
  }
  const e1 = easeInOut(clamp01(t / SPREAD_FLAT));
  const k2 = clamp01((t - SPREAD_FLAT) / (1 - SPREAD_FLAT));
  const e2 = k2 > 0 ? easeInOut(k2) : 0;
  const grid = k2 > 0 ? gridFor(model) : null;
  for (let i = 0; i < n; i += 1) {
    const o = i * 3;
    const bx = base[o];
    const by = base[o + 1];
    const bz = base[o + 2];
    // 1) 은하 → 평면: 반지름을 살짝 벌리고 높이를 0 으로
    let x = bx + (bx * FLAT_SCALE - bx) * e1;
    let y = by + (0 - by) * e1;
    let z = bz + (bz * FLAT_SCALE - bz) * e1;
    // 2) 평면 → 정렬 격자
    if (grid) {
      x += (grid[o] - x) * e2;
      y += (grid[o + 1] - y) * e2;
      z += (grid[o + 2] - z) * e2;
    }
    out[o] = x;
    out[o + 1] = y;
    out[o + 2] = z;
  }
}
