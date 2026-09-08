import * as THREE from "three";

let dot: THREE.Texture | null = null;
/** 별·헤일로에 쓰는 소프트 원형 텍스처 (1회 생성 공유) */
export function dotTexture() {
  if (dot) return dot;
  const s = 64;
  const c = document.createElement("canvas");
  c.width = c.height = s;
  const ctx = c.getContext("2d")!;
  const g = ctx.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  g.addColorStop(0, "rgba(255,255,255,1)");
  g.addColorStop(0.25, "rgba(255,255,255,0.85)");
  g.addColorStop(0.6, "rgba(255,255,255,0.16)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, s, s);
  dot = new THREE.CanvasTexture(c);
  dot.colorSpace = THREE.SRGBColorSpace;
  return dot;
}

let ring: THREE.Texture | null = null;
/** 선택 노드용 링 텍스처 */
export function ringTexture() {
  if (ring) return ring;
  const s = 128;
  const c = document.createElement("canvas");
  c.width = c.height = s;
  const ctx = c.getContext("2d")!;
  ctx.strokeStyle = "rgba(255,255,255,1)";
  ctx.lineWidth = 5;
  ctx.beginPath();
  ctx.arc(s / 2, s / 2, s / 2 - 6, 0, Math.PI * 2);
  ctx.stroke();
  ctx.strokeStyle = "rgba(255,255,255,0.35)";
  ctx.lineWidth = 12;
  ctx.beginPath();
  ctx.arc(s / 2, s / 2, s / 2 - 8, 0, Math.PI * 2);
  ctx.stroke();
  ring = new THREE.CanvasTexture(c);
  ring.colorSpace = THREE.SRGBColorSpace;
  return ring;
}

/* ------------------------------ 한글 라벨 ------------------------------ */
const labelCache = new Map<string, { tex: THREE.Texture; aspect: number }>();
const FONT = 'Pretendard Variable, Pretendard, "Malgun Gothic", "Apple SD Gothic Neo", "Segoe UI", sans-serif';

/**
 * 한글 라벨을 캔버스 텍스처로 만든다 (troika Text 는 기본 폰트에 한글 글리프가 없다).
 * 어두운 우주 배경 위에서도 읽히도록 그림자 + 본문 2패스로 그린다.
 */
export function labelTexture(text: string, color = "#E6EEFC", weight = 600, px = 52) {
  const key = `${text}|${color}|${weight}|${px}`;
  const hit = labelCache.get(key);
  if (hit) return hit;
  const font = `${weight} ${px}px ${FONT}`;
  const measure = document.createElement("canvas").getContext("2d")!;
  measure.font = font;
  const w = Math.ceil(measure.measureText(text).width);
  const padX = px * 0.5;
  const padY = px * 0.4;
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(2, w + padX * 2);
  canvas.height = Math.max(2, px + padY * 2);
  const ctx = canvas.getContext("2d")!;
  ctx.font = font;
  ctx.textBaseline = "middle";
  ctx.textAlign = "center";
  ctx.shadowColor = "rgba(3,6,14,0.95)";
  ctx.shadowBlur = px * 0.5;
  ctx.fillStyle = "rgba(3,6,14,0.9)";
  ctx.fillText(text, canvas.width / 2, canvas.height / 2);
  ctx.shadowBlur = 0;
  ctx.fillStyle = color;
  ctx.fillText(text, canvas.width / 2, canvas.height / 2);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.minFilter = THREE.LinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.anisotropy = 4;
  const entry = { tex, aspect: canvas.width / canvas.height };
  labelCache.set(key, entry);
  return entry;
}
