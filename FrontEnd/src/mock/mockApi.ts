/**
 * 브라우저 안에서 동작하는 API 목업. 응답 형태는 docs/api-specification.md 와 동일하다.
 * 로그인·관심 기업·스크랩·가중치·댓글은 localStorage 에 저장되어 새로고침 후에도 유지된다.
 */
import type { CosmosApi } from "@/api/contract";
import { ApiError, tokenStore } from "@/api/client";
import type * as T from "@/api/types";
import { stableUuid } from "@/lib/rng";
import {
  ADJACENCY,
  COMPANIES,
  COMPANY_BY_ID,
  EDGES,
  EDGE_BY_ID,
  INDUSTRIES,
  NEWS,
  NEWS_BY_COMPANY,
  NEWS_BY_ID,
  SEED_COMMENTS,
  currentSnapshot,
  isDirected,
  metricsFor,
  otherEnd,
  stockSeries,
  windowDays,
  type MockComment,
  type MockCompany,
  type MockEdge,
  type MockNews,
} from "./universe";

const DAY = 86_400_000;

/* ------------------------------------------------------------------ */
/* 유틸                                                                */
/* ------------------------------------------------------------------ */
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const latency = (base = 140) => sleep(base + Math.random() * 160);

function fail(status: number, code: string, message: string, fieldErrors: T.FieldError[] = []): never {
  throw new ApiError(status, { code, message, fieldErrors });
}

function decodeCursor(cursor?: string) {
  if (!cursor) return 0;
  const n = Number(atob(cursor));
  if (!Number.isInteger(n) || n < 0) fail(400, "INVALID_CURSOR", "커서 형식이 올바르지 않습니다.");
  return n;
}
function paginate<T>(all: T[], params?: { cursor?: string; size?: number }, max = 100): T.Page<T> {
  const size = Math.min(max, Math.max(1, params?.size ?? 20));
  const offset = decodeCursor(params?.cursor);
  const items = all.slice(offset, offset + size);
  const next = offset + size;
  return { items, nextCursor: next < all.length ? btoa(String(next)) : null, hasNext: next < all.length };
}

/* ------------------------------------------------------------------ */
/* 저장소 (localStorage)                                                */
/* ------------------------------------------------------------------ */
interface StoredUser {
  userId: string;
  email: string;
  password: string;
  nickname: string;
}
interface UserState {
  watch: Record<string, string>;
  scraps: Record<string, string>;
  newsWeight: number | null;
  weightUpdatedAt: string | null;
}
interface StoredComment extends MockComment {
  deleted?: boolean;
}

const LS = {
  users: "cosmos.mock.users",
  session: "cosmos.mock.session",
  user: (id: string) => `cosmos.mock.user.${id}`,
  comments: "cosmos.mock.comments",
};

