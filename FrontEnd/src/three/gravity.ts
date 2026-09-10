/**
 * 중력 우물 장 — 기업 질량과 산업 성단을 가우시안 우물로 모아 텍스처 한 장에 담는다.
 *
 * 격자(GravityGrid)는 정점마다 모든 우물의 합을 구해야 하는데, 우물을 유니폼 배열로 넘기면 개수가 셰이더 컴파일 시점에
 * 고정된다(모델마다 재컴파일). 256×1 RGBA Float DataTexture 로 넘기면 셰이더는 그대로 두고 개수만 유니폼으로 바꿀 수 있고,
 * 프레임마다 바뀌는 값(호버 딤플·워프 모프 위치)도 4KB 업로드 한 번으로 끝난다.
 * 텍셀 = (x, z, 질량, σ). Float 텍스처의 선형 보간은 OES_texture_float_linear 확장에 의존하므로 반드시 Nearest 로 읽는다.
 */
import * as THREE from "three";
import { massOf, type SceneModel } from "@/lib/graph";
import { liveFor, type LiveTransform } from "./morph";

/** 텍스처 폭 = 우물 상한. GravityGrid 정점 셰이더가 이 값을 템플릿으로 박아 쓰므로 여기만 고치면 된다 */
export const MAX_WELLS = 256;
/** 높이 감쇠 길이 — 바닥에서 이만큼 떠 있는 노드의 우물은 절반이 된다 (은하는 입체라 위 껍질 행성도 바닥에 투영된다) */
const ATTEN_H = 110;
/**
 * 기업 중심 뷰 중심 기업의 우물 배율 — 깔때기 사면 위에 depth-1 딤플이 앉도록 크게 판다.
 * 실측(목업 4개 중심): 중심 우물 80.6 vs 가장 무거운 depth-1 우물 5.4~12.4 → 6.5~14.9배.
 * 여기서 '딤플'은 사면 위 국소 파임(= 그 노드 우물의 진폭)이다. 이웃 자리의 절대 깊이(= 깔때기 꼬리까지 합친 값)와
 * 비교하면 어떤 배율·σ 로도 3배가 나오지 않는다 — 가우시안 합이라 r≈20 에서 꼬리가 0.34배로 남기 때문.
 */
const CENTER_BOOST = 3;
/** 잎 노드도 최소한의 딤플은 남기는 바닥 질량 */
const LEAF_MASS = 0.35;
/** 저사양 기기에서 유지할 노드 우물 수 (질량 상위) */
const LOW_END_WELLS = 96;

let lowEnd: boolean | null = null;
/** 논리 코어 4 이하 = 통합 GPU 노트북으로 보고 격자·우물 수를 줄인다 */
export function isLowEnd() {
  if (lowEnd === null) lowEnd = typeof navigator !== "undefined" && (navigator.hardwareConcurrency ?? 8) <= 4;
  return lowEnd;
}

export interface WellField {
  /** 256×1 RGBA Float — 텍셀 (x, z, 질량, σ) */
  tex: THREE.DataTexture;
  data: Float32Array;
  /** 실제로 쓰는 우물 수 (앞쪽 nodeCount 개가 노드, 나머지가 성단) */
  count: number;
  nodeCount: number;
  /** 노드 우물 슬롯 → model.nodes 인덱스 */
  nodeIdx: Int32Array;
  /** 기업 id → 노드 우물 슬롯 (호버 딤플이 슬롯을 찾는 길) */
  slotOf: Map<string, number>;
  /** 슬롯별 기본 질량·반경 (성단 슬롯 포함) */
  mass: Float32Array;
  sigma: Float32Array;
  /** 격자 바닥 높이 */
  floorY: number;
  /** 격자 반폭 */
  half: number;
  /** 노드 자리에서 관측되는 최대 깊이 ×1.1 — 깊이색 정규화 기준 */
  maxDepth: number;
}

/** 바닥에서 뜬 높이만큼 우물을 약하게 — 위 껍질 행성이 바닥을 뚫을 듯 파지 않게 한다 */
function atten(y: number, floorY: number) {
  const h = (y - floorY) / ATTEN_H;
  return 1 / (1 + h * h);
}

/**
 * 모델 하나의 우물 장을 만든다. 성단 텍셀은 여기서 한 번만 쓰고, 노드 텍셀은 writeWells 가 프레임마다 덮어쓴다.
 * maxDepth 는 노드 위치에서의 합 최대값이라 모델당 O(N × 우물) 한 번만 계산한다.
 */
