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
    monkeypatch.setenv("BUDGET_USD", "20")
    monkeypatch.setenv("TOPIC_ARN", "arn:topic")


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


def test_stuck_query_is_cancelled_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    athena = FakeAthena("RUNNING", "RUNNING", pages=[])
    stopped: list[dict[str, Any]] = []
    athena.stop_query_execution = lambda **kwargs: stopped.append(kwargs)  # type: ignore[attr-defined]
    monkeypatch.setattr(ingest, "POLL_SECONDS", 0)
    monkeypatch.setattr(ingest, "QUERY_SECONDS", -1)
    with pytest.raises(RuntimeError, match="cancelled"):
        ingest._query(athena, "SELECT 1")
    assert stopped == [{"QueryExecutionId": "q1"}]


def test_non_finite_cost_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        ingest._days([dict(zip(HEADER, ["2026-10-01", "AmazonS3", "Storage", "web", "NaN"], strict=True))])


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
    assert ingest.handler({"months": ["2026-10"]}, None) == {"written": {"2026-10": 1}, "alerts": []}
    assert dynamo.puts[0]["TableName"] == "costs" and dynamo.puts[0]["Item"]["SK"] == {"S": "DAY#2026-10-01"}


class Marked:
    """A DynamoDB stand-in that refuses a marker put twice, as the real conditional put does."""

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.deleted: list[dict[str, Any]] = []
        self.queried: list[str] = []
        self.stored: dict[str, list[dict[str, Any]]] = {}

    def put_item(self, **kwargs: Any) -> None:
        key = kwargs["Item"]["SK"]["S"]
        if key in self.keys:
            error = Exception("exists")
            error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}  # type: ignore[attr-defined]
            raise error
        self.keys.add(key)

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queried.append(kwargs["ExpressionAttributeValues"][":pk"]["S"])
        return {"Items": self.stored.get(self.queried[-1], [])}

    def delete_item(self, **kwargs: Any) -> None:
        self.deleted.append(kwargs["Key"])
        self.keys.discard(kwargs["Key"]["SK"]["S"])


class Sns:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[dict[str, Any]] = []
        self.fail = fail

    def publish(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("sns down")
        self.published.append(kwargs)


def _steady(total: str = "1") -> dict[str, dict[str, Any]]:
    return {
        f"2026-10-0{i}": {"total": Decimal(total), "services": {"S": Decimal(total)}, "projects": {}, "usageTypes": {}}
        for i in range(1, 8)
    }


def test_alert_is_published_once_across_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    sns, dynamo = Sns(), Marked()
    monkeypatch.setattr(ingest, "_client", lambda name: sns)
    today = dt.date(2026, 10, 8)
    assert ingest._notify(_steady(), today, dynamo) == ["budget#2026-10"]
    assert ingest._notify(_steady(), today, dynamo) == []  # the marker is already there
    assert len(sns.published) == 1 and sns.published[0]["TopicArn"] == "arn:topic"


def test_failed_publish_releases_the_marker_so_the_next_run_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    dynamo = Marked()
    monkeypatch.setattr(ingest, "_client", lambda name: Sns(fail=True))
    with pytest.raises(RuntimeError, match="sns down"):
        ingest._notify(_steady(), dt.date(2026, 10, 8), dynamo)
    assert dynamo.keys == set() and len(dynamo.deleted) == 1


def test_old_data_never_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    sns = Sns()
    monkeypatch.setattr(ingest, "_client", lambda name: sns)
    dynamo = Marked()
    assert ingest._notify(_steady(), dt.date(2026, 10, 20), dynamo) == []
    assert sns.published == [] and dynamo.queried == []  # a stale export never reaches the history lookup


def _stored(date: str, total: str) -> dict[str, Any]:
    return {
        "SK": {"S": f"DAY#{date}"},
        "total": {"N": total},
        "services": {"M": {"S": {"N": total}}},
        "projects": {"M": {}},
        "usageTypes": {"M": {"u": {"N": total}}},
    }


def test_early_month_alerts_use_last_months_stored_days(monkeypatch: pytest.MonkeyPatch) -> None:
    sns, dynamo = Sns(), Marked()
    monkeypatch.setattr(ingest, "_client", lambda name: sns)
    dynamo.stored["COST#2026-09"] = [_stored(f"2026-09-{d}", "1") for d in range(24, 31)]
    today = {"2026-10-01": {"total": Decimal("1"), "services": {"S": Decimal("1")}, "projects": {}, "usageTypes": {}}}
    today["2026-10-02"] = {
        "total": Decimal("5"),
        "services": {"S": Decimal("5")},
        "projects": {},
        "usageTypes": {"u": Decimal("5")},
    }
    assert ingest._notify(today, dt.date(2026, 10, 3), dynamo) == ["spike#2026-10-02", "service-up#2026-10-02#S"]
    assert dynamo.queried == ["COST#2026-09"]


def test_a_full_baseline_skips_the_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    dynamo = Marked()
    monkeypatch.setattr(ingest, "_client", lambda name: Sns())
    ingest._notify(_steady() | {"2026-10-08": _steady()["2026-10-01"]}, dt.date(2026, 10, 9), dynamo)
    assert dynamo.queried == []


def test_stored_days_are_the_newest_few_read_consistently() -> None:
    class One:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def query(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"Items": [_stored("2026-09-30", "3"), _stored("2026-09-29", "2")]}

    dynamo = One()
    days = ingest._stored_days(dynamo, "2026-09")
    assert days["2026-09-29"]["total"] == Decimal(2) and days["2026-09-30"]["usageTypes"] == {"u": Decimal(3)}
    assert dynamo.calls[0]["ScanIndexForward"] is False and dynamo.calls[0]["Limit"] == 7 and dynamo.calls[0]["ConsistentRead"]


def test_a_failed_history_lookup_still_sends_the_alerts_in_hand(monkeypatch: pytest.MonkeyPatch) -> None:
    sns, dynamo = Sns(), Marked()
    monkeypatch.setattr(ingest, "_client", lambda name: sns)

    def broken(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("AccessDenied")

    dynamo.query = broken  # type: ignore[method-assign]
    assert ingest._notify(_steady(), dt.date(2026, 10, 8), dynamo) == ["budget#2026-10"]


def test_an_empty_previous_month_leaves_the_baseline_short(monkeypatch: pytest.MonkeyPatch) -> None:
    sns, dynamo = Sns(), Marked()
    monkeypatch.setattr(ingest, "_client", lambda name: sns)
    day = {"total": Decimal("5"), "services": {"S": Decimal("5")}, "projects": {}, "usageTypes": {}}
    assert ingest._notify({"2026-10-01": day, "2026-10-02": day}, dt.date(2026, 10, 3), dynamo) == []
    assert dynamo.queried == ["COST#2026-09"]
