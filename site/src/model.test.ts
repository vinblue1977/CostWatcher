import { describe, expect, it } from "vitest";
import { baseline, drivers, forecast, friendly, inRange, isSpike, latestSpike, money, monthName, movers, previousMonth, shiftDate, shortResource, stack, tagCoverage, weekOverWeek } from "./model";
import type { Day } from "./model";

function day(date: string, total: number, services: Record<string, number> = { S3: total }, usageTypes: Record<string, number> = {}): Day {
  return { date, total, services, projects: {}, usageTypes };
}
const steady = (n: number, total = 1, from = "2026-10-01") =>
  Array.from({ length: n }, (_, i) => day(shiftDate(from, i), total));

describe("ranges", () => {
  it("ends on the newest day and crosses month boundaries", () => {
    const days = steady(40, 1, "2026-09-01"); // Sep 1 to Oct 10
    expect(inRange(days, "7d").map((d) => d.date)).toEqual(Array.from({ length: 7 }, (_, i) => shiftDate("2026-10-04", i)));
    expect(inRange(days, "MTD")).toHaveLength(10);
    expect(inRange([], "14d")).toEqual([]);
  });
});

describe("stack", () => {
  it("keeps the top keys and folds the rest into Other", () => {
    const days = [day("2026-10-01", 10, { A: 5, B: 3, C: 1, D: 0.5, E: 0.3, F: 0.2 })];
    const s = stack(days, "services", 3);
    expect(s.names).toEqual(["A", "B", "C", "Other"]);
    expect(s.rows[0]?.parts).toEqual([5, 3, 1, 1]);
  });
  it("has no Other column when everything fits", () => {
    expect(stack([day("2026-10-01", 1)], "services").names).toEqual(["S3"]);
  });
});

describe("movers", () => {
  it("compares the last 7 days with the 7 before and waits for the earlier week", () => {
    expect(movers(steady(7))).toBeNull();
    const days = [...steady(7, 1, "2026-10-01"), ...Array.from({ length: 7 }, (_, i) => day(shiftDate("2026-10-08", i), 2, { S3: 1, AmazonBedrock: 1 }))];
    const result = movers(days);
    expect(result?.[0]).toMatchObject({ name: "AmazonBedrock", delta: 7, pct: null });
    expect(result?.find((m) => m.name === "S3")).toBeUndefined(); // unchanged
  });
});

describe("forecast", () => {
  it("projects the average day over the days left", () => {
    const f = forecast([day("2026-10-01", 2), day("2026-10-02", 4)]);
    expect(f).toMatchObject({ spent: 6, projected: 93, low: 93, high: 93 });
  });
  it("opens a range after a week of varying days and never goes below what is spent", () => {
    const f = forecast(Array.from({ length: 7 }, (_, i) => day(shiftDate("2026-10-01", i), i + 1)));
    expect(f && f.low < f.projected && f.projected < f.high && f.low >= f.spent).toBe(true);
  });
  it("uses only the newest month", () => {
    const f = forecast([day("2026-09-30", 100), day("2026-10-01", 1)]);
    expect(f?.spent).toBe(1);
  });
  it("is null with no days", () => expect(forecast([])).toBeNull());
});

describe("spikes", () => {
  it("needs a week of history, then compares with its average", () => {
    expect(baseline(steady(7), "2026-10-07")).toBeNull();
    expect(baseline(steady(7), "2026-10-08")?.usual).toBe(1);
  });
  it("ignores pennies and small rises", () => {
    expect(isSpike(0.4, 0.1)).toBe(false);
    expect(isSpike(1.9, 1)).toBe(false);
    expect(isSpike(4.08, 1.04)).toBe(true);
  });
  it("finds the newest spike and what drove it", () => {
    const days = [...steady(7), day("2026-10-08", 9, { S3: 1, AmazonVPC: 8 }), day("2026-10-09", 1)];
    const found = latestSpike(days);
    expect(found?.day.date).toBe("2026-10-08");
    const moved = drivers(found!.day.services, found!.base.services);
    expect(moved[0]).toMatchObject({ name: "AmazonVPC", delta: 8 });
  });
});

describe("formatting", () => {
  it("splits service codes into words", () => {
    expect(friendly("AmazonBedrock")).toBe("Amazon Bedrock");
    expect(friendly("AWSLambda")).toBe("AWS Lambda");
    expect(friendly("web")).toBe("web");
  });
  it("formats money with the sign in front", () => {
    expect(money(1234.5)).toBe("$1,234.50");
    expect(money(-0.5)).toBe("-$0.50");
  });
});

describe("week over week", () => {
  it("compares the last 7 days with the 7 before and waits for enough earlier days", () => {
    expect(weekOverWeek(steady(10)).pct).toBeNull(); // 3 earlier days
    const days = [...steady(7, 1, "2026-10-01"), ...steady(7, 2, "2026-10-08")];
    expect(weekOverWeek(days)).toMatchObject({ recent: 14, prior: 7, pct: 100 });
  });
  it("shows no movers on a thin earlier week", () => {
    expect(movers(steady(9))).toBeNull();
  });
});

describe("tag coverage", () => {
  const tagged = (date: string, total: number, untagged: number): Day => ({ ...day(date, total), projects: { web: total - untagged, unattributed: untagged } });
  it("splits a month's cost into tagged and untagged, and ignores other months", () => {
    const days = [tagged("2026-09-30", 100, 100), tagged("2026-10-01", 10, 3), tagged("2026-10-02", 10, 1)];
    const result = tagCoverage(days, "2026-10");
    expect(result).toMatchObject({ total: 20, untagged: 4, tagged: 16 });
    expect(result?.pct).toBeCloseTo(80);
  });
  it("is null with no days and fully tagged when nothing was spent", () => {
    expect(tagCoverage([], "2026-10")).toBeNull();
    expect(tagCoverage([day("2026-10-01", 0)], "2026-10")?.pct).toBe(100);
  });
  it("never reports more untagged than the total (credits)", () => {
    expect(tagCoverage([tagged("2026-10-01", 5, 9)], "2026-10")).toMatchObject({ untagged: 5, tagged: 0, pct: 0 });
  });
});

describe("months", () => {
  it("steps back across a year and names the month", () => {
    expect(previousMonth("2026-10")).toBe("2026-09");
    expect(previousMonth("2027-01")).toBe("2026-12");
    expect(monthName("2026-10")).toBe("October 2026");
  });
});

describe("shortResource", () => {
  it("drops the partition, service, region and account from an ARN", () => {
    expect(shortResource("arn:aws:dynamodb:us-east-2:235316001927:table/Orders")).toBe("table/Orders");
    expect(shortResource("arn:aws:bedrock:us-east-2:235316001927:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0")).toBe("inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0");
    expect(shortResource("arn:aws:secretsmanager:us-east-2:235316001927:secret:google/secrets-CX4YJR")).toBe("secret:google/secrets-CX4YJR");
  });
  it("leaves other IDs alone and labels a missing one", () => {
    expect(shortResource("cdk-hnb659fds-assets-235316001927-us-east-2")).toBe("cdk-hnb659fds-assets-235316001927-us-east-2");
    expect(shortResource("arn:aws:s3:::my-bucket")).toBe("my-bucket"); // S3 ARNs have no region or account
    expect(shortResource("arn:aws:s3:::")).toBe("arn:aws:s3:::"); // nothing after the account part: keep it whole
    expect(shortResource("arn:aws:iam")).toBe("arn:aws:iam");
    expect(shortResource("")).toBe("(no resource ID)");
    expect(shortResource("(no resource ID)")).toBe("(no resource ID)");
  });
});
