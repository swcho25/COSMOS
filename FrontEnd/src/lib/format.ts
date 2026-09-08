const KST = "Asia/Seoul";

export function fmtDateTime(iso: string | null | undefined) {
  if (!iso) return "-";
  const d = new Date(iso);
  const p = new Intl.DateTimeFormat("ko-KR", {
    timeZone: KST,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(d);
  const g = (t: string) => p.find((x) => x.type === t)?.value ?? "";
  return `${g("year")}. ${g("month")}. ${g("day")}. ${g("hour")}:${g("minute")}`;
}

export function fmtDate(iso: string | null | undefined) {
  if (!iso) return "-";
  return new Intl.DateTimeFormat("ko-KR", { timeZone: KST, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(iso));
}

export function fmtShortDate(iso: string) {
  const d = new Date(iso);
  return new Intl.DateTimeFormat("ko-KR", { timeZone: KST, month: "numeric", day: "numeric" }).format(d);
}

export function fmtTime(iso: string) {
  return new Intl.DateTimeFormat("ko-KR", { timeZone: KST, hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(iso));
}

export function fmtRelative(iso: string, now = Date.now()) {
  const diff = Math.max(0, now - new Date(iso).getTime());
  const m = Math.floor(diff / 60000);
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 전`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}일 전`;
  return fmtDate(iso);
}

export function fmtNumber(n: number | null | undefined, digits = 0) {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  return new Intl.NumberFormat("ko-KR", { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(n);
}

export function fmtCompact(n: number | null | undefined) {
  if (n === null || n === undefined) return "-";
  return new Intl.NumberFormat("ko-KR", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

export function fmtPrice(n: number | null | undefined, currency: "KRW" | "USD" = "KRW") {
  if (n === null || n === undefined) return "-";
  if (currency === "USD") return `$${new Intl.NumberFormat("en-US", { maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(n)}`;
  return `₩${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 }).format(n)}`;
}

export function fmtPct(n: number | null | undefined, digits = 2, signed = true) {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  const s = n > 0 && signed ? "+" : "";
  return `${s}${n.toFixed(digits)}%`;
}

export function fmtScore(n: number | null | undefined) {
  if (n === null || n === undefined) return "-";
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

export function clamp(v: number, min: number, max: number) {
  return Math.max(min, Math.min(max, v));
}

export function initials(name: string) {
  const latin = name.match(/[A-Za-z0-9]+/g);
  if (latin && latin.length > 1) return latin.slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  if (latin?.[0] && latin[0].length === name.replace(/\s/g, "").length) return latin[0].slice(0, 2).toUpperCase();
  return name.replace(/\s+/g, "").slice(0, 2);
}
