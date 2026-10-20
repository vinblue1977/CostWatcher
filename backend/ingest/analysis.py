"""Cost analysis over the daily items the ingest builds: month-end forecast, spike drivers and alert rules. No AWS calls."""

import calendar
import statistics
from decimal import Decimal
from typing import Any, NamedTuple

Day = dict[str, Any]  # as built by the ingest: total, services, projects, usageTypes (all Decimal)

TRAILING_DAYS = 7  # the baseline for "normal"; every rule needs this much history before it speaks
SPIKE_FACTOR = Decimal(2)  # a day over this multiple of the baseline average is a spike
SPIKE_MIN = Decimal("0.50")  # ... and over this many dollars above it, so pennies don't page anyone
SERVICE_FACTOR = Decimal("1.5")
SERVICE_MIN = Decimal("1.00")  # a service up by this many dollars and by SERVICE_FACTOR
DRIVERS = 3


class Alert(NamedTuple):
    key: str  # stable per event, so a repeat run can tell it already sent this one
    subject: str
    message: str


def forecast(days: dict[str, Day], month: str) -> dict[str, Decimal] | None:
    """Month-end projection from the days so far: spend to date plus the average day for the days left.

    The range needs a week of days; before that low and high equal the projection. Returns None with no days.
    """
    if not days:
        return None
    elapsed = int(max(days)[8:])  # the last day with data; there is no same-day figure
    spent = sum((d["total"] for d in days.values()), Decimal(0))
    left = calendar.monthrange(int(month[:4]), int(month[5:]))[1] - elapsed
    projected = spent + spent / elapsed * left
    spread = Decimal(0)
    if len(days) >= TRAILING_DAYS:
        # Days vary independently, so the spread over the days left grows with their square root.
        spread = statistics.pstdev(d["total"] for d in days.values()) * Decimal(left).sqrt()
    return {"spent": spent, "projected": projected, "low": max(projected - spread, spent), "high": projected + spread}


def drivers(current: dict[str, Decimal], baseline: dict[str, Decimal], limit: int = DRIVERS) -> list[tuple[str, Decimal]]:
    """The keys (usage types, services) that moved most against the baseline, as signed dollar changes."""
    changes = {k: current.get(k, Decimal(0)) - baseline.get(k, Decimal(0)) for k in current.keys() | baseline.keys()}
    ranked = sorted(((k, c) for k, c in changes.items() if c), key=lambda kc: (-abs(kc[1]), kc[0]))
    return ranked[:limit]


def _average(days: list[Day], group: str) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for day in days:
        for key, cost in day[group].items():
            totals[key] = totals.get(key, Decimal(0)) + cost
    return {key: cost / len(days) for key, cost in totals.items()}


def _describe(changes: list[tuple[str, Decimal]]) -> str:
    return ", ".join(f"{key} {'+' if change > 0 else '-'}${abs(change):.2f}" for key, change in changes)


def alerts(days: dict[str, Day], budget: Decimal) -> list[Alert]:
    """Alerts for the latest day in `days` (keyed by date, any months): spike, service rise, new service, budget."""
    if not days:
        return []
    latest = max(days)
    found: list[Alert] = []
    month = latest[:7]
    projection = forecast({d: v for d, v in days.items() if d.startswith(month)}, month)
    if projection and projection["projected"] > budget:
        found.append(
            Alert(
                f"budget#{month}",
                f"CostWatcher: {month} is on track to pass ${budget:.2f}",
                f"Projected ${projection['projected']:.2f} for {month} (range ${projection['low']:.2f} to "
                f"${projection['high']:.2f}) against a ${budget:.2f} budget; ${projection['spent']:.2f} spent through {latest}.",
            )
        )

    history = [days[d] for d in sorted(days) if d < latest][-TRAILING_DAYS:]
    if len(history) < TRAILING_DAYS:
        return found
    today = days[latest]

    usual = sum((d["total"] for d in history), Decimal(0)) / len(history)
    if today["total"] > usual * SPIKE_FACTOR and today["total"] - usual > SPIKE_MIN:
        moved = drivers(today["usageTypes"], _average(history, "usageTypes"))
        found.append(
            Alert(
                f"spike#{latest}",
                f"CostWatcher: ${today['total']:.2f} on {latest}, over twice the usual",
                f"{latest} cost ${today['total']:.2f} against a ${usual:.2f} average for the last {len(history)} days. "
                f"Biggest changes: {_describe(moved)}.",
            )
        )

    usual_services = _average(history, "services")
    for service, cost in sorted(today["services"].items()):
        before = usual_services.get(service, Decimal(0))
        if before <= 0:
            if cost > 0 and not any(service in d["services"] for d in history):
                found.append(
                    Alert(
                        f"new-service#{service}",
                        f"CostWatcher: {service} was billed for the first time",
                        f"{service} cost ${cost:.2f} on {latest} and has no charges in the last {len(history)} days.",
                    )
                )
        elif cost > before * SERVICE_FACTOR and cost - before > SERVICE_MIN:
            found.append(
                Alert(
                    f"service-up#{latest}#{service}",
                    f"CostWatcher: {service} is up to ${cost:.2f} on {latest}",
                    f"{service} cost ${cost:.2f} on {latest} against a ${before:.2f} average for the last {len(history)} days.",
                )
            )
    return found
