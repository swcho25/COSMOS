/**
 * 행성 셰이더 — 광택 있는 홀로그램 구체.
 *  - 산업색을 바탕으로 시선 각도에 따라 색이 변하는 이리데센스(무지갯빛)
 *  - 시드별 방향의 부드러운 능선(릿지)이 법선을 살짝 흔들어 표면에 결이 생긴다
 *  - 주광 하이라이트 + 보조광 하이라이트 + 프레넬 림
 *  - 로고는 아틀라스 텍스처에서 읽어 카메라를 향한 앞면에 "인쇄"되고, 그 위로 같은 조명이 얹힌다
 * 인스턴스 속성: aColor · aSeed · aBright(호버/중심) · aEmph(강조) · aCell(아틀라스 셀 원점) · aHasLogo
 */
export const planetVertex = /* glsl */ `
  attribute vec3 aColor;
  attribute float aSeed;
  attribute float aBright;
  attribute float aEmph;
  attribute vec2 aCell;
  attribute float aHasLogo;
  varying vec3 vColor;
  varying vec3 vNormalW;
  varying vec3 vNormalV;
  varying vec3 vViewDir;
  varying vec3 vSurf;
  varying float vSeed;
  varying float vBright;
  varying float vEmph;
  varying vec2 vCell;
  varying float vHasLogo;
  void main() {
    vColor = aColor; vSeed = aSeed; vBright = aBright; vEmph = aEmph; vCell = aCell; vHasLogo = aHasLogo;
    vSurf = normal;
    vec4 worldPos = modelMatrix * instanceMatrix * vec4(position, 1.0);
    vec3 nW = normalize(mat3(modelMatrix) * mat3(instanceMatrix) * normal);
    vNormalW = nW;
    vNormalV = normalize(mat3(viewMatrix) * nW);
    vViewDir = normalize(cameraPosition - worldPos.xyz);
    gl_Position = projectionMatrix * viewMatrix * worldPos;
  }
`;

export const planetFragment = /* glsl */ `
  uniform sampler2D uAtlas;
  uniform float uCell;      // 아틀라스 셀 크기 (uv 단위)
  uniform vec3 uLight;
  uniform float uTime;
  varying vec3 vColor;
  varying vec3 vNormalW;
  varying vec3 vNormalV;
  varying vec3 vViewDir;
  varying vec3 vSurf;
  varying float vSeed;
  varying float vBright;
  varying float vEmph;
  varying vec2 vCell;
  varying float vHasLogo;

  vec3 hueShift(vec3 c, float h) {
    const vec3 k = vec3(0.57735);
    float cosA = cos(h);
    float sinA = sin(h);
    return c * cosA + cross(k, c) * sinA + k * dot(k, c) * (1.0 - cosA);
  }

  void main() {
    vec3 n = normalize(vNormalV);          // 뷰 공간 법선 — 하이라이트 위치가 카메라 기준으로 고정된다
    vec3 v = vec3(0.0, 0.0, 1.0);
    float nv = clamp(dot(n, v), 0.0, 1.0);

    // 릿지: 시드별 축을 기준으로 한 동심 능선. 법선을 살짝 기울여 결을 만든다
    vec3 axis = normalize(vec3(sin(vSeed * 6.2831), 0.55 + vSeed * 0.4, cos(vSeed * 6.2831)));
    float d = dot(vSurf, axis);
    float freq = 11.0 + vSeed * 6.0;
    float ridge = sin(d * freq + vSeed * 10.0);
    float ridgeMask = smoothstep(-0.2, 0.9, ridge);
    // 릿지 방향(뷰 공간)으로 법선을 기울인다
    vec3 axisV = normalize(mat3(viewMatrix) * axis);
    vec3 tang = axisV - n * dot(axisV, n);
    float tl = length(tang);
    tang = tl > 0.001 ? tang / tl : vec3(0.0);
    vec3 np = normalize(n + tang * cos(d * freq + vSeed * 10.0) * 0.16);

    // 조명 (뷰 공간): 주광은 왼쪽 위, 보조광은 오른쪽 아래
    vec3 L = normalize(vec3(-0.6, 0.62, 0.5));
    vec3 L2 = normalize(vec3(0.7, -0.55, 0.45));
    vec3 h1 = normalize(L + v);
    vec3 h2 = normalize(L2 + v);
    float diff = 0.30 + 0.70 * clamp(dot(np, L), 0.0, 1.0);
    float spec1 = pow(clamp(dot(np, h1), 0.0, 1.0), 90.0);
    float specSoft = pow(clamp(dot(np, h1), 0.0, 1.0), 6.0);
    float spec2 = pow(clamp(dot(np, h2), 0.0, 1.0), 28.0);
    float fres = pow(1.0 - nv, 2.4);

    // 이리데센스: 시선 각과 릿지 위상에 따라 색상이 돈다
    vec3 base = vColor;
    vec3 warm = hueShift(base, -0.9 + 0.5 * ridgeMask);
    vec3 cool = hueShift(base, 0.8);
    vec3 albedo = mix(base, warm, fres * 0.9);
    albedo = mix(albedo, cool, (1.0 - fres) * ridgeMask * 0.45);
    albedo *= 0.55 + 0.35 * ridgeMask;

    // 로고 데칼: 카메라를 향한 앞면 원 안에 아틀라스 셀을 투영한다
    float logoA = 0.0;
    vec3 logoC = vec3(0.0);
    if (vHasLogo > 0.5 && vNormalV.z > 0.0) {
      vec2 p = vNormalV.xy / 0.86;               // 앞면의 86% 반지름에 로고 원을 맞춘다
      float rr = length(p);
      if (rr < 1.0) {
        vec2 uv = vec2(0.5 + p.x * 0.5, 0.5 - p.y * 0.5);
        vec4 tex = texture2D(uAtlas, vCell + uv * uCell);
        logoC = tex.rgb;
        logoA = tex.a * smoothstep(1.0, 0.94, rr);
      }
    }
    albedo = mix(albedo, logoC, logoA * 0.96);

    // 로고 위에서는 하이라이트를 눌러 판독성을 지킨다
    float keep = 1.0 - logoA * 0.85;
    vec3 col = albedo * mix(diff, 0.82 + 0.18 * diff, logoA);
    col += (vec3(1.0) * spec1 * 0.8 + mix(base, vec3(1.0), 0.5) * specSoft * 0.16) * keep;
    col += mix(cool, vec3(1.0), 0.6) * spec2 * 0.35 * keep;
    col += mix(warm, vec3(1.0), 0.35) * fres * (0.55 - 0.3 * logoA);
    col *= vBright;
    col *= mix(0.32, 1.0, vEmph);
    gl_FragColor = vec4(col, 1.0);
  }
`;
