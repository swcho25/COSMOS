import * as THREE from "three";

/**
 * 관계 간선의 곡선 정의 — 빔(RelationLines)·화살촉·툴팁이 같은 곡선 위의 점을 얻도록 한곳에 둔다.
 * 두 노드를 잇는 2차 베지어 아크이고, 제어점은 원점 바깥으로 살짝 부풀린다.
 * 끝점은 Vec3 도 되고, 여러 점이 이어 붙은 좌표 버퍼(live.pos 등) + 오프셋도 된다 — 드래그로 움직인 끝점을 복사 없이 읽기 위해서다.
 */
export const BOW = 0.16;

const A = new THREE.Vector3();
const B = new THREE.Vector3();
const OUT = new THREE.Vector3();

/** 제어점을 out 에 쓰고 두 끝점 거리를 돌려준다 */
export function controlPoint(from: ArrayLike<number>, to: ArrayLike<number>, out: THREE.Vector3, fromOff = 0, toOff = 0) {
  A.fromArray(from, fromOff);
  B.fromArray(to, toOff);
  const dist = A.distanceTo(B);
  out.addVectors(A, B).multiplyScalar(0.5);
  OUT.copy(out);
  if (OUT.lengthSq() < 1e-6) OUT.set(0, 1, 0);
  OUT.normalize().multiplyScalar(dist * BOW);
  OUT.y += dist * BOW * 0.35;
  out.add(OUT);
  return dist;
}

/** 곡선 위 t(0~1) 지점 */
export function bezier(from: ArrayLike<number>, to: ArrayLike<number>, ctrl: THREE.Vector3, t: number, out: THREE.Vector3, fromOff = 0, toOff = 0) {
  const it = 1 - t;
  out.set(0, 0, 0);
  out.addScaledVector(A.fromArray(from, fromOff), it * it);
  out.addScaledVector(ctrl, 2 * it * t);
  out.addScaledVector(B.fromArray(to, toOff), t * t);
  return out;
}

/** 점수(0~1) → 튜브 반지름(월드 단위). 가까이 가면 굵어지고 멀면 가늘어진다 */
export function radiusOf(s: number) {
  return 0.09 + s * s * 0.4;
}
