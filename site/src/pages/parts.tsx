import type { ReactNode } from "react";

export function Empty() {
  return (
    <section className="panel empty">
      <h2>No cost data yet</h2>
      <p className="muted">
        The first billing export can take up to 24 hours to arrive, and the daily import runs at 08:30 UTC. Nothing is
        wrong; there is just nothing to show, which is not the same as $0.
      </p>
    </section>
  );
}

export function Note({ children }: { children: ReactNode }) {
  return <p className="note">{children}</p>;
}