function read<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}
function write(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

const DEMO_USER: StoredUser = { userId: stableUuid("user:demo"), email: "demo@cosmos.dev", password: "cosmos123", nickname: "우주탐험가" };
function users(): StoredUser[] {
  const list = read<StoredUser[]>(LS.users, []);
  if (!list.some((u) => u.userId === DEMO_USER.userId)) {
    list.unshift(DEMO_USER);
    write(LS.users, list);
  }
  return list;
}
function userState(userId: string): UserState {
  return read<UserState>(LS.user(userId), { watch: {}, scraps: {}, newsWeight: null, weightUpdatedAt: null });
}
function saveUserState(userId: string, s: UserState) {
  write(LS.user(userId), s);
}

/** 토큰 → 사용자. 형식: mock.<userId>.<issuedAt> */
function currentUser(): StoredUser | null {
  const token = tokenStore.get();
  if (!token?.startsWith("mock.")) return null;
  const [, userId] = token.split(".");
  return users().find((u) => u.userId === userId) ?? null;
}
function requireUser(): StoredUser {
  const u = currentUser();
  if (!u) fail(401, tokenStore.get() ? "TOKEN_INVALID" : "TOKEN_EXPIRED", "로그인이 필요합니다.");
  return u;
}
function issueToken(userId: string) {
  const token = `mock.${userId}.${Date.now()}`;
  tokenStore.set(token);
  return token;
}

function allComments(): StoredComment[] {
  const extra = read<StoredComment[]>(LS.comments, []);
  const overridden = new Map(extra.map((c) => [c.commentId, c]));
  const merged: StoredComment[] = [];
  SEED_COMMENTS.forEach((c) => merged.push(overridden.get(c.commentId) ?? c));
  extra.forEach((c) => {
    if (!SEED_COMMENTS.some((s) => s.commentId === c.commentId)) merged.push(c);
  });
  return merged.filter((c) => !c.deleted).sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}
function upsertComment(c: StoredComment) {
  const extra = read<StoredComment[]>(LS.comments, []);
  const idx = extra.findIndex((x) => x.commentId === c.commentId);
  if (idx >= 0) extra[idx] = c;
  else extra.push(c);
  write(LS.comments, extra);
}

/* ------------------------------------------------------------------ */
/* 매퍼                                                                */
/* ------------------------------------------------------------------ */
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function personalizedScore(score: number, weight: number | null) {
  if (weight === null || weight === 50) return score;
  // 뉴스 가중치가 높을수록 뉴스 기반 점수를 소폭 증폭 (프로토타입 근사)
  const k = 0.85 + (weight / 100) * 0.3;
  return Math.round(Math.min(100, score * k) * 10) / 10;
}
function toEdge(e: MockEdge, weight: number | null): T.GraphEdge {
  return {
    relationshipId: e.relationshipId,
    sourceCompanyId: e.sourceCompanyId,
    targetCompanyId: e.targetCompanyId,
    relationshipType: e.relationshipType,
    score: personalizedScore(e.score, weight),
    impactDirection: e.impactDirection,
  };
}
function toSummary(c: MockCompany, watch: Record<string, string>): T.CompanySummary {
  return {
    companyId: c.companyId,
    name: c.name,
    nameEn: c.nameEn,
    stockCode: c.stockCode,
    market: c.market,
    primaryIndustry: { industryId: c.industryId, name: c.industry },
    watched: Boolean(watch[c.companyId]),
  };
}
function toNewsItem(n: MockNews, scraps: Record<string, string>): T.NewsItem {
  return {
    newsId: n.newsId,
    title: n.title,
    summary: n.summary,
    publisher: n.publisher,
    originalUrl: n.originalUrl,
    publishedAt: n.publishedAt,
    sentiment: n.sentiment,
    relatedCompanies: n.related.map((r) => ({ companyId: r.companyId, name: COMPANY_BY_ID.get(r.companyId)?.name ?? "" })),
    scrapped: Boolean(scraps[n.newsId]),
  };
}
function weightOf(user: StoredUser | null) {
  return user ? userState(user.userId).newsWeight : null;
}

/* ------------------------------------------------------------------ */
/* 구현                                                                */
/* ------------------------------------------------------------------ */
export const mockApi: CosmosApi = {
  auth: {
    async checkEmail(email) {
      await latency(80);
      if (!EMAIL_RE.test(email)) fail(400, "VALIDATION_FAILED", "이메일 형식이 올바르지 않습니다.", [{ field: "email", message: "이메일 형식 오류" }]);
      return { available: !users().some((u) => u.email.toLowerCase() === email.toLowerCase()) };
    },
    async checkNickname(nickname) {
      await latency(80);
      if (nickname.length < 2 || nickname.length > 30) fail(400, "VALIDATION_FAILED", "닉네임은 2~30자여야 합니다.", [{ field: "nickname", message: "길이 오류" }]);
      return { available: !users().some((u) => u.nickname === nickname) };
    },
    async login({ email, password }) {
      await latency(220);
      const u = users().find((x) => x.email.toLowerCase() === email.toLowerCase() && x.password === password);
      if (!u) fail(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호가 올바르지 않습니다.");
      write(LS.session, u.userId);
      return { accessToken: issueToken(u.userId) };
    },
    async logout() {
      await latency(60);
      localStorage.removeItem(LS.session);
      tokenStore.set(null);
    },
    async refresh() {
      await latency(90);
      const sessionUser = read<string | null>(LS.session, null);
      if (!sessionUser || !users().some((u) => u.userId === sessionUser)) fail(401, "TOKEN_EXPIRED", "세션이 만료되었습니다.");
      return { accessToken: issueToken(sessionUser) };
    },
    async signup({ email, password, nickname }) {
      await latency(260);
      const fieldErrors: T.FieldError[] = [];
      if (!EMAIL_RE.test(email) || email.length > 255) fieldErrors.push({ field: "email", message: "이메일 형식이 올바르지 않습니다." });
      if (!/^(?=.*[A-Za-z])(?=.*\d).{8,64}$/.test(password)) fieldErrors.push({ field: "password", message: "8~64자, 영문과 숫자를 각 1자 이상 포함해야 합니다." });
      if (nickname.trim().length < 2 || nickname.length > 30) fieldErrors.push({ field: "nickname", message: "닉네임은 2~30자여야 합니다." });
      if (fieldErrors.length) fail(400, "VALIDATION_FAILED", "입력값을 확인해 주세요.", fieldErrors);
      const list = users();
      if (list.some((u) => u.email.toLowerCase() === email.toLowerCase())) fail(409, "EMAIL_DUPLICATED", "이미 사용 중인 이메일입니다.");
      if (list.some((u) => u.nickname === nickname)) fail(409, "NICKNAME_DUPLICATED", "이미 사용 중인 닉네임입니다.");
      const user: StoredUser = { userId: stableUuid(`user:${email.toLowerCase()}`), email, password, nickname: nickname.trim() };
      list.push(user);
      write(LS.users, list);
      return { userId: user.userId, email: user.email, nickname: user.nickname };
    },
  },

  users: {
    async me() {
      await latency(70);
      const u = requireUser();
      return { userId: u.userId, email: u.email, nickname: u.nickname };
    },
    async updateNickname(nickname) {
      await latency(150);
      const u = requireUser();
      if (nickname.trim().length < 2 || nickname.length > 30) fail(400, "VALIDATION_FAILED", "닉네임은 2~30자여야 합니다.", [{ field: "nickname", message: "길이 오류" }]);
      const list = users();
      if (list.some((x) => x.nickname === nickname && x.userId !== u.userId)) fail(409, "NICKNAME_DUPLICATED", "이미 사용 중인 닉네임입니다.");
      const me = list.find((x) => x.userId === u.userId)!;
      me.nickname = nickname.trim();
      write(LS.users, list);
      return { userId: me.userId, email: me.email, nickname: me.nickname };
    },
    async scraps(p) {
      await latency();
      const u = requireUser();
      const s = userState(u.userId);
      const items = Object.entries(s.scraps)
        .map(([newsId, scrappedAt]) => ({ n: NEWS_BY_ID.get(newsId), scrappedAt }))
        .filter((x): x is { n: MockNews; scrappedAt: string } => Boolean(x.n))
        .sort((a, b) => b.scrappedAt.localeCompare(a.scrappedAt))
        .map(({ n, scrappedAt }) => ({ newsId: n.newsId, title: n.title, summary: n.summary, publisher: n.publisher, originalUrl: n.originalUrl, publishedAt: n.publishedAt, scrappedAt }));
      return paginate(items, p);
    },
    async addScrap(newsId) {
      await latency(120);
      const u = requireUser();
      if (!NEWS_BY_ID.has(newsId)) fail(404, "NEWS_NOT_FOUND", "뉴스를 찾을 수 없습니다.");
      const s = userState(u.userId);
      if (s.scraps[newsId]) fail(409, "NEWS_SCRAP_DUPLICATED", "이미 스크랩한 뉴스입니다.");
      const at = new Date().toISOString();
      s.scraps[newsId] = at;
      saveUserState(u.userId, s);
      return { newsId, scrappedAt: at };
    },
    async removeScrap(newsId) {
      await latency(100);
      const u = requireUser();
      const s = userState(u.userId);
      delete s.scraps[newsId];
      saveUserState(u.userId, s);
    },
    async watchCompanies(p) {
      await latency();
      const u = requireUser();
      const s = userState(u.userId);
      const items = Object.entries(s.watch)
        .map(([companyId, watchedAt]) => ({ c: COMPANY_BY_ID.get(companyId), watchedAt }))
        .filter((x): x is { c: MockCompany; watchedAt: string } => Boolean(x.c))
        .sort((a, b) => b.watchedAt.localeCompare(a.watchedAt))
        .map(({ c, watchedAt }) => ({ companyId: c.companyId, name: c.name, stockCode: c.stockCode, market: c.market, primaryIndustry: { industryId: c.industryId, name: c.industry }, watchedAt }));
      return paginate(items, p);
    },
    async addWatch(companyId) {
      await latency(120);
      const u = requireUser();
      if (!COMPANY_BY_ID.has(companyId)) fail(404, "COMPANY_NOT_FOUND", "기업을 찾을 수 없습니다.");
      const s = userState(u.userId);
      if (s.watch[companyId]) fail(409, "WATCH_COMPANY_DUPLICATED", "이미 등록한 관심 기업입니다.");
      const at = new Date().toISOString();
      s.watch[companyId] = at;
      saveUserState(u.userId, s);
      return { companyId, watchedAt: at };
    },
    async removeWatch(companyId) {
      await latency(100);
      const u = requireUser();
      const s = userState(u.userId);
      delete s.watch[companyId];
      saveUserState(u.userId, s);
    },
    async weightSetting() {
      await latency(70);
      const u = requireUser();
      const s = userState(u.userId);
      const nw = s.newsWeight ?? 50;
      return { newsWeight: nw, disclosureWeight: 100 - nw, updatedAt: s.weightUpdatedAt };
    },
    async updateWeightSetting(newsWeight) {
      await latency(160);
      const u = requireUser();
      if (!Number.isInteger(newsWeight) || newsWeight < 0 || newsWeight > 100) fail(400, "VALIDATION_FAILED", "newsWeight 는 0~100 정수여야 합니다.", [{ field: "newsWeight", message: "범위 오류" }]);
      const s = userState(u.userId);
      s.newsWeight = newsWeight;
      s.weightUpdatedAt = new Date().toISOString();
      saveUserState(u.userId, s);
      return { newsWeight, disclosureWeight: 100 - newsWeight, updatedAt: s.weightUpdatedAt };
    },
  },

  companies: {
    async list(p) {
      await latency();
      const u = currentUser();
      const watch = u ? userState(u.userId).watch : {};
      const kw = p?.keyword?.trim().toLowerCase();
      let items = COMPANIES.slice();
      if (p?.market) items = items.filter((c) => c.market === p.market);
      if (p?.industryId) items = items.filter((c) => c.industryId === p.industryId);
      if (kw) {
        const scored = items
          .map((c) => {
            const hay = [c.name, c.nameEn, c.stockCode].map((s) => s.toLowerCase());
            let score = 0;
            if (hay.some((h) => h === kw)) score = 3;
            else if (hay.some((h) => h.startsWith(kw))) score = 2;
            else if (hay.some((h) => h.includes(kw))) score = 1;
            return { c, score };
          })
          .filter((x) => x.score > 0)
          .sort((a, b) => b.score - a.score || a.c.name.localeCompare(b.c.name, "ko"));
        items = scored.map((x) => x.c);
      } else {
        items.sort((a, b) => a.name.localeCompare(b.name, "ko"));
      }
      return paginate(items.map((c) => toSummary(c, watch)), p);
    },
    async detail(companyId) {
      await latency(110);
      const c = COMPANY_BY_ID.get(companyId);
      if (!c) fail(404, "COMPANY_NOT_FOUND", "기업을 찾을 수 없습니다.");
      const u = currentUser();
      const watch = u ? userState(u.userId).watch : {};
      return {
        companyId: c.companyId,
        name: c.name,
        nameEn: c.nameEn,
        stockCode: c.stockCode,
        market: c.market,
        description: c.description,
        industries: [{ industryId: c.industryId, name: c.industry, primary: true }],
        watched: Boolean(watch[c.companyId]),
      };
    },
    async metrics(companyId, window = "30D") {
      await latency(120);
      if (!COMPANY_BY_ID.has(companyId)) fail(404, "COMPANY_NOT_FOUND", "기업을 찾을 수 없습니다.");
      const m = metricsFor(companyId, window);
      return { companyId, window, ...m, measuredAt: currentSnapshot().asOfAt };
    },
    async metricsHistory(companyId, window = "30D") {
      await latency(160);
      if (!COMPANY_BY_ID.has(companyId)) fail(404, "COMPANY_NOT_FOUND", "기업을 찾을 수 없습니다.");
      const days = windowDays(window);
      const end = new Date();
      end.setUTCHours(0, 0, 0, 0);
      const items: T.MetricPoint[] = [];
      for (let i = days - 1; i >= 0; i -= 1) {
        const at = end.getTime() - i * DAY + DAY - 1;
        const m = metricsFor(companyId, "7D", at);
        items.push({ measuredAt: new Date(at).toISOString(), ...m, sentimentScore: m.sentimentScore ?? 0.5 });
      }
      return { companyId, window, items };
    },
    async stockPrices(companyId, period = "1M") {
      await latency(140);
      const c = COMPANY_BY_ID.get(companyId);
      if (!c) fail(404, "COMPANY_NOT_FOUND", "기업을 찾을 수 없습니다.");
      const all = stockSeries(c);
      const n = period === "1M" ? 22 : period === "3M" ? 65 : period === "6M" ? 130 : all.length;
      const items = all.slice(-n);
      return { companyId, period, interval: "1D", asOfAt: items.at(-1)?.tradingAt ?? new Date().toISOString(), items };
    },
    async industries() {
      await latency(90);
      return { items: INDUSTRIES.map((i) => ({ ...i })) };
    },
  },

  news: {
    async list(p) {
      await latency();
      const u = currentUser();
      const scraps = u ? userState(u.userId).scraps : {};
      let items = p?.companyId ? (NEWS_BY_COMPANY.get(p.companyId) ?? []) : NEWS;
      if (p?.industryId) items = items.filter((n) => n.related.some((r) => COMPANY_BY_ID.get(r.companyId)?.industryId === p.industryId));
      if (p?.sentiment) items = items.filter((n) => (p.companyId ? n.related.find((r) => r.companyId === p.companyId)?.sentiment : n.sentiment) === p.sentiment);
      if (p?.keyword) {
        const kw = p.keyword.toLowerCase();
        items = items.filter((n) => n.title.toLowerCase().includes(kw));
      }
      if (p?.from) {
        const f = Date.parse(p.from);
        items = items.filter((n) => n.publishedMs >= f);
      }
      if (p?.to) {
        const t = Date.parse(p.to) + DAY;
        items = items.filter((n) => n.publishedMs < t);
      }
      return paginate(items.map((n) => toNewsItem(n, scraps)), p);
    },
    async detail(newsId) {
      await latency(120);
      const n = NEWS_BY_ID.get(newsId);
      if (!n) fail(404, "NEWS_NOT_FOUND", "뉴스를 찾을 수 없습니다.");
      const u = currentUser();
      const scraps = u ? userState(u.userId).scraps : {};
      return {
        newsId: n.newsId,
        title: n.title,
        summary: n.summary,
        publisher: n.publisher,
        author: n.author,
        originalUrl: n.originalUrl,
        publishedAt: n.publishedAt,
        relatedCompanies: n.related.map((r) => ({ companyId: r.companyId, name: COMPANY_BY_ID.get(r.companyId)?.name ?? "", sentiment: r.sentiment, relevanceScore: r.relevanceScore, impactScore: r.impactScore })),
        evidence: n.evidence,
        scrapped: Boolean(scraps[n.newsId]),
      };
    },
  },

  graphs: {
    async latest(p) {
      await latency(260);
      const u = currentUser();
      const weight = weightOf(u);
      const perCompany = Math.min(5, Math.max(1, p?.edgesPerCompany ?? 5));
      if (p?.industryId && !INDUSTRIES.some((i) => i.industryId === p.industryId)) fail(400, "VALIDATION_FAILED", "지원하지 않는 산업입니다.");
      const nodes = p?.industryId ? COMPANIES.filter((c) => c.industryId === p.industryId) : COMPANIES;
      const nodeIds = new Set(nodes.map((c) => c.companyId));
      // 기업별 상위 N 간선 — 어느 한쪽 끝점의 상위 N 에 들면 유지
      const keep = new Set<string>();
      nodes.forEach((c) => {
        (ADJACENCY.get(c.companyId) ?? [])
          .filter((e) => nodeIds.has(otherEnd(e, c.companyId)))
          .slice(0, perCompany)
          .forEach((e) => keep.add(e.relationshipId));
      });
      const edges = EDGES.filter((e) => keep.has(e.relationshipId)).map((e) => toEdge(e, weight));
      const snap = currentSnapshot();
      return {
        ...snap,
        personalized: weight !== null && weight !== 50,
        nodes: nodes.map((c) => ({ companyId: c.companyId, name: c.name, stockCode: c.stockCode, market: c.market, industryName: c.industry })),
        edges,
      };
    },
    async company(companyId, p) {
      await latency(240);
      const center = COMPANY_BY_ID.get(companyId);
      if (!center) fail(404, "COMPANY_NOT_FOUND", "기업을 찾을 수 없습니다.");
      const maxDepth = Math.min(3, Math.max(1, p?.maxDepth ?? 3));
      const perNode = Math.min(5, Math.max(1, p?.neighborsPerNode ?? 3));
      const maxNodes = Math.min(100, Math.max(2, p?.maxNodes ?? 100));
      const u = currentUser();
      const weight = weightOf(u);

      const depth = new Map<string, number>([[companyId, 0]]);
      const order: string[] = [companyId];
      let frontier = [companyId];
      for (let d = 1; d <= maxDepth && order.length < maxNodes; d += 1) {
        const next: string[] = [];
        for (const id of frontier) {
          const adj = (ADJACENCY.get(id) ?? []).filter((e) => !depth.has(otherEnd(e, id))).slice(0, perNode);
          for (const e of adj) {
            const o = otherEnd(e, id);
            if (depth.has(o) || order.length >= maxNodes) continue;
            depth.set(o, d);
            order.push(o);
            next.push(o);
          }
        }
        frontier = next;
      }
      const included = new Set(order);
      const edges = EDGES.filter((e) => included.has(e.sourceCompanyId) && included.has(e.targetCompanyId)).map((e) => toEdge(e, weight));
      const snap = currentSnapshot();
      return {
        snapshotId: snap.snapshotId,
        asOfAt: snap.asOfAt,
        centerCompanyId: companyId,
        personalized: weight !== null && weight !== 50,
        nodes: order.map((id) => ({ companyId: id, name: COMPANY_BY_ID.get(id)!.name, depth: depth.get(id)! })),
        edges,
      };
    },
    async relationship(relationshipId, window = "30D") {
      await latency(130);
      const e = EDGE_BY_ID.get(relationshipId);
      if (!e) fail(404, "RELATIONSHIP_NOT_FOUND", "기업 관계를 찾을 수 없습니다.");
      const u = currentUser();
      const weight = weightOf(u);
      // 윈도우별 점수: 해당 기간 근거 뉴스 비중으로 살짝 변동
      const days = windowDays(window);
      const from = Date.now() - days * DAY;
      const evidences = NEWS.filter((n) => n.relationshipId === relationshipId);
      const inWindow = evidences.filter((n) => n.publishedMs >= from).length;
      const ratio = evidences.length ? inWindow / evidences.length : 1;
      const adj = window === "90D" ? 1 : window === "30D" ? 0.85 + ratio * 0.25 : 0.7 + ratio * 0.5;
      const score = Math.round(Math.min(100, personalizedScore(e.score, weight) * adj) * 10) / 10;
      const s = COMPANY_BY_ID.get(e.sourceCompanyId)!;
      const t = COMPANY_BY_ID.get(e.targetCompanyId)!;
      return {
        relationshipId,
        sourceCompany: { companyId: s.companyId, name: s.name },
        targetCompany: { companyId: t.companyId, name: t.name },
        relationshipType: e.relationshipType,
        directionality: isDirected(e.relationshipType) ? "DIRECTED" : "UNDIRECTED",
        window,
        score,
        impactDirection: e.impactDirection,
        confidence: e.confidence,
        evidenceCount: Math.max(inWindow, Math.round(e.evidenceCount * (window === "90D" ? 1 : window === "30D" ? 0.6 : 0.25))),
        asOfAt: currentSnapshot().asOfAt,
        personalized: weight !== null && weight !== 50,
      };
    },
    async evidence(relationshipId, p) {
      await latency(150);
      if (!EDGE_BY_ID.has(relationshipId)) fail(404, "RELATIONSHIP_NOT_FOUND", "기업 관계를 찾을 수 없습니다.");
      const items: T.RelationshipEvidence[] = NEWS.filter((n) => n.relationshipId === relationshipId).map((n) => ({
        newsId: n.newsId,
        title: n.title,
        summary: n.summary,
        publisher: n.publisher,
        originalUrl: n.originalUrl,
        publishedAt: n.publishedAt,
        evidenceSentence: n.evidence[0]?.sentence ?? n.summary,
        contributionScore: n.contributionScore,
        confidence: n.evidence[0]?.confidence ?? 0.8,
      }));
      return paginate(items, { cursor: p?.cursor, size: p?.size ?? 10 }, 20);
    },
  },

  community: {
    async latest(p) {
      await latency();
      const items: T.GlobalComment[] = allComments().map((c) => ({
        commentId: c.commentId,
        content: c.content,
        author: { userId: c.userId, nickname: c.nickname },
        company: { companyId: c.companyId, name: COMPANY_BY_ID.get(c.companyId)?.name ?? "" },
        createdAt: c.createdAt,
        updatedAt: c.updatedAt,
        edited: c.edited,
      }));
      return paginate(items, p);
    },
    async byCompany(companyId, p) {
      await latency();
      if (!COMPANY_BY_ID.has(companyId)) fail(404, "COMPANY_NOT_FOUND", "기업을 찾을 수 없습니다.");
      const items: T.CompanyComment[] = allComments()
        .filter((c) => c.companyId === companyId)
        .map((c) => ({ commentId: c.commentId, content: c.content, author: { userId: c.userId, nickname: c.nickname }, companyId: c.companyId, createdAt: c.createdAt, updatedAt: c.updatedAt, edited: c.edited }));
      return paginate(items, p);
    },
    async write(companyId, content) {
      await latency(180);
      const u = requireUser();
      if (!COMPANY_BY_ID.has(companyId)) fail(404, "COMPANY_NOT_FOUND", "기업을 찾을 수 없습니다.");
      const text = content.trim();
      if (!text || text.length > 1000) fail(400, "VALIDATION_FAILED", "댓글은 1~1000자여야 합니다.", [{ field: "content", message: "길이 오류" }]);
      const now = new Date().toISOString();
      const c: StoredComment = { commentId: crypto.randomUUID(), companyId, userId: u.userId, nickname: u.nickname, content: text, createdAt: now, updatedAt: now, edited: false };
      upsertComment(c);
      return { commentId: c.commentId, companyId, content: text, createdAt: now, updatedAt: now };
    },
    async edit(commentId, content) {
      await latency(160);
      const u = requireUser();
      const c = allComments().find((x) => x.commentId === commentId);
      if (!c) fail(404, "COMMENT_NOT_FOUND", "댓글을 찾을 수 없습니다.");
      if (c.userId !== u.userId) fail(403, "FORBIDDEN", "본인 댓글만 수정할 수 있습니다.");
      const text = content.trim();
      if (!text || text.length > 1000) fail(400, "VALIDATION_FAILED", "댓글은 1~1000자여야 합니다.", [{ field: "content", message: "길이 오류" }]);
      const next: StoredComment = { ...c, content: text, updatedAt: new Date().toISOString(), edited: true };
      upsertComment(next);
      return { commentId, content: text, createdAt: next.createdAt, updatedAt: next.updatedAt, edited: true };
    },
    async remove(commentId) {
      await latency(120);
      const u = requireUser();
      const c = allComments().find((x) => x.commentId === commentId);
      if (!c) return;
      if (c.userId !== u.userId) fail(403, "FORBIDDEN", "본인 댓글만 삭제할 수 있습니다.");
      upsertComment({ ...c, deleted: true });
    },
  },
};
