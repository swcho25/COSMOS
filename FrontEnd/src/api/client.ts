import type { ApiErrorBody } from "./types";

export class ApiError extends Error {
  status: number;
  code: string;
  fieldErrors: ApiErrorBody["fieldErrors"];
  constructor(status: number, body: Partial<ApiErrorBody> | null) {
    super(body?.message ?? `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = body?.code ?? "UNKNOWN";
    this.fieldErrors = body?.fieldErrors ?? [];
  }
}

/** Access Token 은 메모리에만 둔다. 새로고침 시 refresh 쿠키로 재발급한다. */
let accessToken: string | null = null;
export const tokenStore = {
  get: () => accessToken,
  set: (t: string | null) => {
    accessToken = t;
  },
};

const BASE = import.meta.env.VITE_API_BASE ?? "/api";

type Query = object;

export function qs(params?: Query) {
  if (!params) return "";
  const sp = new URLSearchParams();
  Object.entries(params as Record<string, unknown>).forEach(([k, v]) => {
    if (v === undefined || v === null || v === "") return;
    sp.set(k, String(v));
  });
  const s = sp.toString();
  return s ? `?${s}` : "";
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  /** 401 TOKEN_EXPIRED 시 refresh 후 1회 재시도 */
  retry?: boolean;
}

let refreshing: Promise<string | null> | null = null;
async function refreshAccessToken(): Promise<string | null> {
  if (!refreshing) {
    refreshing = fetch(`${BASE}/auth/refresh`, { method: "POST", credentials: "include" })
      .then(async (r) => {
        if (!r.ok) return null;
        const data = (await r.json()) as { accessToken: string };
        tokenStore.set(data.accessToken);
        return data.accessToken;
      })
      .catch(() => null)
      .finally(() => {
        refreshing = null;
      });
  }
  return refreshing;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const token = tokenStore.get();
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(`${BASE}${path}`, {
    method: opts.method ?? "GET",
    headers,
    credentials: "include",
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });

  if (res.status === 204) return undefined as T;
  if (res.ok) return (await res.json()) as T;

  let body: Partial<ApiErrorBody> | null = null;
  try {
    body = (await res.json()) as ApiErrorBody;
  } catch {
    body = null;
  }

  if (res.status === 401 && body?.code === "TOKEN_EXPIRED" && opts.retry !== false) {
    const next = await refreshAccessToken();
    if (next) return request<T>(path, { ...opts, retry: false });
    tokenStore.set(null);
  }
  throw new ApiError(res.status, body);
}
