import { useMemo } from "react";
import { RankedBars } from "../charts";
import { UNATTRIBUTED, friendly, inRange, money, ranked, shortDate, sumGroup, usageLabel } from "../model";
import type { Day, Group, Range } from "../model";
import { Empty, Note } from "./parts";

const SECTIONS: { group: Group; title: string; hint: string }[] = [
  { group: "projects", title: "By project", hint: "From the project cost allocation tag" },
  { group: "services", title: "By service", hint: "AWS service" },
  { group: "usageTypes", title: "By usage type", hint: "What was billed within the services" },
];

export function Breakdown({ days, range }: { days: Day[]; range: Range }) {
  const shown = useMemo(() => inRange(days, range), [days, range]);
  if (days.length === 0) return <Empty />;
  const total = shown.reduce((sum, d) => sum + d.total, 0);
  const projects = sumGroup(shown, "projects");
  const unattributed = projects[UNATTRIBUTED] ?? 0;
  return (
    <>
      <p className="lead">
        {shown[0] && shortDate(shown[0].date)} – {shown.at(-1) && shortDate(shown.at(-1)!.date)} · <strong>{money(total)}</strong>
      </p>
      {unattributed > 0.005 && total > 0 && (
        <p className="alert soft">
          {money(unattributed)} ({Math.round((unattributed / total) * 100)}%) is unattributed. Tag those resources with a
          project, and activate the tag in the Billing console, to move them into a project.
        </p>
      )}
      {SECTIONS.map(({ group, title, hint }) => {
        const items = ranked(sumGroup(shown, group)).filter(([, cost]) => Math.abs(cost) >= 0.005);
        const names = items.map(([name]) => name);
        return (
          <section className="panel" key={group} aria-labelledby={`${group}-h`}>
            <h2 id={`${group}-h`}>{title}</h2>
            <p className="muted">{hint}</p>
            <RankedBars items={items.slice(0, 10)} total={total} names={names} labelOf={group === "usageTypes" ? usageLabel : friendly} />
            {items.length > 10 && <p className="muted">and {items.length - 10} smaller</p>}
          </section>
        );
      })}
      <Note>Per-resource detail is not shown yet; it needs the export's resource IDs stored per day.</Note>
    </>
  );
}
