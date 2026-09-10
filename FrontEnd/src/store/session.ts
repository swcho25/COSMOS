import { create } from "zustand";
import { api, ApiError, type Me } from "@/api";
import { setSessionExpiredHandler, tokenStore } from "@/api/client";

type Status = "booting" | "guest" | "authed";

interface SessionState {
  status: Status;
  user: Me | null;
  /** 로그인 사용자별 캐시 분리를 위한 키 */
  sessionKey: string;
  bootstrap(): Promise<void>;
  login(email: string, password: string): Promise<void>;
  signup(email: string, password: string, nickname: string): Promise<void>;
  logout(): Promise<void>;
  setUser(user: Me): void;
}

export const useSession = create<SessionState>((set) => ({
  status: "booting",
  user: null,
  sessionKey: "guest",

  async bootstrap() {
    // userId 는 64비트 정수라 캐시 키로 쓰려면 문자열로 바꾼다
    setSessionExpiredHandler(() => set({ status: "guest", user: null, sessionKey: "guest" }));
    try {
      await api.auth.refresh();
      const me = await api.users.me();
      set({ status: "authed", user: me, sessionKey: String(me.userId) });
    } catch {
      tokenStore.set(null);
      set({ status: "guest", user: null, sessionKey: "guest" });
    }
  },

  async login(email, password) {
    await api.auth.login({ email, password });
    const me = await api.users.me();
    set({ status: "authed", user: me, sessionKey: String(me.userId) });
  },

  async signup(email, password, nickname) {
    await api.auth.signup({ email, password, nickname });
    await api.auth.login({ email, password });
    const me = await api.users.me();
    set({ status: "authed", user: me, sessionKey: String(me.userId) });
  },

  async logout() {
    try {
      await api.auth.logout();
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
    }
    tokenStore.set(null);
    set({ status: "guest", user: null, sessionKey: "guest" });
  },

  setUser(user) {
    set({ user });
  },
}));
