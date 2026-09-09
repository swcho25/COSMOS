import * as THREE from "three";
import { FONT } from "./logoAtlas";

/**
 * 이름 아틀라스 — 기업명(한글)을 행 단위로 좌→우 패킹한 큰 캔버스 텍스처.
 * 이름표 칩 셰이더가 인스턴스별 셀 사각형(aName = u,v,w,h)으로 자기 이름을 읽어 칩 188개를 드로우콜 1회로 그린다.
 * (troika Text 는 기본 폰트에 한글 글리프가 없어 캔버스로 굽는다)
 */
const SIZE = 2048;
/** 행 높이(px) — 글자 36px + 그림자 여백 */
const ROW = 56;
const PX = 36;
const PAD = 12;
const FONT_SPEC = `700 ${PX}px ${FONT}`;

export interface NameCell {
  u: number;
  v: number;
  w: number;
  h: number;
  /** 셀 폭 / 셀 높이 — 칩에서 이름 영역의 가로 비율을 정한다 */
  aspect: number;
}

interface Slot {
  text: string;
  x: number;
  y: number;
  w: number;
}

let canvas: HTMLCanvasElement | null = null;
let ctx: CanvasRenderingContext2D | null = null;
let texture: THREE.CanvasTexture | null = null;
const cells = new Map<string, NameCell | null>();
const slots: Slot[] = [];
let cursorX = 0;
let cursorY = 0;
let fontsHooked = false;

export function nameAtlasTexture() {
  if (texture) return texture;
  canvas = document.createElement("canvas");
  canvas.width = canvas.height = SIZE;
  ctx = canvas.getContext("2d")!;
  texture = new THREE.CanvasTexture(canvas);
  texture.flipY = false; // 캔버스 좌표(y 아래)를 그대로 uv 로 쓴다
  texture.colorSpace = THREE.SRGBColorSpace;
  // 은하 기본 거리에서는 36px 로 구운 글자가 화면 ~12px 로 3배 축소되어 읽힌다. 밉맵 없이 bilinear 만 쓰면 회전 중 획이 반짝이므로
  // 밉맵을 켠다 (2048² 는 2의 제곱이라 생성이 싸고, 행마다 글자 위아래 10px 여백이 있어 거친 레벨에서도 이웃 행이 거의 새지 않는다)
  texture.minFilter = THREE.LinearMipmapLinearFilter;
  texture.magFilter = THREE.LinearFilter;
  texture.generateMipmaps = true;
  texture.anisotropy = 4;
  hookFonts();
  return texture;
}

/** 웹폰트(Pretendard)가 늦게 도착하면 폴백 폰트로 구운 셀을 한 번 다시 그린다 */
function hookFonts() {
  if (fontsHooked || typeof document === "undefined" || !("fonts" in document)) return;
  fontsHooked = true;
  document.fonts.ready.then(() => {
    if (!ctx || !texture) return;
    slots.forEach(drawSlot);
    texture.needsUpdate = true;
  });
}

/** 셀 하나를 그린다. 폰트가 바뀌어 글자가 셀보다 넓어지면 가로로만 눌러 넣는다 */
function drawSlot(s: Slot) {
  const c = ctx!;
  c.save();
  c.clearRect(s.x, s.y, s.w, ROW);
  c.beginPath();
  c.rect(s.x, s.y, s.w, ROW);
  c.clip();
  c.font = FONT_SPEC;
  c.textAlign = "center";
  c.textBaseline = "middle";
  const avail = s.w - PAD * 2;
  const measured = c.measureText(s.text).width;
  const squeeze = measured > avail ? avail / measured : 1;
  c.translate(s.x + s.w / 2, s.y + ROW / 2 + 1);
  c.scale(squeeze, 1);
  // 어두운 우주 위에서도 읽히도록 그림자 패스 + 본문 패스
  c.shadowColor = "rgba(3,6,14,0.9)";
  c.shadowBlur = 6;
  c.fillStyle = "rgba(3,6,14,0.85)";
  c.fillText(s.text, 0, 0);
  c.shadowBlur = 0;
  c.fillStyle = "#ffffff";
  c.fillText(s.text, 0, 0);
  c.restore();
}

/**
 * 이름 셀을 확보한다. 같은 텍스트는 같은 셀을 재사용하고, 아틀라스가 가득 차면 null (칩은 로고 원만 그린다).
 */
export function ensureNameCell(text: string): NameCell | null {
  nameAtlasTexture();
  const hit = cells.get(text);
  if (hit !== undefined) return hit;
  const label = text.trim() || "·";
  ctx!.font = FONT_SPEC;
  const w = Math.min(SIZE, Math.ceil(ctx!.measureText(label).width) + PAD * 2);
  if (cursorX + w > SIZE) {
    cursorX = 0;
    cursorY += ROW;
  }
  if (cursorY + ROW > SIZE) {
    cells.set(text, null);
    return null;
  }
  const slot: Slot = { text: label, x: cursorX, y: cursorY, w };
  cursorX += w;
  slots.push(slot);
  drawSlot(slot);
  texture!.needsUpdate = true;
  const cell: NameCell = { u: slot.x / SIZE, v: slot.y / SIZE, w: w / SIZE, h: ROW / SIZE, aspect: w / ROW };
  cells.set(text, cell);
  return cell;
}
