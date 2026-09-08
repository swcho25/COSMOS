import { create } from "zustand";

export type AuthDialog = "login" | "signup" | null;
export interface Toast {
  id: number;
  tone: "info" | "success" | "error";
  message: string;
}

interface UiState {
  authDialog: AuthDialog;
  toasts: Toast[];
  openAuth(kind: Exclude<AuthDialog, null>): void;
  closeAuth(): void;
  toast(message: string, tone?: Toast["tone"]): void;
  dismissToast(id: number): void;
}

let seq = 0;
export const useUi = create<UiState>((set) => ({
  authDialog: null,
  toasts: [],
  openAuth(kind) {
    set({ authDialog: kind });
  },
  closeAuth() {
    set({ authDialog: null });
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
