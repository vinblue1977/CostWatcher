import { accessToken, forgetTokens } from "./auth";
import type { Config } from "./auth";
import type { Day } from "./model";

export class SignedOut extends Error {}

interface MonthResponse {
  month: string;
  days: Day[];
}

export interface UntaggedResource {
  id: string; // "(no resource ID)" for data transfer, support and similar items
  service: string;
  cost: number;
  project?: string; // the project tag of an API stage, looked up by the ingest
}
export interface Untagged {
  month: string;
  resources: UntaggedResource[];
  otherCount: number;
  otherCost: number;
}

async function get<T>(config: Config, token: string, path: string): Promise<T> {
  const response = await fetch(`${config.apiUrl}/${path}`, { headers: { Authorization: `Bearer ${token}` } });
  if (response.status === 401) {
    forgetTokens();
    throw new SignedOut();
  }
  if (!response.ok) throw new Error(`could not load ${path} (${response.status})`);
  return (await response.json()) as T;
}

async function month(config: Config, token: string, key: string): Promise<Day[]> {
  return (await get<MonthResponse>(config, token, `costs/${key}`)).days;
}

/** The month's costliest resources without a project tag. */
export async function loadUntagged(config: Config, key: string): Promise<Untagged> {
  const token = await accessToken(config);
  if (!token) throw new SignedOut();
  return get<Untagged>(config, token, `tags/${key}`);
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
