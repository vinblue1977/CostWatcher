import { useCallback, useEffect, useState } from "react";
import { SignedOut } from "../api";
import { friendly, money, monthName, previousMonth, shortResource, tagCoverage } from "../model";
import type { Day } from "../model";
import type { Untagged } from "../api";
import { Empty } from "./parts";

type Load = { kind: "loading" } | { kind: "error"; month: string; message: string } | { kind: "ready"; month: string; data: Untagged };

export function Tags({ days, load }: { days: Day[]; load: (month: string) => Promise<Untagged> }) {
  const latest = days.reduce((max, d) => (d.date > max ? d.date : max), "").slice(0, 7);
  const [last, setLast] = useState(false);
  const month = latest && last ? previousMonth(latest) : latest;
  const [state, setState] = useState<Load>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    if (!month) return;
    let current = true;
    setState({ kind: "loading" });
    load(month).then(
      (data) => current && setState({ kind: "ready", month, data }),
      (error: unknown) => {
        if (error instanceof SignedOut) return window.location.reload(); // the app then shows its sign-in screen
        if (current) setState({ kind: "error", month, message: error instanceof Error ? error.message : "something went wrong" });
      },
    );
    return () => {
      current = false;
    };
  }, [load, month, attempt]);

  if (days.length === 0) return <Empty />;
  const coverage = tagCoverage(days, month);
  const shown: Load = state.kind !== "loading" && state.month !== month ? { kind: "loading" } : state; // never a stale month's rows
  const untaggedPct = coverage ? 100 - Math.round(coverage.pct) : 0;
  return (
    <>
      <p className="lead">
        {monthName(month)}
        {!last && " so far"} · <strong>{coverage ? money(coverage.total) : "$0.00"}</strong> total
      </p>

      {coverage ? (
        <section className="panel" aria-labelledby="cov-h">
          <h2 id="cov-h">Tag coverage</h2>
          <p className="muted">Share of this month's cost that carries a project tag</p>
          <div className="coverage">
            <span className="pct">{Math.round(coverage.pct)}%</span>
            <span className="muted">
              tagged · {money(coverage.untagged)} ({untaggedPct}%) has no project tag
            </span>
          </div>
          <div
            className="covbar"
            role="img"
            aria-label={`${Math.round(coverage.pct)} percent tagged, ${untaggedPct} percent untagged`}
          >
            <span className="tagged" style={{ width: `${coverage.pct}%` }} />
            <span className="untagged" style={{ width: `${100 - coverage.pct}%` }} />
          </div>
          <ul className="legend">
            <li>
              <span className="swatch tagged" />
              Tagged {money(coverage.tagged)}
            </li>
            <li>
              <span className="swatch untagged" />
              Untagged {money(coverage.untagged)}
            </li>
          </ul>
        </section>
      ) : (
        <p className="alert soft">No cost days for {monthName(month)} yet.</p>
      )}

      <section className="panel" aria-labelledby="top-h">
        <div className="panel-head">
          <div>
            <h2 id="top-h">Top untagged resources</h2>
            <p className="muted">Tag these first to shrink "unattributed". {last ? "Last month" : "Month to date"}, largest first.</p>
          </div>
          <div role="group" aria-label="Month" className="tabs">
            <button type="button" aria-pressed={!last} onClick={() => setLast(false)}>
              This month
            </button>
            <button type="button" aria-pressed={last} onClick={() => setLast(true)}>
              Last month
            </button>
          </div>
        </div>
        <Resources state={shown} retry={retry} />
      </section>

      <p className="alert soft">
        Costs with no resource ID (data transfer, support, credits) cannot be traced to a resource, so they are listed on one
        line. New tags apply to costs from the day they are added; earlier spend stays untagged.
      </p>
    </>
  );
}

function Resources({ state, retry }: { state: Load; retry: () => void }) {
  if (state.kind === "loading") return <p className="muted" role="status">Loading…</p>;
  if (state.kind === "error") {
    return (
      <p className="muted" role="alert">
        {state.message}{" "}
        <button type="button" className="quiet" onClick={retry}>Try again</button>
      </p>
    );
  }
  const { resources, otherCount, otherCost } = state.data;
  if (resources.length === 0) return <p className="muted">Nothing without a project tag in this month's data.</p>;
  const denominator = resources.reduce((sum, r) => sum + r.cost, otherCost) || 1; // this list's own total, so shares add up
  return (
    <>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Resource</th>
              <th scope="col">Service</th>
              <th scope="col" className="share-col">Share of untagged</th>
              <th scope="col" className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {resources.map((r, i) => (
              <tr key={`${i}|${r.id}`}>
                <td className="mono" title={r.id || undefined}>{shortResource(r.id)}</td>
                <td>{friendly(r.service)}</td>
                <td className="share-col">
                  <span className="track">
                    <span className="fill warn" style={{ width: `${Math.min(100, Math.max(0, (r.cost / denominator) * 100))}%` }} />
                  </span>
                </td>
                <td className="num">{money(r.cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {otherCount > 0 && <p className="muted">and {otherCount} smaller · {money(otherCost)}</p>}
    </>
  );
}
