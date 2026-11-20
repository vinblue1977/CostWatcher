import { useCallback, useEffect, useState } from "react";
import { SignedOut } from "../api";
import { checkTiles, filterFindings, findingSaving, money, regionOptions, scanDate, shortDate, unlistedFindings, WASTE_CHECKS, WASTE_REGIONS } from "../model";
import type { RegionFilter, WasteScan } from "../model";

type Load = { kind: "loading" } | { kind: "error"; message: string } | { kind: "ready"; data: WasteScan };

export function WastePage({ load }: { load: () => Promise<WasteScan> }) {
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
  return <Scan scan={state.data} />;
}

function Scan({ scan }: { scan: WasteScan }) {
  const [region, setRegion] = useState<RegionFilter>("all");
  if (!scan.scannedAt) {
    return (
      <section className="panel empty">
        <h2>No scan yet</h2>
        <p className="muted">The waste scan runs on Sundays at 15:00 UTC. Nothing is wrong; the first results will appear here after it has run.</p>
      </section>
    );
  }
  const errors = scan.errors ?? [];
  const total = scan.total ?? 0;
  const listed = filterFindings(scan.findings ?? [], region);
  const unlisted = unlistedFindings(scan);
  const newCount = scan.newCount ?? 0;
  const failed = errors.length > 0 && (
    <p className="alert soft" role="alert">Could not check: {errors.join("; ")}. Results for those checks may be missing.</p>
  );
  if (total === 0) {
    return (
      <>
        {failed}
        <section className="panel empty">
          <h2>Nothing wasteful found</h2>
          <p className="muted">
            The scan on {scanDate(scan.scannedAt)} checked {WASTE_CHECKS.length} things in {WASTE_REGIONS.join(" and ")} and found
            no idle or unused resources{errors.length > 0 ? ", apart from the checks that failed" : ""}.
          </p>
        </section>
      </>
    );
  }
  return (
    <>
      <p className="lead">
        Scanned {scanDate(scan.scannedAt)} · <strong>{total} {total === 1 ? "finding" : "findings"}</strong> · about{" "}
        <strong>{money(scan.monthly ?? 0)}/month</strong> if fixed · <strong>{newCount} new</strong> since last week
      </p>
      {failed}

      <section className="panel" aria-labelledby="c-h">
        <h2 id="c-h">What the scan checks</h2>
        <p className="muted">One tile per check. Amber means something was found.</p>
        <ul className="checks">
          {checkTiles(scan).map((t) => (
            <li key={t.id} className={`check ${t.count > 0 ? "hit" : "clear"}`}>
              <b>{t.label}</b>
              <span>{t.detail}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel" aria-labelledby="f-h">
        <div className="panel-head">
          <div>
            <h2 id="f-h">Findings</h2>
            <p className="muted">Largest estimated saving first. Estimates use list prices.</p>
          </div>
          <div role="group" aria-label="Region" className="tabs">
            <button type="button" aria-pressed={region === "all"} onClick={() => setRegion("all")}>Both regions</button>
            {regionOptions(scan.findings ?? []).map((r) => (
              <button key={r} type="button" aria-pressed={region === r} onClick={() => setRegion(r)}>{r}</button>
            ))}
          </div>
        </div>
        {listed.length === 0 ? (
          <p className="muted">{(scan.findings ?? []).length === 0 ? "No findings are listed." : `No listed findings in ${region}.`}</p>
        ) : (
          <div className="table-wrap">
            <table className="findings">
              <thead>
                <tr>
                  <th scope="col">Finding</th>
                  <th scope="col">Resource</th>
                  <th scope="col">Region</th>
                  <th scope="col">First seen</th>
                  <th scope="col" className="num">Saves</th>
                </tr>
              </thead>
              <tbody>
                {listed.map((f) => {
                  const saving = findingSaving(f);
                  return (
                    <tr key={f.key}>
                      <td className="what">
                        {f.summary}
                        {f.new && <> <span className="pill new">New</span></>}
                        <div className="why">{f.detail}</div>
                      </td>
                      <td className="mono res" title={f.id}>{f.id}</td>
                      <td className="region">{f.region}</td>
                      <td className="since">{f.firstSeen ? shortDate(f.firstSeen) : ""}</td>
                      <td className="num"><span className={`pill${saving.cost ? " cost" : ""}`}>{saving.text}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {unlisted > 0 && (
          <p className="muted">
            and {unlisted} more not listed here (the smallest estimates; the page keeps the top {scan.findings?.length ?? 0}).
          </p>
        )}
      </section>

      <p className="alert soft">
        Findings are suggestions: nothing is changed or deleted for you. The scan is read-only, runs Sundays at 15:00 UTC, and
        emails new findings once.
      </p>
    </>
  );
}
