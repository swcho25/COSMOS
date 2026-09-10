import { useEffect, useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import type { SceneModel } from "@/lib/graph";
import { useGalaxy } from "@/store/galaxy";
import { pctOf } from "./altitude";
import { touch } from "./bufferUtil";
import { liveFor, morphState, type LiveTransform } from "./morph";

interface Props {
  model: SceneModel;
}

/** 꼬리를 붙이는 등락률 기준(%) — 이 아래는 원반 두께와 구분이 안 돼 선만 지저분해진다 */
const TAIL_MIN = 5;
/** 흰-푸른 실선. 상승·하락을 색으로 나누지 않는다 — 위·아래라는 위치가 이미 방향을 말한다 */
const TAIL_COLOR = "#bcd0ff";

/** 꼬리 버퍼 (useMemo 로 만든 값의 부분집합) */
interface TailBuffers {
  pos: Float32Array;
  idx: number[];
  attr: THREE.BufferAttribute;
}

/**
 * 선분 양 끝을 지금 위치로 다시 쓴다 — 아래는 레이아웃 평면(node.pos 의 y), 위는 live 의 행성.
 * x·z 를 둘 다 live 에서 읽으므로 끌어 옮긴 행성 아래에서도 꼬리가 수직으로 선다.
 * (렌더 안에서 만든 함수가 useMemo 버퍼를 고치면 react-hooks/immutability 에 걸리므로 모듈에 둔다 — followEdges 와 같은 이유)
 */
function writeTails(buf: TailBuffers, model: SceneModel, live: LiveTransform) {
  const { pos, idx } = buf;
  for (let k = 0; k < idx.length; k += 1) {
    const i = idx[k];
    const o = k * 6;
    pos[o] = live.pos[i * 3];
    pos[o + 1] = model.nodes[i].pos[1];
    pos[o + 2] = live.pos[i * 3 + 2];
    pos[o + 3] = live.pos[i * 3];
    pos[o + 4] = live.pos[i * 3 + 1];
    pos[o + 5] = live.pos[i * 3 + 2];
  }
  touch(buf.attr);
}

/**
 * 주가 고도의 꼬리 — 원반 평면에서 행성까지 긋는 가는 수직선 (LineSegments 1개, 은하 뷰 전용).
 * 부감 시점에서는 "행성이 얼마나 떠 있는지" 가 잘 보이지 않는데, 평면에 발을 붙여 주면 높이가 거리로 읽힌다.
 * 매 프레임 live 버퍼를 읽지만 노드 수만큼(≈188)의 선분이라 저렴하다.
 */
export default function AltitudeTails({ model }: Props) {
  const lines = useRef<THREE.LineSegments>(null);
  const live = liveFor(model);
  const altitudeWindow = useGalaxy((s) => s.altitudeWindow);

  const built = useMemo(() => {
    const idx: number[] = [];
    model.nodes.forEach((nd, i) => {
      const pct = pctOf(nd, altitudeWindow);
      if (pct !== null && Math.abs(pct) >= TAIL_MIN) idx.push(i);
    });
    const pos = new Float32Array(idx.length * 6);
    const attr = new THREE.BufferAttribute(pos, 3);
    attr.setUsage(THREE.DynamicDrawUsage);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", attr);
    return { geo, attr, pos, idx };
  }, [model, altitudeWindow]);
  useEffect(() => () => built.geo.dispose(), [built]);

  useFrame(() => {
    const mesh = lines.current;
    if (!mesh) return;
    // 워프 낙하 중에는 행성이 원반을 떠나므로 발을 붙일 평면이 없다
    mesh.visible = !morphState.plan && built.idx.length > 0;
    if (mesh.visible) writeTails(built, model, live);
  });

  return (
    <lineSegments ref={lines} geometry={built.geo} frustumCulled={false} raycast={() => null} renderOrder={1}>
      <lineBasicMaterial color={TAIL_COLOR} transparent opacity={0.35} depthWrite={false} toneMapped={false} fog={false} />
    </lineSegments>
  );
}
