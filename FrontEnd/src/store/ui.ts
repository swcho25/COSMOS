import { create } from "zustand";

export type AuthDialog = "login" | "signup" | null;
/** 다이얼로그를 열 때 넘기는 초기값 — 가입 후 로그인 전환처럼 문맥을 이어줄 때 쓴다 */
export interface AuthPrefill {
  email?: string;
  notice?: string;
}
export interface Toast {
  id: number;
  tone: "info" | "success" | "error";
  message: string;
}

interface UiState {
  authDialog: AuthDialog;
  authPrefill: AuthPrefill | null;
  toasts: Toast[];
  openAuth(kind: Exclude<AuthDialog, null>, opts?: AuthPrefill): void;
  closeAuth(): void;
  toast(message: string, tone?: Toast["tone"]): void;
  dismissToast(id: number): void;
}

let seq = 0;
export const useUi = create<UiState>((set) => ({
  authDialog: null,
  authPrefill: null,
  toasts: [],
  openAuth(kind, opts) {
    // opts 를 주지 않은 기존 호출은 프리필 없이 여는 동작 그대로다
    set({ authDialog: kind, authPrefill: opts ?? null });
  },
  closeAuth() {
    set({ authDialog: null, authPrefill: null });
  },
  toast(message, tone = "info") {
    const id = ++seq;
    set((s) => ({ toasts: [...s.toasts, { id, tone, message }] }));
    setTimeout(() => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), 3600);
  },
  dismissToast(id) {
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
  },
}));
