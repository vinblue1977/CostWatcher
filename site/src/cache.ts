// The last config and costs the app showed, so opening it paints at once and refreshes behind the scenes
// instead of waiting for config.json, a token refresh and the API (a cold Lambda) before showing anything.
// Same trust as the sign-in tokens beside it; signing out or losing the session clears it.

import type { Config } from "./auth";
import type { Day } from "./model";

const KEY = "costwatcher.snapshot";
const VERSION = 1; // bump when Day or Config change shape, so an old snapshot is dropped instead of crashing the first render

export interface Snapshot {
  config: Config;
  days: Day[];
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isDay = (d: unknown): d is Day =>
  isRecord(d) && typeof d.date === "string" && typeof d.total === "number" && isRecord(d.services) && isRecord(d.projects) && isRecord(d.usageTypes);

export function readSnapshot(): Snapshot | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { version?: number; config?: Config; days?: unknown[] } | null;
    if (!parsed || parsed.version !== VERSION || typeof parsed.config?.apiUrl !== "string" || !Array.isArray(parsed.days)) return null;
    if (!parsed.days.every(isDay)) {
      clearSnapshot();
      return null;
    }
    return { config: parsed.config, days: parsed.days };
  } catch {
    return null;
  }
}

export function saveSnapshot(snapshot: Snapshot): void {
  try {
    localStorage.setItem(KEY, JSON.stringify({ version: VERSION, ...snapshot }));
  } catch {
    // Storage can be blocked or full; the app just loads the slow way next time.
  }
}

export function clearSnapshot(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    // nothing to clear
  }
}
