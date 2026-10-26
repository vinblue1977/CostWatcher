// Pure functions over the daily items the API returns. The alert thresholds match backend/ingest/analysis.py.

export interface Day {
  date: string;
  total: number;
  services: Record<string, number>;
  projects: Record<string, number>;
  usageTypes: Record<string, number>;
}
export type Group = "services" | "projects" | "usageTypes";
export type Range = "7d" | "14d" | "30d" | "MTD";

export const RANGES: Range[] = ["7d", "14d", "30d", "MTD"];
const RANGE_DAYS: Record<string, number> = { "7d": 7, "14d": 14, "30d": 30 };
export const TRAILING_DAYS = 7;
const MIN_PRIOR_DAYS = 5; // of the 7 before the last week; fewer would make every change look huge
const SPIKE_FACTOR = 2;
const SPIKE_MIN = 0.5;
export const OTHER = "Other";
export const UNATTRIBUTED = "unattributed";

export function shiftDate(date: string, days: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function sortDays(days: Day[]): Day[] {
  return [...days].sort((a, b) => a.date.localeCompare(b.date));
}

/** The days inside a range that ends on the newest day we have (there is no same-day figure). */
export function inRange(days: Day[], range: Range): Day[] {
  const sorted = sortDays(days);
  const latest = sorted.at(-1);
  if (!latest) return [];
  if (range === "MTD") return sorted.filter((d) => d.date.startsWith(latest.date.slice(0, 7)));
  const from = shiftDate(latest.date, 1 - (RANGE_DAYS[range] ?? 14));
  return sorted.filter((d) => d.date >= from);
}

export function sumGroup(days: Day[], group: Group): Record<string, number> {
  const totals: Record<string, number> = {};
  for (const day of days) for (const [key, cost] of Object.entries(day[group])) totals[key] = (totals[key] ?? 0) + cost;
  return totals;
}

export function ranked(totals: Record<string, number>): [string, number][] {
  return Object.entries(totals).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

export interface Stack {
  names: string[]; // the top keys by cost, then "Other" when anything was folded in
  rows: { date: string; total: number; parts: number[] }[]; // parts line up with names
}

/** Daily cost split into the top `top` keys and everything else. Negative lines (credits) fold into the total only. */
export function stack(days: Day[], group: Group, top = 5): Stack {
  const names = ranked(sumGroup(days, group))
    .filter(([, cost]) => cost > 0)
    .slice(0, top)
    .map(([name]) => name);
  const rest = days.some((d) => Object.entries(d[group]).some(([k, cost]) => cost > 0 && !names.includes(k)));
  const all = rest ? [...names, OTHER] : names;
  const rows = days.map((day) => {
    const parts = all.map(() => 0);
    for (const [key, cost] of Object.entries(day[group])) {
      const at = names.indexOf(key);
      const slot = at >= 0 ? at : all.length - 1;
      if (cost > 0 && slot >= 0) parts[slot] = (parts[slot] ?? 0) + cost;
    }
    return { date: day.date, total: day.total, parts };
  });
  return { names: all, rows };
}

/** Cost of the last 7 days, of the 7 before, and the change between them (null with too few earlier days). */
export function weekOverWeek(days: Day[]): { recent: number; prior: number; pct: number | null } {
  const latest = sortDays(days).at(-1);
  if (!latest) return { recent: 0, prior: 0, pct: null };
  const split = shiftDate(latest.date, 1 - TRAILING_DAYS);
  const before = shiftDate(split, -TRAILING_DAYS);
  const sum = (list: Day[]) => list.reduce((total, d) => total + d.total, 0);
  const prior = days.filter((d) => d.date >= before && d.date < split);
  const recent = sum(days.filter((d) => d.date >= split));
  const was = sum(prior);
  return { recent, prior: was, pct: prior.length >= MIN_PRIOR_DAYS && was > 0 ? ((recent - was) / was) * 100 : null };
}

export interface Mover {
  name: string;
  delta: number;
  pct: number | null; // null when the key had no cost in the earlier week
  cost?: number; // the key's own cost, set by drivers()
}

/** Keys whose cost changed most between the last 7 days and the 7 before. Null until two weeks exist. */
export function movers(days: Day[], group: Group = "services", limit = 5): Mover[] | null {
  const sorted = sortDays(days);
  const latest = sorted.at(-1);
  if (!latest) return null;
  const split = shiftDate(latest.date, 1 - TRAILING_DAYS);
  const before = shiftDate(split, -TRAILING_DAYS);
  const recent = sumGroup(
    sorted.filter((d) => d.date >= split),
    group,
  );
  const prior = sumGroup(
    sorted.filter((d) => d.date >= before && d.date < split),
    group,
  );
  if (sorted.filter((d) => d.date >= before && d.date < split).length < MIN_PRIOR_DAYS) return null;
  const keys = new Set([...Object.keys(recent), ...Object.keys(prior)]);
  return [...keys]
    .map((name) => {
      const delta = (recent[name] ?? 0) - (prior[name] ?? 0);
      const was = prior[name] ?? 0;
      return { name, delta, pct: was > 0 ? (delta / was) * 100 : null };
    })
    .filter((m) => Math.abs(m.delta) >= 0.005)
    .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta) || a.name.localeCompare(b.name))
    .slice(0, limit);
}

