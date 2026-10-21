import { useCallback, useEffect, useMemo, useState } from "react";
import { loadDays, loadUntagged, SignedOut } from "./api";
import type { Untagged } from "./api";
import { handleRedirect, loadConfig, signIn, signOut } from "./auth";
import type { Config } from "./auth";
import { Breakdown } from "./pages/Breakdown";
import { Overview } from "./pages/Overview";
import { Spikes } from "./pages/Spikes";
import { Tags } from "./pages/Tags";
import { RANGES, sortDays } from "./model";
import type { Day, Range } from "./model";

type State =
  | { kind: "loading" }
  | { kind: "signedOut"; config: Config }
  | { kind: "error"; message: string }
  | { kind: "ready"; config: Config; days: Day[] };

const PAGES = [
  { path: "/", label: "Overview", icon: "M4 18V11M11 18V5M18 18V9" },
  { path: "/breakdown", label: "Breakdown", icon: "M3 3h9v9H3zM14 3h5v9h-5zM3 14h5v5H3zM10 14h9v5h-9z" },
  { path: "/spikes", label: "Spikes", icon: "M2 13h4l3-8 4 12 3-4h4" },
  { path: "/tags", label: "Tags", icon: "M3 3h8l8 8-8 8-8-8zM7.5 7.5h.01" },
] as const;

function usePath(): string {
  const read = () => window.location.hash.replace(/^#/, "") || "/";
  const [path, setPath] = useState(read);
  useEffect(() => {
    const onChange = () => setPath(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return path;
}

export function App() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [range, setRange] = useState<Range>("14d");
  const path = usePath();

  const load = useCallback(async () => {
    try {
      const config = await loadConfig();
      await handleRedirect(config);
      const days = await loadDays(config);
      setState(days ? { kind: "ready", config, days: sortDays(days) } : { kind: "signedOut", config });
    } catch (error) {
      if (error instanceof SignedOut) {
        const config = await loadConfig().catch(() => null);
        if (config) return setState({ kind: "signedOut", config });
      }
      setState({ kind: "error", message: error instanceof Error ? error.message : "something went wrong" });
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  if (state.kind === "loading") return <p className="status" role="status">Loading…</p>;
  if (state.kind === "error") {
    return (
      <main className="gate">
        <h1>CostWatcher</h1>
        <p role="alert">{state.message}</p>
        <button type="button" className="primary" onClick={() => void load()}>Try again</button>
      </main>
    );
  }
  if (state.kind === "signedOut") {
    return (
      <main className="gate">
        <h1>CostWatcher</h1>
        <p>Your AWS spend, by service and project.</p>
        <button type="button" className="primary" onClick={() => void signIn(state.config)}>Sign in</button>
      </main>
    );
  }
  return <Shell state={state} path={path} range={range} setRange={setRange} />;
}

type Ready = Extract<State, { kind: "ready" }>;

export function Shell({ state, path, range, setRange, loadTags }: { state: Ready; path: string; range: Range; setRange: (r: Range) => void; loadTags?: (month: string) => Promise<Untagged> }) {
  const page = PAGES.find((p) => p.path === path)?.path ?? "/";
  const config = state.config;
  const load = useMemo(() => loadTags ?? ((month: string) => loadUntagged(config, month)), [loadTags, config]);
  const body = useMemo(() => {
    if (page === "/breakdown") return <Breakdown days={state.days} range={range} />;
    if (page === "/spikes") return <Spikes days={state.days} />;
    if (page === "/tags") return <Tags days={state.days} load={load} />;
    return <Overview days={state.days} range={range} budget={state.config.budgetUsd} />;
  }, [page, state, range, load]);
  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true">
            <rect x="2" y="14" width="5" height="10" rx="1" fill="#8AB4FF" />
            <rect x="10" y="8" width="5" height="16" rx="1" fill="#4FD6C0" />
            <rect x="18" y="2" width="5" height="22" rx="1" fill="#FFB454" />
          </svg>
          <span>CostWatcher</span>
        </div>
        <nav aria-label="Primary" className="nav">
          {PAGES.map((p) => (
            <a key={p.path} href={`#${p.path}`} aria-current={p.path === page ? "page" : undefined}>
              <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d={p.icon} />
              </svg>
              {p.label}
            </a>
          ))}
        </nav>
        <div className="controls">
          {page !== "/spikes" && page !== "/tags" && (
            <>
              <div role="group" aria-label="Date range" className="segmented">
                {RANGES.map((r) => (
                  <button key={r} type="button" aria-pressed={r === range} onClick={() => setRange(r)}>
                    {r}
                  </button>
                ))}
              </div>
              <label className="range-select">
                <span className="sr">Date range</span>
                <select value={range} onChange={(e) => setRange(RANGES.find((r) => r === e.target.value) ?? range)}>
                  {RANGES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </label>
            </>
          )}
          <button type="button" className="quiet desktop-only" onClick={() => signOut(state.config)}>Sign out</button>
        </div>
      </header>
      <main className="page">
        {body}
        <button type="button" className="quiet phone-only" onClick={() => signOut(state.config)}>Sign out</button>
      </main>
    </div>
  );
}
