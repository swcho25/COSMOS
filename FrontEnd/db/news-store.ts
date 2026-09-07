import type { NewsSignal } from "../lib/news-data";
import {
  loadNewsHistory,
  newsDatabaseFromEnv,
  saveNewsBodies,
  saveNewsHistory,
} from "./news-repository";
import { createMySqlNewsStore } from "./news-mysql-repository";

export type NewsStore = {
  kind: "d1" | "mysql";
  label: string;
  loadHistory(storageKey: string, now: Date, limit?: number): Promise<NewsSignal[]>;
  saveHistory(storageKey: string, items: NewsSignal[], provider: string, collectedAtIso: string): Promise<void>;
  saveBodies(storageKey: string, items: NewsSignal[], extractedAtIso: string): Promise<number>;
};

export async function newsStoreFromEnv(runtimeEnv: Record<string, unknown>): Promise<NewsStore | null> {
  const driver = runtimeString(runtimeEnv, "NEWS_DATABASE_DRIVER")?.toLowerCase();
  const mysqlUrl = runtimeString(runtimeEnv, "MYSQL_URL");
  const d1 = newsDatabaseFromEnv(runtimeEnv);

  if (driver === "mysql") return mysqlUrl ? createMySqlNewsStore(mysqlUrl) : null;
  if (driver === "d1") return d1 ? d1Store(d1) : null;
  if (d1) return d1Store(d1);
  return mysqlUrl ? createMySqlNewsStore(mysqlUrl) : null;
}

function d1Store(db: NonNullable<ReturnType<typeof newsDatabaseFromEnv>>): NewsStore {
  return {
    kind: "d1",
    label: "D1",
    loadHistory: (storageKey, now, limit) => loadNewsHistory(db, storageKey, now, limit),
    saveHistory: (storageKey, items, provider, collectedAtIso) => saveNewsHistory(db, storageKey, items, provider, collectedAtIso),
    saveBodies: (storageKey, items, extractedAtIso) => saveNewsBodies(db, storageKey, items, extractedAtIso),
  };
}

function runtimeString(runtimeEnv: Record<string, unknown>, key: string): string | undefined {
  const value = runtimeEnv[key];
  return typeof value === "string" ? value.trim() || undefined : undefined;
}
