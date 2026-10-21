import { useMemo, useState } from "react";
import { Legend, StackedBars } from "../charts";
import { baseline, drivers, forecast, friendly, inRange, latestSpike, money, movers, shortDate, signedMoney, stack, sumGroup, usageLabel, weekOverWeek } from "../model";
import type { Baseline, Day, Group, Range } from "../model";
import { Empty, Note } from "./parts";

const GROUPS: { value: Group; label: string }[] = [
  { value: "services", label: "Service" },
  { value: "projects", label: "Project" },
  { value: "usageTypes", label: "Usage type" },
];

export function Overview({ days, range, budget }: { days: Day[]; range: Range; budget?: number | undefined }) {
  const [group, setGroup] = useState<Group>("services");
  const shown = useMemo(() => inRange(days, range), [days, range]);
  const data = useMemo(() => stack(shown, group), [shown, group]);
  const bases = useMemo(() => new Map<string, Baseline | null>(shown.map((d) => [d.date, baseline(days, d.date)])), [shown, days]);
  const fc = useMemo(() => forecast(days), [days]);
  const spike = useMemo(() => latestSpike(days), [days]);
  const moved = useMemo(() => movers(days, group), [days, group]);
  const labelOf = group === "usageTypes" ? usageLabel : friendly;
  if (days.length === 0) return <Empty />;

  const latest = days.at(-1);
  const { recent: weekCost, pct: change } = weekOverWeek(days);
  const average = shown.length ? shown.reduce((sum, d) => sum + d.total, 0) / shown.length : 0;
  const worst = spike && drivers(spike.day.services, spike.base.services, 1)[0];

  return (
    <>
      {spike && (
        <section className="alert" aria-label="Alert">
          <span className="dot" />
          <p>
            <strong>
              {shortDate(spike.day.date)} cost {money(spike.day.total)}
            </strong>
            , {(spike.day.total / spike.base.usual).toFixed(1)}× the trailing 7-day average ({money(spike.base.usual)}).
            {worst && ` ${friendly(worst.name)} was ${signedMoney(worst.delta)} from normal.`}
          </p>
          <a href="#/spikes">See what changed →</a>
        </section>
      )}

      <section className="figures" aria-label="Key figures">
        <div className="card">
          <div className="label">Month to date</div>
          <div className="value">{money(fc?.spent ?? 0)}</div>
          <div className="sub">{latest && `${shortDate(`${latest.date.slice(0, 7)}-01`)} – ${shortDate(latest.date)}`}</div>
        </div>
        <div className="card">
          <div className="label">Forecast for the month</div>
          <div className="value">{fc ? money(fc.projected) : "—"}</div>
          <div className="sub">
            {fc && fc.high > fc.low ? `Range ${money(fc.low, 0)} – ${money(fc.high, 0)}` : "Straight-line projection"}
            {budget ? ` · budget ${money(budget, 0)}` : ""}
          </div>
        </div>
        <div className="card">
          <div className="label">Last 7 days</div>
          <div className="value">{money(weekCost)}</div>
          <div className="sub">
            {change === null ? (
              "Needs two weeks of data to compare"
            ) : (
              <>
                <span className={change > 0 ? "up" : "down"}>
                  {change > 0 ? "+" : ""}
                  {change.toFixed(0)}%
                </span>{" "}
                vs prior 7 days
              </>
            )}
          </div>
        </div>
        <div className="card">
          <div className="label">Average day</div>
          <div className="value">{money(average)}</div>
          <div className="sub">over the selected {range === "MTD" ? "month to date" : range}</div>
        </div>
      </section>

      <section className="panel" aria-labelledby="chart-h">
        <div className="panel-head">
          <div>
            <h2 id="chart-h">Daily cost by {GROUPS.find((g) => g.value === group)?.label.toLowerCase()}</h2>
            <p className="muted">
              {shown[0] && shortDate(shown[0].date)} – {shown.at(-1) && shortDate(shown.at(-1)!.date)} · stacked, USD
            </p>
          </div>
          <label className="select">
            <span>Group by</span>
            <select value={group} onChange={(e) => setGroup(e.target.value as Group)}>
              {GROUPS.map((g) => (
                <option key={g.value} value={g.value}>
                  {g.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <Legend names={data.names} labelOf={labelOf} />
        <StackedBars data={data} bases={bases} labelOf={labelOf} />
      </section>

      <div className="two">
        <section className="panel" aria-labelledby="movers-h">
          <h2 id="movers-h">Top movers</h2>
          <p className="muted">Last 7 days vs the 7 days before</p>
          {moved === null ? (
            <p className="muted">Movers appear once there are two weeks of data.</p>
          ) : moved.length === 0 ? (
            <p className="muted">Nothing changed.</p>
          ) : (
            <ul className="movers">
              {moved.map((m) => (
                <li key={m.name}>
                  <span className="name">{labelOf(m.name)}</span>
                  <span className="amount">{signedMoney(m.delta)}</span>
                  <span className="share">{m.pct === null ? "new" : `${m.pct > 0 ? "+" : ""}${m.pct.toFixed(0)}%`}</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel" aria-labelledby="fc-h">
          <h2 id="fc-h">Forecast{budget ? " and budget" : ""}</h2>
          {fc ? <ForecastBar spent={fc.spent} projected={fc.projected} budget={budget} /> : null}
          <p className="muted">
            Straight-line from the days so far; the range opens once a week of days exists, so early in a month or right
            after sign-up it is only a rough guide.
          </p>
        </section>
      </div>
      <Note>
        {Object.keys(sumGroup(shown, "services")).length} services in this range. Billing data arrives up to a day late and
        can be revised until the month closes.
      </Note>
    </>
  );
}

function ForecastBar({ spent, projected, budget }: { spent: number; projected: number; budget?: number | undefined }) {
  const scale = Math.max(budget ?? 0, projected, 0.01);
  return (
    <>
      <div className="meter" role="img" aria-label={`${money(spent)} spent, ${money(projected)} projected${budget ? `, budget ${money(budget, 0)}` : ""}`}>
        <div className="spent" style={{ width: `${(spent / scale) * 100}%` }} />
        <div className="rest" style={{ left: `${(spent / scale) * 100}%`, width: `${Math.max(0, (projected - spent) / scale) * 100}%` }} />
        {budget ? <div className="limit" style={{ left: `${Math.min(100, (budget / scale) * 100)}%` }} /> : null}
      </div>
      <div className="meter-legend">
        <span>
          <b className="blue">{money(spent)}</b> spent
        </span>
        <span>
          <b>{money(projected)}</b> projected{budget ? ` (${Math.round((projected / budget) * 100)}% of budget)` : ""}
        </span>
        {budget ? <span>{money(budget, 0)} budget</span> : null}
      </div>
    </>
  );
}
