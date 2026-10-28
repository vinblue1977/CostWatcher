import { ageLabel, clock, freshLevel, shortDate, stamp } from "./model";
import type { Day } from "./model";
import type { Freshness as Times } from "./api";

/** The newest day is partial while it is still today in UTC, the day AWS bills by. */
function partialDay(days: Day[], now: Date): string | null {
  const latest = days[days.length - 1]?.date;
  return latest && latest === now.toISOString().slice(0, 10) ? shortDate(latest) : null;
}

function Badge({ exportAt, now }: { exportAt: string; now: Date }) {
  const level = freshLevel(exportAt, now);
  const age = ageLabel(exportAt, now);
  return (
    <span className={`fresh ${level}`}>
      <i aria-hidden="true" />
      {level === "stale" ? `Billing data is ${age}` : <>Billing data as of {clock(exportAt)} <span className="t">· {age}</span></>}
    </span>
  );
}

/** A pill in the top bar on desktop, and on phones a strip under the header that opens the details. */
export function Freshness({ variant, times, days, now = new Date() }: { variant: "pill" | "strip"; times: Times | null; days: Day[]; now?: Date }) {
  if (!times?.exportAt) return null;
  if (variant === "pill") return <div className="desktop-only"><Badge exportAt={times.exportAt} now={now} /></div>;
  const partial = partialDay(days, now);
  return (
      <details className={`fresh-strip phone-only ${freshLevel(times.exportAt, now)}`}>
        <summary><Badge exportAt={times.exportAt} now={now} /></summary>
        <p>AWS delivers the billing export up to 3 times a day, at times it picks. CostWatcher reads it every 3 hours, so the AWS console can be ahead.</p>
        <dl>
          <dt>Export delivered</dt><dd>{stamp(times.exportAt)}</dd>
          {times.ingestAt && <><dt>Last checked</dt><dd>{stamp(times.ingestAt)}</dd></>}
          {partial && <><dt>Latest day</dt><dd>{partial}, partial</dd></>}
        </dl>
      </details>
  );
}