export function buildWellField(model: SceneModel): WellField {
  const clusters = model.clusters;
  const room = Math.max(0, MAX_WELLS - clusters.length);
  const cap = isLowEnd() ? Math.min(room, LOW_END_WELLS) : room;
  // 질량 큰(= 연결 많은) 기업부터 담는다. size 는 질량의 단조 함수라 정렬 기준으로 그대로 쓴다
  const order = model.nodes.map((_, i) => i);
  order.sort((a, b) => model.nodes[b].size - model.nodes[a].size);
  const nodeIdx = Int32Array.from(order.slice(0, cap));
  const nodeCount = nodeIdx.length;
  const count = nodeCount + clusters.length;

  const floorY = model.kind === "galaxy" ? model.extent.minY - 18 : -14;
  const half = model.kind === "galaxy" ? Math.max(80, model.extent.maxR * 1.35 + 30) : 120;

  const mass = new Float32Array(count);
  const sigma = new Float32Array(count);
  const slotOf = new Map<string, number>();
  for (let s = 0; s < nodeCount; s += 1) {
    const n = model.nodes[nodeIdx[s]];
    const boost = model.kind === "system" && n.id === model.centerId ? CENTER_BOOST : 1;
    mass[s] = (massOf(n.size) + LEAF_MASS) * boost;
    sigma[s] = 6 + 2.2 * n.size;
    slotOf.set(n.id, s);
  }

  const data = new Float32Array(MAX_WELLS * 4);
  // 성단 분지 — 중심·반경이 레이아웃 고정값이라 정적이다 (은하 뷰만, 기업 중심 뷰는 clusters 가 비어 있다)
  clusters.forEach((c, ci) => {
    const s = nodeCount + ci;
    mass[s] = c.mass * 0.5 * atten(c.center[1], floorY);
    sigma[s] = Math.max(6, c.radius * 0.9);
    data[s * 4] = c.center[0];
    data[s * 4 + 1] = c.center[2];
    data[s * 4 + 2] = mass[s];
    data[s * 4 + 3] = sigma[s];
  });

  const tex = new THREE.DataTexture(data, MAX_WELLS, 1, THREE.RGBAFormat, THREE.FloatType);
  tex.minFilter = THREE.NearestFilter;
  tex.magFilter = THREE.NearestFilter;
  tex.generateMipmaps = false;
  tex.needsUpdate = true;

  const field: WellField = { tex, data, count, nodeCount, nodeIdx, slotOf, mass, sigma, floorY, half, maxDepth: 1 };
  writeWells(field, liveFor(model), new Float32Array(nodeCount).fill(1));
  field.maxDepth = maxDepthOf(field, model);
  return field;
}

/**
 * 노드 텍셀을 지금 위치·질량으로 다시 쓴다.
 * 위치는 node.pos 가 아니라 live 버퍼라 워프 모프 중에도 우물이 행성을 따라 움직인다.
 * massScale 은 슬롯 길이(field.nodeCount)와 같아야 한다 — 호버 딤플이 1 → 1.6 으로 오르는 배율.
 */
export function writeWells(field: WellField, live: LiveTransform, massScale: Float32Array) {
  const { data, nodeIdx, mass, sigma, floorY } = field;
  for (let s = 0; s < field.nodeCount; s += 1) {
    const i = nodeIdx[s];
    data[s * 4] = live.pos[i * 3];
    data[s * 4 + 1] = live.pos[i * 3 + 2];
    data[s * 4 + 2] = mass[s] * massScale[s] * atten(live.pos[i * 3 + 1], floorY);
    data[s * 4 + 3] = sigma[s];
  }
  field.tex.needsUpdate = true;
}

/** 노드 자리에서 재는 우물 합의 최대값 ×1.1 — 가장 깊은 분지가 딱 가장 밝은 색이 되도록 하는 정규화 기준 */
export function maxDepthOf(field: WellField, model: SceneModel) {
  const { data, count } = field;
  let max = 0;
  model.nodes.forEach((n) => {
    let d = 0;
    for (let s = 0; s < count; s += 1) {
      const dx = n.pos[0] - data[s * 4];
      const dz = n.pos[2] - data[s * 4 + 1];
      const s2 = data[s * 4 + 3] * data[s * 4 + 3];
      d += data[s * 4 + 2] * Math.exp(-(dx * dx + dz * dz) / (2 * s2));
    }
    max = Math.max(max, d);
  });
  // 0 으로 나누지 않도록 하한을 둔다 (노드가 없는 빈 모델)
  return Math.max(1e-3, max * 1.1);
}