export interface Forecast {
  spent: number;
  projected: number;
  low: number;
  high: number;
}

function daysInMonth(month: string): number {
  return new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0)).getUTCDate();
}

/** Month-end projection: spend so far plus the average day for the days left (range after a week of days). */
export function forecast(days: Day[]): Forecast | null {
  const sorted = sortDays(days);
  const latest = sorted.at(-1);
  if (!latest) return null;
  const month = latest.date.slice(0, 7);
  const mine = sorted.filter((d) => d.date.startsWith(month));
  const spent = mine.reduce((sum, d) => sum + d.total, 0);
  const left = daysInMonth(month) - Number(latest.date.slice(8));
  const mean = spent / mine.length;
  const projected = spent + mean * left;
  let spread = 0;
  if (mine.length >= TRAILING_DAYS) {
    const variance = mine.reduce((sum, d) => sum + (d.total - mean) ** 2, 0) / mine.length;
    spread = Math.sqrt(variance) * Math.sqrt(left);
  }
  return { spent, projected, low: Math.max(projected - spread, spent), high: projected + spread };
}

export interface Baseline {
  usual: number; // average total of the days before
  services: Record<string, number>;
  usageTypes: Record<string, number>;
}

/** The average of the 7 days before `date`, or null with fewer than 7 (the backend alerts wait the same way). */
export function baseline(days: Day[], date: string): Baseline | null {
  const before = sortDays(days)
    .filter((d) => d.date < date)
    .slice(-TRAILING_DAYS);
  if (before.length < TRAILING_DAYS) return null;
  const average = (group: Group) =>
    Object.fromEntries(Object.entries(sumGroup(before, group)).map(([k, v]) => [k, v / before.length]));
  return {
    usual: before.reduce((sum, d) => sum + d.total, 0) / before.length,
    services: average("services"),
    usageTypes: average("usageTypes"),
  };
}

export function isSpike(total: number, usual: number): boolean {
  return total > usual * SPIKE_FACTOR && total - usual > SPIKE_MIN;
}

/** Each key's own cost and signed change against its baseline, largest change first. Keys that cost something are kept even when unchanged. */
export function drivers(current: Record<string, number>, usual: Record<string, number>, limit = 6): Mover[] {
  const keys = new Set([...Object.keys(current), ...Object.keys(usual)]);
  return [...keys]
    .map((name) => ({ name, cost: current[name] ?? 0, delta: (current[name] ?? 0) - (usual[name] ?? 0), pct: null }))
    .filter((m) => Math.abs(m.delta) >= 0.005 || Math.abs(m.cost) >= 0.005)
    .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta) || a.name.localeCompare(b.name))
    .slice(0, limit);
}

/** The newest day in the list that spiked, with its baseline; null when none did. */
export function latestSpike(days: Day[], within = 14): { day: Day; base: Baseline } | null {
  const sorted = sortDays(days).slice(-within);
  for (const day of sorted.reverse()) {
    const base = baseline(days, day.date);
    if (base && isSpike(day.total, base.usual)) return { day, base };
  }
  return null;
}

