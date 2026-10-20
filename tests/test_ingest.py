"""The ingest Lambda: which months it re-reads, the query it sends, and how Athena rows become daily items."""

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest
from ingest import handler as ingest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABLE_NAME", "costs")
    monkeypatch.setenv("ATHENA_WORKGROUP", "wg")
    monkeypatch.setenv("ATHENA_DATABASE", "costwatcher")
    monkeypatch.setenv("ATHENA_TABLE", "cur")


class FakeAthena:
    def __init__(self, *states: str, pages: list[list[list[str]]]) -> None:
        self.states = list(states)
        self.pages = pages
        self.started: list[dict[str, Any]] = []

    def start_query_execution(self, **kwargs: Any) -> dict[str, str]:
        self.started.append(kwargs)
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        return {"QueryExecution": {"Status": {"State": self.states.pop(0), "StateChangeReason": "boom"}}}

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        index = int(kwargs.get("NextToken", 0))
        page: dict[str, Any] = {
            "ResultSet": {"Rows": [{"Data": [{"VarCharValue": v} for v in row]} for row in self.pages[index]]}
        }
        if index + 1 < len(self.pages):
            page["NextToken"] = str(index + 1)
        return page


HEADER = ["day", "service", "usage_type", "project", "cost"]


def test_mid_month_reads_only_this_month() -> None:
    assert ingest._months({}, dt.date(2026, 10, 20)) == ["2026-10"]


def test_early_in_a_month_also_rereads_the_last_one() -> None:
    assert ingest._months({}, dt.date(2026, 10, 3)) == ["2026-09", "2026-10"]
    assert ingest._months({}, dt.date(2027, 1, 1)) == ["2026-12", "2027-01"]


def test_months_can_be_given_but_must_be_valid() -> None:
    assert ingest._months({"months": ["2026-08"]}, dt.date(2026, 10, 20)) == ["2026-08"]
    with pytest.raises(ValueError, match="months"):
        ingest._months({"months": ["2026-13"]}, dt.date(2026, 10, 20))
    with pytest.raises(ValueError, match="months"):
        ingest._months({"months": ["2026-10' OR 1=1 --"]}, dt.date(2026, 10, 20))


def test_query_is_bound_to_one_partition_and_names_the_project_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    sql = ingest._sql("2026-10")
    assert "billing_period = '2026-10'" in sql and '"costwatcher"."cur"' in sql
    assert "element_at(resource_tags, 'user_project')" in sql  # a missing tag is NULL, not an error
    monkeypatch.setenv("PROJECT_TAG", "Cost Center")
    assert "'user_cost_center'" in ingest._sql("2026-10")


def test_rows_are_summed_per_day_service_project_and_usage_type() -> None:
    rows = [
        dict(zip(HEADER, ["2026-10-01", "AWSLambda", "Request", "web", "1.5"], strict=True)),
        dict(zip(HEADER, ["2026-10-01", "AWSLambda", "Duration", "unattributed", "0.25"], strict=True)),
        dict(zip(HEADER, ["2026-10-01", "AmazonS3", "Storage", "web", "2"], strict=True)),
        dict(zip(HEADER, ["2026-10-02", "AmazonS3", "Storage", "web", "-0.5"], strict=True)),  # credits are negative
    ]
    days = ingest._days(rows)
    assert days["2026-10-01"]["total"] == Decimal("3.75")
    assert days["2026-10-01"]["services"] == {"AWSLambda": Decimal("1.75"), "AmazonS3": Decimal(2)}
    assert days["2026-10-01"]["projects"] == {"web": Decimal("3.5"), "unattributed": Decimal("0.25")}
    assert days["2026-10-02"]["usageTypes"] == {"Storage": Decimal("-0.5")}


def test_item_is_keyed_for_the_api_and_rounds_money() -> None:
    day = ingest._days([dict(zip(HEADER, ["2026-10-01", "AmazonS3", "Storage", "web", "0.1234567"], strict=True))])["2026-10-01"]
    item = ingest._item("2026-10", "2026-10-01", day)
    assert item["PK"] == {"S": "COST#2026-10"} and item["SK"] == {"S": "DAY#2026-10-01"}
    assert item["total"] == {"N": "0.123457"}
    assert item["services"] == {"M": {"AmazonS3": {"N": "0.123457"}}}


def test_query_waits_reads_every_page_and_skips_the_header_once() -> None:
    athena = FakeAthena("RUNNING", "SUCCEEDED", pages=[[HEADER, ["d1", "s", "u", "p", "1"]], [["d2", "s", "u", "p", "2"]]])
    ingest.POLL_SECONDS = 0
    rows = ingest._query(athena, "SELECT 1")
    assert [r["day"] for r in rows] == ["d1", "d2"]
    assert athena.started == [{"QueryString": "SELECT 1", "WorkGroup": "wg"}]


def test_failed_query_raises_with_the_reason() -> None:
    ingest.POLL_SECONDS = 0
    with pytest.raises(RuntimeError, match="failed: boom"):
        ingest._query(FakeAthena("FAILED", pages=[]), "SELECT 1")


def test_handler_writes_one_item_per_day(monkeypatch: pytest.MonkeyPatch) -> None:
    athena = FakeAthena("SUCCEEDED", pages=[[HEADER, ["2026-10-01", "AmazonS3", "Storage", "web", "2"]]])

    class FakeDynamo:
        puts: list[dict[str, Any]] = []

        def put_item(self, **kwargs: Any) -> None:
            self.puts.append(kwargs)

    dynamo = FakeDynamo()
    monkeypatch.setattr(ingest, "_client", lambda name: athena if name == "athena" else dynamo)
    assert ingest.handler({"months": ["2026-10"]}, None) == {"written": {"2026-10": 1}}
    assert dynamo.puts[0]["TableName"] == "costs" and dynamo.puts[0]["Item"]["SK"] == {"S": "DAY#2026-10-01"}
