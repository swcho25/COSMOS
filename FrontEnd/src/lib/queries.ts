import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api";
import type * as T from "@/api/types";
import { useSession } from "@/store/session";

const useKey = () => useSession((s) => s.sessionKey);
const useAuthed = () => useSession((s) => s.status === "authed");
/** 쿼리 키에 들어가는 세션 키 — 컴포넌트가 prefetch 옵션을 만들 때 같은 키를 쓰도록 공개한다 */
export const useSessionKey = useKey;

/* ------------------------------ 기준 정보 ------------------------------ */
export function useIndustries() {
  return useQuery({ queryKey: ["industries"], queryFn: () => api.companies.industries(), staleTime: 60 * 60_000 });
}

/* ------------------------------ 그래프 ------------------------------ */
export function useLatestGraph(industryId: string | null) {
  const k = useKey();
  return useQuery({
    queryKey: ["graph", "latest", k, industryId],
    queryFn: () => api.graphs.latest({ industryId: industryId ?? undefined }),
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
  });
}
/** 기업 그래프 쿼리 옵션 — 훅과 prefetch(호버 선로딩)가 같은 키·함수를 공유하도록 팩토리로 둔다 */
export const companyGraphQuery = (k: string, companyId: string) => ({
  queryKey: ["graph", "company", k, companyId] as const,
  queryFn: () => api.graphs.company(companyId),
  staleTime: 5 * 60_000,
});
export function useCompanyGraph(companyId: string | null) {
  const k = useKey();
  return useQuery({ ...companyGraphQuery(k, companyId ?? ""), enabled: Boolean(companyId) });
}
export function useRelationship(relationshipId: string | null, window: T.MetricWindow) {
  const k = useKey();
  return useQuery({
    queryKey: ["relationship", k, relationshipId, window],
    queryFn: () => api.graphs.relationship(relationshipId!, window),
    enabled: Boolean(relationshipId),
    placeholderData: keepPreviousData,
  });
}
export function useRelationshipEvidence(relationshipId: string | null) {
  return useInfiniteQuery({
    queryKey: ["relationship", "evidence", relationshipId],
    queryFn: ({ pageParam }) => api.graphs.evidence(relationshipId!, { cursor: pageParam, size: 10 }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
    enabled: Boolean(relationshipId),
  });
}

/* ------------------------------ 기업 ------------------------------ */
export function useCompany(companyId: string | null) {
  const k = useKey();
  return useQuery({ queryKey: ["company", k, companyId], queryFn: () => api.companies.detail(companyId!), enabled: Boolean(companyId) });
}
export function useCompanyMetrics(companyId: string | null, window: T.MetricWindow) {
  return useQuery({
    queryKey: ["company", "metrics", companyId, window],
    queryFn: () => api.companies.metrics(companyId!, window),
    enabled: Boolean(companyId),
    placeholderData: keepPreviousData,
  });
}
export function useMetricsHistory(companyId: string | null, params: T.MetricsHistoryParams) {
  return useQuery({
    queryKey: ["company", "metrics-history", companyId, params],
    queryFn: () => api.companies.metricsHistory(companyId!, params),
    enabled: Boolean(companyId),
    placeholderData: keepPreviousData,
  });
}
export function useStockPrices(companyId: string | null, period: T.PricePeriod) {
  return useQuery({
    queryKey: ["company", "prices", companyId, period],
    queryFn: () => api.companies.stockPrices(companyId!, period),
    enabled: Boolean(companyId),
    placeholderData: keepPreviousData,
  });
}
export function useCompanySearch(keyword: string, enabled = true) {
  const k = useKey();
  const q = keyword.trim();
  return useQuery({
    queryKey: ["companies", "search", k, q],
    queryFn: () => api.companies.list({ keyword: q, size: 12 }),
    enabled: enabled && q.length >= 1,
    placeholderData: keepPreviousData,
  });
}
export function useCompanyDirectory(params: T.CompanyListParams) {
  const k = useKey();
  return useInfiniteQuery({
    queryKey: ["companies", "directory", k, params],
    queryFn: ({ pageParam }) => api.companies.list({ ...params, cursor: pageParam, size: params.size ?? 40 }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
    placeholderData: keepPreviousData,
  });
}

/* ------------------------------ 뉴스 ------------------------------ */
export function useNewsFeed(params: T.NewsListParams, enabled = true) {
  const k = useKey();
  return useInfiniteQuery({
    queryKey: ["news", "feed", k, params],
    queryFn: ({ pageParam }) => api.news.list({ ...params, cursor: pageParam, size: params.size ?? 20 }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
    enabled,
    placeholderData: keepPreviousData,
  });
}
export function useNewsDetail(newsId: string | null) {
  const k = useKey();
  return useQuery({ queryKey: ["news", "detail", k, newsId], queryFn: () => api.news.detail(newsId!), enabled: Boolean(newsId) });
}

/* ------------------------------ 커뮤니티 ------------------------------ */
export function useCompanyComments(companyId: string | null) {
  return useInfiniteQuery({
    queryKey: ["comments", "company", companyId],
    queryFn: ({ pageParam }) => api.community.byCompany(companyId!, { cursor: pageParam, size: 20 }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
    enabled: Boolean(companyId),
  });
}
export function useLatestComments(size = 8) {
  return useQuery({ queryKey: ["comments", "latest", size], queryFn: () => api.community.latest({ size }), staleTime: 60_000 });
}

/* ------------------------------ 내 정보 ------------------------------ */
export function useWatchlist() {
  const k = useKey();
  const authed = useAuthed();
  return useQuery({ queryKey: ["me", "watchlist", k], queryFn: () => api.users.watchCompanies({ size: 100 }), enabled: authed });
}
export function useScraps() {
  const k = useKey();
  const authed = useAuthed();
  return useQuery({ queryKey: ["me", "scraps", k], queryFn: () => api.users.scraps({ size: 100 }), enabled: authed });
}
export function useWeightSetting() {
  const k = useKey();
  const authed = useAuthed();
  return useQuery({ queryKey: ["me", "weight", k], queryFn: () => api.users.weightSetting(), enabled: authed });
}

/* ------------------------------ 변경 ------------------------------ */
export function useToggleWatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ companyId, watched }: { companyId: string; watched: boolean }) => {
      if (watched) await api.users.removeWatch(companyId);
      else await api.users.addWatch(companyId);
      return !watched;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["company"] });
      void qc.invalidateQueries({ queryKey: ["companies"] });
      void qc.invalidateQueries({ queryKey: ["me", "watchlist"] });
    },
  });
}
export function useToggleScrap() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ newsId, scrapped }: { newsId: string; scrapped: boolean }) => {
      if (scrapped) await api.users.removeScrap(newsId);
      else await api.users.addScrap(newsId);
      return !scrapped;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["news"] });
      void qc.invalidateQueries({ queryKey: ["me", "scraps"] });
    },
  });
}
export function useWriteComment(companyId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (content: string) => api.community.write(companyId!, content),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["comments"] });
    },
  });
}
export function useEditComment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ commentId, content }: { commentId: string; content: string }) => api.community.edit(commentId, content),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["comments"] });
    },
  });
}
export function useDeleteComment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (commentId: string) => api.community.remove(commentId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["comments"] });
    },
  });
}
export function useUpdateNickname() {
  const setUser = useSession((s) => s.setUser);
  return useMutation({
    mutationFn: (nickname: string) => api.users.updateNickname(nickname),
    onSuccess: (me) => setUser(me),
  });
}
export function useUpdateWeight() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (newsWeight: number) => api.users.updateWeightSetting(newsWeight),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["me", "weight"] });
      void qc.invalidateQueries({ queryKey: ["graph"] });
      void qc.invalidateQueries({ queryKey: ["relationship"] });
    },
  });
}
