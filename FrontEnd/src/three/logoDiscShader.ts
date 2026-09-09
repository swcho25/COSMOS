/**
 * 로고 디스크 셰이더 — 기업 노드는 카메라를 정면으로 바라보는 원형 로고 하나로만 그린다.
 * 인스턴스 행렬은 매 프레임 카메라 방향으로 정렬(빌보드)되므로 화면 어디에 있어도 원으로 보이고,
 * 로고가 좌우 가장자리에서 틀어지지 않는다. 장식(광택·능선·림)은 두지 않는다.
 * 인스턴스 속성: aBright(호버/중심) · aEmph(강조) · aCell(아틀라스 셀 원점)
 */
export const discVertex = /* glsl */ `
  attribute float aBright;
  attribute float aEmph;
  attribute vec2 aCell;
  varying vec2 vUv;
  varying float vBright;
  varying float vEmph;
  varying vec2 vCell;
  void main() {
    vUv = uv; vBright = aBright; vEmph = aEmph; vCell = aCell;
    gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
  }
`;

export const discFragment = /* glsl */ `
  uniform sampler2D uAtlas;
  uniform float uCell;      // 아틀라스 셀 크기 (uv 단위)
  varying vec2 vUv;
  varying float vBright;
  varying float vEmph;
  varying vec2 vCell;
  void main() {
    vec2 p = vUv * 2.0 - 1.0;
    float r = length(p);
    float aa = fwidth(r) * 1.2;
    float mask = 1.0 - smoothstep(1.0 - aa, 1.0, r);
    if (mask < 0.004) discard;
    // 아틀라스는 캔버스 좌표(y 아래)라 v 를 뒤집어 읽는다
    vec2 uv = vec2(vUv.x, 1.0 - vUv.y);
    vec3 col = texture2D(uAtlas, vCell + uv * uCell).rgb;
    col *= vBright;
    col *= mix(0.3, 1.0, vEmph);
    gl_FragColor = vec4(col, mask);
  }
`;
