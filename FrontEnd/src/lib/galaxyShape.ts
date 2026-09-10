/**
 * 나선 은하의 형상 상수와 팔 곡선 — 배치(layoutGalaxy)와 별가루 배경(GalaxyDust)이 같은 곡선을 써야
 * 행성이 별가루 팔 위에 정확히 얹힌다. 그래서 두 곳이 공유하는 이 파일 하나에만 둔다.
 */

/** 나선 팔 개수 (참고 이미지: 정면에서 본 3개 팔) */
export const ARMS = 3;
/** 로그 나선의 감김 세기(rad) — 클수록 팔이 여러 바퀴 휘감긴다 */
export const ARM_TWIST = 2.3;
/** 로그 나선의 기준 반지름 — r 이 이 값 근처부터 팔이 눈에 띄게 휜다 */
export const R0 = 22;
/** 핵(벌지) 반지름 — 이 안쪽은 팔이 아니라 둥근 핵으로 취급한다 */
export const R_CORE = 14;
/** 원반 바깥 반지름 (전체 우주 기준) */
export const R_MAX = 175;
/** 핵의 두께(σ) — 벌지는 팔보다 두툼하다 */
export const CORE_Y = 4;
/** 팔의 두께(σ) — 바깥으로 갈수록 완만하게 부푼다 */
export const DISC_Y = (r: number) => 1.6 + 0.028 * r;

const TAU = Math.PI * 2;

/** 팔 k 의 중심선이 반지름 r 에서 갖는 각도 (로그 나선) */
export function armTheta(k: number, r: number) {
  return (TAU * k) / ARMS + ARM_TWIST * Math.log(1 + r / R0);
}

/** 최단호로 접은 각도 차 (-π..π) */
function wrapPi(a: number) {
  let v = (a + Math.PI) % TAU;
  if (v < 0) v += TAU;
  return v - Math.PI;
}

/**
 * 반지름 r 의 점 theta 에서 가장 가까운 팔과, 그 중심선까지의 부호 있는 각도 차.
 * delta 는 "theta 에 더하면 팔 위로 간다" 는 방향이라 그대로 접선 인력에 쓸 수 있다.
 */
export function nearestArm(theta: number, r: number) {
  let arm = 0;
  let delta = 0;
  let best = Infinity;
  for (let k = 0; k < ARMS; k += 1) {
    const d = wrapPi(armTheta(k, r) - theta);
    if (Math.abs(d) < best) {
      best = Math.abs(d);
      arm = k;
      delta = d;
    }
  }
  return { arm, delta };
}
