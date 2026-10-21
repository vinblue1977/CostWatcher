import { useMemo, useState } from "react";
import { baseline, drivers, friendly, inRange, isSpike, latestSpike, money, shortDate, signedMoney, usageLabel } from "../model";
import type { Day } from "../model";
import { Empty } from "./parts";

export function Spikes({ days }: { days: Day[] }) {
  const shown = useMemo(() => inRange(days, "14d"), [days]);
  const initial = useMemo(() => latestSpike(days)?.day.date ?? shown.at(-1)?.date ?? "", [days, shown]);
  const [picked, setPicked] = useState<string | null>(null);
  if (days.length === 0) return <Empty />;

  const date = picked ?? initial;
  const day = days.find((d) => d.date === date);
  const base = day ? baseline(days, day.date) : null;
  const max = Math.max(0.01, ...shown.map((d) => d.total));
  const spiked = day && base ? isSpike(day.total, base.usual) : false;
  const changed = day && base ? drivers(day.services, base.services) : [];
  const usage = day && base ? drivers(day.usageTypes, base.usageTypes, 8) : [];
  const biggest = Math.max(0.01, ...changed.map((c) => Math.abs(c.delta)));

  return (
    <>
      <section className="panel" aria-labelledby="pick-h">
        <div className="panel-head">
          <h2 id="pick-h">Pick a day</h2>
          <span className="muted">daily total, USD</span>
        </div>
        <div className="picker">
          {shown.map((d) => {
            const b = baseline(days, d.date);
            const hot = b ? isSpike(d.total, b.usual) : false;
            return (
              <button
                key={d.date}
                type="button"
                aria-pressed={d.date === date}
                aria-label={`${shortDate(d.date)}: ${money(d.total)}${hot ? ", spike" : ""}`}
                className={hot ? "hot" : undefined}
                onClick={() => setPicked(d.date)}
              >
                <span className="column" style={{ height: `${(Math.max(0, d.total) / max) * 100}%` }} />
                <span className="tick">{Number(d.date.slice(8))}</span>
              </button>
            );
          })}
        </div>
      </section>

      {day && (
        <>
          <section className="figures" aria-label="Summary">
            <div className="card">
              <div className="label">{shortDate(day.date)}</div>
              <div className={`value${spiked ? " warn" : ""}`}>{money(day.total)}</div>
              <div className="sub">{base && base.usual > 0 ? `${(day.total / base.usual).toFixed(1)}× the trailing 7-day average` : "Total for the day"}</div>
            </div>
            <div className="card">
              <div className="label">Normal for this account</div>
              <div className="value">{base ? money(base.usual) : "—"}</div>
              <div className="sub">{base ? "average of the 7 days before" : "needs 7 earlier days"}</div>
            </div>
            <div className="card">
              <div className="label">Difference</div>
              <div className="value">{base ? signedMoney(day.total - base.usual) : "—"}</div>
              <div className="sub">{spiked ? "over twice normal" : "against normal"}</div>
            </div>
          </section>

          {base ? (
            <div className="two">
              <section className="panel" aria-labelledby="drv-h">
                <h2 id="drv-h">What changed</h2>
                <p className="muted">{shortDate(day.date)} vs the trailing 7-day average, by service</p>
                <ul className="ranked">
                  {changed.map((c) => (
                    <li key={c.name}>
                      <span className="name">{friendly(c.name)}</span>
                      <span className="track">
                        <span className={`fill ${c.delta < 0 ? "neg" : ""}`} style={{ width: `${(Math.abs(c.delta) / biggest) * 100}%` }} />
                      </span>
                      <span className="amount">{signedMoney(c.delta)}</span>
                    </li>
                  ))}
                </ul>
              </section>
              <section className="panel" aria-labelledby="use-h">
                <h2 id="use-h">Usage types that moved</h2>
                <p className="muted">Whatever was billed differently that day</p>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">Usage type</th>
                        <th scope="col" className="num">Change</th>
                      </tr>
                    </thead>
                    <tbody>
                      {usage.map((u) => (
                        <tr key={u.name}>
                          <td>{usageLabel(u.name)}</td>
                          <td className="num">{signedMoney(u.delta)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            </div>
          ) : (
            <section className="panel">
              <p className="muted">Comparisons start once there are 7 earlier days of data.</p>
            </section>
          )}
        </>
      )}
      <p className="note">Alerts arrive after a day is billed, so the earliest notice is the next morning.</p>
    </>
  );
}
