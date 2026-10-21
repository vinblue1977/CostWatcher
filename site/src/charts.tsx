import { OTHER, UNATTRIBUTED, friendly, isSpike, money, shortDate } from "./model";
import type { Baseline, Stack } from "./model";

const PALETTE = ["#8AB4FF", "#FFB454", "#4FD6C0", "#C39BFF", "#F27BA6"];
const GREY = "#7C8797";

/** The same name gets the same color on every chart: by its place in the list, grey for Other/unattributed. */
export function color(names: string[], name: string): string {
  if (name === OTHER || name === UNATTRIBUTED) return GREY;
  return PALETTE[names.filter((n) => n !== OTHER && n !== UNATTRIBUTED).indexOf(name)] ?? GREY; // grey past the fifth, never a repeat
}

export function Legend({ names }: { names: string[] }) {
  return (
    <ul className="legend">
      {names.map((name) => (
        <li key={name}>
          <span className="swatch" style={{ background: color(names, name) }} />
          {friendly(name)}
        </li>
      ))}
    </ul>
  );
}

export function StackedBars({ data, bases }: { data: Stack; bases: Map<string, Baseline | null> }) {
  const max = Math.max(0.01, ...data.rows.map((r) => r.total));
  const first = data.rows[0];
  const last = data.rows.at(-1);
  const label = `Stacked bars of daily cost by group from ${first ? shortDate(first.date) : ""} to ${last ? shortDate(last.date) : ""}. Largest day ${money(max)}.`;
  return (
    <div className="bars" role="img" aria-label={label}>
      <div className="plot">
        {data.rows.map((row) => {
          const base = bases.get(row.date);
          const spike = base ? isSpike(row.total, base.usual) : false;
          const stacked = row.parts.reduce((a, b) => a + b, 0); // credits fold into the total only, so the parts may not add up to it
          return (
            <div className="bar" key={row.date}>
              {spike && <span className="flag">{money(row.total)}</span>}
              <div className="stack" style={{ height: `${(stacked / max) * 100}%` }}>
                {row.parts.map((part, i) => {
                  const name = data.names[i] ?? OTHER;
                  return (
                    <div
                      key={name}
                      title={`${friendly(name)}: ${money(part)}`}
                      style={{ height: `${stacked > 0 ? (part / stacked) * 100 : 0}%`, background: color(data.names, name) }}
                    />
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
      <div className="axis">
        {data.rows.map((row, i) => (
          <span key={row.date}>{data.rows.length > 16 && i % 3 !== 0 ? "" : Number(row.date.slice(8))}</span>
        ))}
      </div>
    </div>
  );
}

export function RankedBars({ items, total, names }: { items: [string, number][]; total: number; names: string[] }) {
  const top = Math.max(0.01, ...items.map(([, cost]) => cost));
  return (
    <ul className="ranked">
      {items.map(([name, cost]) => (
        <li key={name}>
          <span className="swatch" style={{ background: color(names, name) }} />
          <span className="name">{friendly(name)}</span>
          <span className="track">
            <span className="fill" style={{ width: `${Math.max(0, (cost / top) * 100)}%`, background: color(names, name) }} />
          </span>
          <span className="amount">{money(cost)}</span>
          <span className="share">{total > 0 ? `${Math.round((cost / total) * 100)}%` : ""}</span>
        </li>
      ))}
    </ul>
  );
}
