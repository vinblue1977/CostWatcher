import { accessToken, forgetTokens } from "./auth";
import type { Config } from "./auth";
import type { Day } from "./model";

export class SignedOut extends Error {}

interface MonthResponse {
  month: string;
  days: Day[];
}

async function month(config: Config, token: string, key: string): Promise<Day[]> {
  const response = await fetch(`${config.apiUrl}/costs/${key}`, { headers: { Authorization: `Bearer ${token}` } });
  if (response.status === 401) {
    forgetTokens();
    throw new SignedOut();
  }
  if (!response.ok) throw new Error(`could not load ${key} (${response.status})`);
  return ((await response.json()) as MonthResponse).days;
}

/** The current UTC month and the one before, enough for a 30-day range and the 7-versus-7 comparison. */
export async function loadDays(config: Config, now = new Date()): Promise<Day[] | null> {
  const token = await accessToken(config);
  if (!token) return null;
  const current = now.toISOString().slice(0, 7);
  const previous = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1)).toISOString().slice(0, 7);
  // The previous month only widens the 30-day range and the weekly comparison, so its failure is not fatal.
  const [before, after] = await Promise.all([
    month(config, token, previous).catch((error: unknown) => {
      if (error instanceof SignedOut) throw error;
      return [] as Day[];
    }),
    month(config, token, current),
  ]);
  return [...before, ...after];
}
