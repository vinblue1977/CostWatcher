"""The analysis rules on small hand-built days: forecast, drivers and each alert."""

from decimal import Decimal
from typing import Any

from ingest import analysis

D = Decimal


def day(total: str, services: dict[str, str] | None = None, usage: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "total": D(total),
        "services": {k: D(v) for k, v in (services or {}).items()},
        "projects": {},
        "usageTypes": {k: D(v) for k, v in (usage or {}).items()},
    }


def week(total: str = "1", services: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Seven steady days, 2026-10-01 to 2026-10-07."""
    return {f"2026-10-0{i}": day(total, services or {"AmazonS3": total}) for i in range(1, 8)}


def test_forecast_projects_the_average_day_over_the_days_left() -> None:
    result = analysis.forecast({"2026-10-01": day("2"), "2026-10-02": day("4")}, "2026-10")
    assert result is not None
    assert result["spent"] == 6 and result["projected"] == 93  # $3 a day over 31 days
    assert result["low"] == result["high"] == 93  # no range before a week of days


def test_forecast_range_opens_with_a_week_of_varying_days() -> None:
    days = {f"2026-10-0{i}": day(str(i)) for i in range(1, 8)}
    result = analysis.forecast(days, "2026-10")
    assert result is not None
    assert result["low"] < result["projected"] < result["high"] and result["low"] >= result["spent"]


def test_forecast_of_a_finished_month_is_what_was_spent() -> None:
    result = analysis.forecast({"2026-09-29": day("1"), "2026-09-30": day("2")}, "2026-09")
    assert result is not None and result["projected"] == result["spent"] == 3
    assert analysis.forecast({}, "2026-10") is None


def test_drivers_rank_signed_changes_and_skip_unchanged() -> None:
    moved = analysis.drivers({"a": D(5), "b": D(1), "c": D(2)}, {"a": D(1), "b": D(1), "d": D(3)})
    assert moved == [("a", D(4)), ("d", D(-3)), ("c", D(2))]


def test_quiet_account_raises_nothing() -> None:
    assert analysis.alerts(week(), D(100)) == []


def test_alerts_wait_for_a_week_of_history() -> None:
    days = {"2026-10-01": day("1"), "2026-10-02": day("50", {"New": "50"})}
    assert analysis.alerts(days, D(1000)) == []


def test_spike_names_the_usage_types_that_moved() -> None:
    days = week()
    days["2026-10-08"] = day("9", {"AmazonS3": "1", "AmazonVPC": "8"}, {"NatGateway-Bytes": "8"})
    spike = next(a for a in analysis.alerts(days, D(1000)) if a.key == "spike#2026-10-08")
    assert "NatGateway-Bytes +$8.00" in spike.message


def test_a_small_jump_on_pennies_is_not_a_spike() -> None:
    days = week("0.10")
    days["2026-10-08"] = day("0.40", {"AmazonS3": "0.40"})
    assert analysis.alerts(days, D(1000)) == []


def test_service_rise_needs_both_the_factor_and_the_dollars() -> None:
    days = week("2", {"AmazonS3": "1", "AWSLambda": "1"})
    days["2026-10-08"] = day("2", {"AmazonS3": "1", "AWSLambda": "1.4"})  # +40%: under the factor
    assert analysis.alerts(days, D(1000)) == []
    days["2026-10-08"] = day("3.5", {"AmazonS3": "1", "AWSLambda": "2.5"})  # 2.5x and +$1.50
    keys = [a.key for a in analysis.alerts(days, D(1000))]
    assert keys == ["service-up#2026-10-08#AWSLambda"]


def test_first_billed_service_is_flagged_once_per_name() -> None:
    days = week()
    days["2026-10-08"] = day("1.3", {"AmazonS3": "1", "AmazonKinesis": "0.3"})
    assert [a.key for a in analysis.alerts(days, D(1000))] == ["new-service#AmazonKinesis"]


def test_budget_alert_fires_when_the_projection_passes_it() -> None:
    alert = analysis.alerts(week("1"), D(20))[0]  # $1 a day is $31 for the month
    assert alert.key == "budget#2026-10" and "$31.00" in alert.message
    assert analysis.alerts(week("1"), D(31)) == []


def test_budget_alert_waits_for_a_week_of_data() -> None:
    assert analysis.alerts({"2026-10-02": day("50")}, D(5)) == []


def test_digest_compares_the_week_with_the_one_before_and_forecasts_the_month() -> None:
    days = {f"2026-10-{i:02d}": day("1", {"AmazonS3": "1"}) for i in range(1, 8)}
    days |= {f"2026-10-{i:02d}": day("3", {"AmazonS3": "1", "AmazonEC2": "2"}) for i in range(8, 15)}
    result = analysis.digest(days, D(50))
    assert result is not None
    subject, message = result
    assert subject == "CostWatcher weekly: $21.00 over the last 7 days"
    assert "2026-10-08 to 2026-10-14: $21.00." in message
    assert "The 7 days before: $7.00, so up $14.00 (+200%)." in message
    assert "Biggest changes by service: AmazonEC2 +$14.00." in message
    assert "Top services: AmazonEC2 $14.00, AmazonS3 $7.00." in message
    assert "2026-10 so far $28.00; projected $62.00" in message and "over the $50.00 budget" in message


def test_digest_with_less_than_two_weeks_skips_the_comparison() -> None:
    result = analysis.digest(week(), D(50))
    assert result is not None
    assert "The 7 days before" not in result[1] and "within the $50.00 budget" in result[1]
    assert analysis.digest({}, D(50)) is None


def test_digest_reports_a_drop_and_a_zero_prior_week() -> None:
    drop = {f"2026-10-{i:02d}": day("2" if i < 8 else "1", {"S": "1"}) for i in range(1, 15)}
    zero = {f"2026-10-{i:02d}": day("0" if i < 8 else "1", {"S": "1"}) for i in range(1, 15)}
    assert "down $7.00 (-50%)" in (analysis.digest(drop, D(99)) or ("", ""))[1]
    assert "so up $7.00." in (analysis.digest(zero, D(99)) or ("", ""))[1]
