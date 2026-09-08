import { useEffect } from "react";
import { useThree } from "@react-three/fiber";
import * as THREE from "three";

const vert = /* glsl */ `
  varying vec3 vDir;
  void main(){
    vDir = normalize(position);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

const frag = /* glsl */ `
  varying vec3 vDir;
  float hash(vec3 p){
    p = fract(p * 0.3183099 + vec3(0.1, 0.2, 0.3));
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
  }
  float noise(vec3 x){
    vec3 i = floor(x);
    vec3 f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(hash(i + vec3(0,0,0)), hash(i + vec3(1,0,0)), f.x),
          mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
      mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x),
          mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y), f.z);
  }
  float fbm(vec3 p){
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 5; i++){ v += a * noise(p); p *= 2.07; a *= 0.5; }
    return v;
  }
  void main(){
    vec3 d = vDir;
    float n = fbm(d * 2.1);
    float m = fbm(d * 5.3 + n * 1.4);
    float band = exp(-pow(abs(d.y * 2.6 + n * 0.55 - 0.28), 2.0) * 3.2);
    vec3 deepBlue = vec3(0.035, 0.07, 0.19);
    vec3 indigo   = vec3(0.16, 0.20, 0.52);
    vec3 violet   = vec3(0.26, 0.12, 0.44);
    vec3 teal     = vec3(0.06, 0.30, 0.44);
    vec3 col = deepBlue * 0.5;
    col = mix(col, violet, smoothstep(0.38, 0.92, n) * 0.7);
    col = mix(col, teal, smoothstep(0.48, 0.95, m) * 0.45);
    col += indigo * pow(max(m - 0.6, 0.0), 2.0) * 1.2;
    col *= (0.08 + band * 0.40) * 2.6;
    col += (hash(d * 900.0) - 0.5) * 0.012;
    gl_FragColor = vec4(col, 1.0);
  }
`;

/**
 * 성운 스카이돔. fbm 을 매 프레임 돌리지 않고 기동 시 큐브맵에 한 번만 구워 scene.background 로 쓴다.
 */
export default function Nebula() {
  const gl = useThree((s) => s.gl);
  const scene = useThree((s) => s.scene);

  useEffect(() => {
    const rt = new THREE.WebGLCubeRenderTarget(1024, {
      generateMipmaps: true,
      minFilter: THREE.LinearMipmapLinearFilter,
      magFilter: THREE.LinearFilter,
    });
    const cam = new THREE.CubeCamera(1, 4000, rt);
    const geo = new THREE.SphereGeometry(900, 64, 40);
    const mat = new THREE.ShaderMaterial({ vertexShader: vert, fragmentShader: frag, side: THREE.BackSide, depthWrite: false, toneMapped: false });
    const bake = new THREE.Scene();
    bake.add(new THREE.Mesh(geo, mat));
    const prevTone = gl.toneMapping;
    gl.toneMapping = THREE.NoToneMapping;
    cam.update(gl, bake);
    gl.toneMapping = prevTone;
    const prevBg = scene.background;
    scene.background = rt.texture;
    geo.dispose();
    mat.dispose();
    return () => {
      scene.background = prevBg;
      rt.dispose();
    };
  }, [gl, scene]);

  return null;
}