/** "AmazonBedrock" -> "Amazon Bedrock", "AWSLambda" -> "AWS Lambda". Tag values and usage types pass through. */
export function friendly(name: string): string {
  return name.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
}

const REGION_CODES: Record<string, string> = {
  USE1: "us-east-1", USE2: "us-east-2", USW1: "us-west-1", USW2: "us-west-2", CAN1: "ca-central-1", SAE1: "sa-east-1",
  EUW1: "eu-west-1", EUW2: "eu-west-2", EUW3: "eu-west-3", EUC1: "eu-central-1", EUN1: "eu-north-1",
  APS1: "ap-southeast-1", APS2: "ap-southeast-2", APN1: "ap-northeast-1", APN2: "ap-northeast-2", APS3: "ap-south-1",
};
const CODES = Object.keys(REGION_CODES).join("|");
const PREFIXED = new RegExp(`^(${CODES})-(.+)$`);
const HOP = new RegExp(`^(${CODES})-AWS-(In|Out)-Bytes$`); // traffic between two regions
const HOME_REGION = "us-east-2";
const USAGE_NAMES: Record<string, string> = {
  "InputTokenCount-Units": "Input tokens", // Bedrock, billed through the marketplace: MP:<region>_ is dropped first
  "OutputTokenCount-Units": "Output tokens",
  "DataTransfer-Out-Bytes": "Data transfer out",
  "DataTransfer-In-Bytes": "Data transfer in",
  "DataTransfer-xAZ-Out-Bytes": "Data transfer between zones (out)",
  "DataTransfer-xAZ-In-Bytes": "Data transfer between zones (in)",
  "CloudFront-Out-Bytes": "CloudFront data out",
  "CloudFront-In-Bytes": "CloudFront data in",
  "Lambda-GB-Second": "Lambda compute",
  "Lambda-GB-Second-ARM": "Lambda compute (ARM)",
  Request: "Lambda requests",
  "Request-ARM": "Lambda requests (ARM)",
  "TimedStorage-ByteHrs": "Storage",
  "TimedPITRStorage-ByteHrs": "Point-in-time recovery storage",
  "EBS:SnapshotArchiveStorage": "EBS snapshot archive storage",
  ApiGatewayHttpRequest: "API Gateway HTTP requests",
  "AWSSecretsManager-Secrets": "Secrets Manager secrets",
  AWSSecretsManagerAPIRequest: "Secrets Manager API requests",
  APIRequest: "API requests",
  "KMS-Requests": "KMS requests",
  StateTransition: "State transitions",
  "XRay-TracesStored": "X-Ray traces stored",
  CognitoEssentialsMAU: "Cognito monthly active users",
  "VendedLog-Bytes": "Vended logs",
  ScheduledInvocation: "Scheduled invocations",
};

/** "USE2-MP:USE2_InputTokenCount-Units" -> "Input tokens". Names outside the home region say which one; unknown names are tidied, never hidden. */
export function usageLabel(name: string): string {
  const prefixed = PREFIXED.exec(name);
  const named = /^((?:us|eu|ap|ca|sa)-[a-z]+-\d)-(.+)$/.exec(name);
  const region = prefixed ? REGION_CODES[prefixed[1] ?? ""] : named?.[1];
  const rest = ((prefixed ?? named)?.[2] ?? name).replace(/^MP:[A-Z0-9]+_/, "");
  const hop = HOP.exec(rest);
  const label = hop
    ? `Data transfer ${hop[2] === "In" ? "from" : "to"} ${REGION_CODES[hop[1] ?? ""]}`
    : (USAGE_NAMES[rest] ??
      friendly(rest.replace(/[:-]+/g, " "))
        .split(/\s+/)
        .filter(Boolean)
        .map((word, i) => (i === 0 || word === word.toUpperCase() ? word : word.toLowerCase())) // sentence case, acronyms kept
        .join(" ")
        .replace(/^./, (c) => c.toUpperCase()));
  return region && region !== HOME_REGION ? `${label} (${region})` : label;
}

