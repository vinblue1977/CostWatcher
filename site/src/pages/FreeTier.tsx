import { useCallback, useEffect, useState } from "react";
import { SignedOut } from "../api";
import type { FreeTier } from "../api";
import { allowance, amount, daysUntilReset, monthName, runsOutOn, shortDate, sortAllowances, usageLabel } from "../model";
import type { Allowance, FreeTierUsage } from "../model";

type Load = { kind: "loading" } | { kind: "error"; message: string } | { kind: "ready"; data: FreeTier };

export function FreeTierPage({ load, now = new Date() }: { load: () => Promise<FreeTier>; now?: Date }) {
  const [state, setState] = useState<Load>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    let current = true;
    setState({ kind: "loading" });
    load().then(
      (data) => current && setState({ kind: "ready", data }),
      (error: unknown) => {
        if (error instanceof SignedOut) return window.location.reload(); // the app then shows its sign-in screen
        if (current) setState({ kind: "error", message: error instanceof Error ? error.message : "something went wrong" });
      },
    );
    return () => {
      current = false;
    };
  }, [load, attempt]);

  if (state.kind === "loading") return <p className="muted" role="status">Loading…</p>;
  if (state.kind === "error") {
    return (
      <p className="muted" role="alert">
        {state.message}{" "}
        <button type="button" className="quiet" onClick={retry}>Try again</button>
      </p>
    );
  }
  const { month, usages, total } = state.data;
  if (!month || usages.length === 0 || month !== now.toISOString().slice(0, 7)) {
    return (
      <section className="panel empty">
        <h2>No free-tier data for this month yet</h2>
        <p className="muted">
          {month && usages.length > 0
            ? `The last import was for ${monthName(month)}. The daily import (08:30 UTC) stores the new month's allowances; if this is still showing after a day, check the ingest alarm.`
            : "The daily import (08:30 UTC) stores your free-tier allowances from the AWS Free Tier API. Nothing is wrong; there is just nothing to show yet."}
        </p>
      </section>
    );
  }
  const today = now.getUTCDate();
  const sorted = sortAllowances(usages);
  const over = sorted.filter((u) => allowance(u) === "over");
  const watch = sorted.filter((u) => allowance(u) === "watch");
  const worst = over[0] ?? watch[0];
  const left = daysUntilReset(now);
  return (
    <>
      <p className="lead">
        {monthName(month)} · resets in <strong>{left} {left === 1 ? "day" : "days"}</strong>
      </p>

      <div className="figures">
        <div className="card">
          <div className="label">Allowances over the limit</div>
          <div className="value">{over.length} of {total}</div>
          <div className="sub">Billed once past the limit</div>
        </div>
        <div className={watch.length > 0 ? "card hot" : "card"}>
          <div className="label">Will run out before month end</div>
          <div className="value">{watch.length}</div>
          <div className="sub">At AWS's forecast for the month</div>
        </div>
      </div>

      {worst && (
        <p className="alert soft" role="status">
          {worst.service}{allowanceName(worst) === worst.service ? "" : ` (${allowanceName(worst)})`} {allowance(worst) === "over" ? "is over its free tier." : `is on pace to pass its free tier${soon(worst, month, today)}.`}
          {over.length + watch.length > 1 && ` ${over.length + watch.length - 1} more below.`}
        </p>
      )}

      <section className="panel" aria-labelledby="allow-h">
        <div className="panel-head">
          <div>
            <h2 id="allow-h">This month's allowances</h2>
            <p className="muted">Worst first. The tick is AWS's forecast for the end of the month.</p>
          </div>
        </div>
        <ul className="allowances">
          {sorted.map((u) => (
            <Row key={`${u.service}|${u.operation}|${u.usageType}|${u.region}|${u.type}`} u={u} month={month} today={today} />
          ))}
        </ul>
      </section>
    </>
  );
}

function allowanceName(u: FreeTierUsage): string {
  return u.usageType ? usageLabel(u.usageType) : u.service;
}

function soon(u: FreeTierUsage, month: string, today: number): string {
  const date = runsOutOn(u, month, today);
  return date ? ` and run out around ${shortDate(date)}` : "";
}

const LABELS: Record<Allowance, string> = { over: "Over", watch: "Runs out", ok: "On track" };

function Row({ u, month, today }: { u: FreeTierUsage; month: string; today: number }) {
  const status = allowance(u);
  const used = Math.min(100, (u.actual / u.limit) * 100);
  const projected = Math.min(100, (u.forecast / u.limit) * 100);
  const date = status === "watch" ? runsOutOn(u, month, today) : null;
  const pill = status === "over" ? `Over by ${amount(u.actual - u.limit)} ${u.unit}` : date ? `Runs out ${shortDate(date)}` : LABELS[status];
  return (
    <li className="allowance">
      <div className="who">
        <span className="title">{allowanceName(u)}</span>
        <span className="sub">
          {u.service} · {u.type || "Free tier"} · {amount(u.limit)} {u.unit}
        </span>
      </div>
      <span className={`pill ${status}`}>{pill}</span>
      <div className="allow-meter">
        <div
          className="gauge"
          role="img"
          aria-label={`${amount(u.actual)} of ${amount(u.limit)} ${u.unit} used, forecast ${amount(u.forecast)}`}
        >
          <span className={`fill ${status}`} style={{ width: `${used}%` }} />
          <span className="tick" style={{ left: `${projected}%` }} />
        </div>
        <div className="nums">
          <span>
            {amount(u.actual)} of {amount(u.limit)} {u.unit}
          </span>
          <span>Forecast {amount(u.forecast)}</span>
        </div>
      </div>
    </li>
  );
}
