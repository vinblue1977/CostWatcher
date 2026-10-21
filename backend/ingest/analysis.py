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
    spent = sum((d["total"] for d in days.values()), Decimal(0))
    left = calendar.monthrange(int(month[:4]), int(month[5:]))[1] - int(max(days)[8:])
    projected = spent + spent / len(days) * left  # the average of the days we have, so a mid-month start isn't diluted
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
    month_days = {d: v for d, v in days.items() if d.startswith(month)}
    projection = forecast(month_days, month)
    # A day or two of data extrapolates wildly, and the alert is sent only once a month.
    if projection and projection["projected"] > budget and len(month_days) >= TRAILING_DAYS:
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


def _sum(days: list[Day], group: str) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for day in days:
        for key, cost in day[group].items():
            totals[key] = totals.get(key, Decimal(0)) + cost
    return totals


def digest(days: dict[str, Day], budget: Decimal) -> tuple[str, str] | None:
    """The weekly email: the last 7 days against the 7 before, top services and movers, and the month's forecast.

    `days` holds this month and the end of the last, keyed by date. Returns (subject, message), or None with no days.
    """
    if not days:
        return None
    dates = sorted(days)
    latest = dates[-1]
    week = [days[d] for d in dates[-TRAILING_DAYS:]]
    before = [days[d] for d in dates[-2 * TRAILING_DAYS : -TRAILING_DAYS]]
    spent = sum((d["total"] for d in week), Decimal(0))
    subject = f"CostWatcher weekly: ${spent:.2f} over the last {len(week)} days"
    lines = [f"{dates[-len(week)]} to {latest}: ${spent:.2f}."]
    if len(before) == TRAILING_DAYS:
        prior = sum((d["total"] for d in before), Decimal(0))
        change = spent - prior
        percent = f" ({change / prior:+.0%})" if prior else ""
        lines.append(f"The 7 days before: ${prior:.2f}, so {'up' if change >= 0 else 'down'} ${abs(change):.2f}{percent}.")
        moved = drivers(_sum(week, "services"), _sum(before, "services"))
        if moved:
            lines.append(f"Biggest changes by service: {_describe(moved)}.")
    top = sorted(_sum(week, "services").items(), key=lambda kc: (-kc[1], kc[0]))[:DRIVERS]
    if top:
        lines.append("Top services: " + ", ".join(f"{service} ${cost:.2f}" for service, cost in top) + ".")
    month = latest[:7]
    projection = forecast({d: v for d, v in days.items() if d.startswith(month)}, month)
    if projection:
        verdict = "over" if projection["projected"] > budget else "within"
        lines.append(
            f"{month} so far ${projection['spent']:.2f}; projected ${projection['projected']:.2f} "
            f"(range ${projection['low']:.2f} to ${projection['high']:.2f}), {verdict} the ${budget:.2f} budget."
        )
    return subject, "\n".join(lines)