/** An API stage with a known project reads "<project> API". An ARN without its partition, service, region and account, and without a "table/" or "inference-profile/" type prefix ("Orders"); other IDs pass through; no ID gets a label. */
export function shortResource(id: string, project?: string): string {
  if (id === "" || id.startsWith("(")) return "(no resource ID)";
  const parts = id.split(":");
  if (parts[0] !== "arn" || parts.length <= 5 || !parts[5]) return id;
  const resource = parts.slice(5).join(":");
  if (project && parts[2] === "apigateway" && /^\/apis\/[a-z0-9]+\/stages\//.test(resource)) return `${project} API`; // the API ID alone says nothing
  return resource.replace(/^(inference-profile|table)\//, "") || resource; // never an empty name
}

export function money(value: number, digits = 2): string {
  const sign = value < 0 ? "-" : "";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function signedMoney(value: number): string {
  return `${value < 0 ? "-" : "+"}${money(Math.abs(value))}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function shortDate(date: string): string {
  return `${MONTHS[Number(date.slice(5, 7)) - 1]} ${Number(date.slice(8))}`;
}

export interface Coverage {
  month: string;
  total: number;
  tagged: number;
  untagged: number;
  pct: number; // share of the cost that carries a project tag
}

/** How much of a month's cost has a project tag, from the daily items. Null when the month has no days. */
export function tagCoverage(days: Day[], month: string): Coverage | null {
  const mine = days.filter((d) => d.date.startsWith(month));
  if (mine.length === 0) return null;
  const total = mine.reduce((sum, d) => sum + d.total, 0);
  const untagged = Math.min(Math.max(sumGroup(mine, "projects")[UNATTRIBUTED] ?? 0, 0), Math.max(total, 0));
  const tagged = total - untagged;
  return { month, total, tagged, untagged, pct: total > 0 ? (tagged / total) * 100 : 100 };
}

export function previousMonth(month: string): string {
  return new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)) - 2, 1)).toISOString().slice(0, 7);
}

const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
export function monthName(month: string): string {
  return `${MONTH_NAMES[Number(month.slice(5, 7)) - 1]} ${month.slice(0, 4)}`;
}

/** One free-tier allowance for the current month, as the ingest stores it from the AWS Free Tier API. */
export interface FreeTierUsage {
  service: string;
  operation: string;
  usageType: string;
  region: string;
  unit: string;
  type: string; // "Always Free", "12 Months Free", "Free Trial"
  limit: number;
  actual: number;
  forecast: number; // AWS's month-end projection, never below usage so far
}
export type Allowance = "over" | "watch" | "ok";

export function allowance(u: FreeTierUsage): Allowance {
  if (u.actual > u.limit) return "over";
  return u.forecast > u.limit ? "watch" : "ok";
}

/** Worst first (over, then projected to run out), then by how much of the limit is projected to be used. */
export function sortAllowances(usages: FreeTierUsage[]): FreeTierUsage[] {
  const order: Record<Allowance, number> = { over: 0, watch: 1, ok: 2 };
  return [...usages].sort(
    (a, b) => order[allowance(a)] - order[allowance(b)] || b.forecast / b.limit - a.forecast / a.limit || a.service.localeCompare(b.service),
  );
}

/** The date the month's pace reaches the limit (the same pace for every remaining day), or null when it will not within the month. */
export function runsOutOn(u: FreeTierUsage, month: string, today: number): string | null {
  const perDay = u.actual / Math.max(today, 1);
  if (perDay <= 0 || u.actual >= u.limit) return null;
  const day = today + Math.ceil((u.limit - u.actual) / perDay);
  const last = new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0)).getUTCDate();
  return day > last ? null : `${month}-${String(day).padStart(2, "0")}`;
}

/** "5.8", "820,000": whole numbers once large, two decimals when small. */
export function amount(value: number): string {
  return Math.abs(value) >= 100 ? Math.round(value).toLocaleString("en-US") : String(Math.round(value * 100) / 100);
}

/** Days until the UTC month containing `now` resets, counting today (on the last day it is 1). */
export function daysUntilReset(now: Date): number {
  const last = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0)).getUTCDate();
  return last - now.getUTCDate() + 1;
}
