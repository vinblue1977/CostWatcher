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


class DailyAndUntaggedAthena(FakeAthena):
    """Answers the daily query and then the untagged one, as the handler sends them for each month."""

    def __init__(self) -> None:
        super().__init__("SUCCEEDED", "SUCCEEDED", pages=[])

    def start_query_execution(self, **kwargs: Any) -> dict[str, str]:
        daily = [HEADER, ["2026-10-01", "AmazonS3", "Storage", "web", "2"]]
        untagged = [["resource", "service", "cost"], ["vol-1", "AmazonEC2", "1"]]
        self.pages = [daily if "AS day" in kwargs["QueryString"] else untagged]
        return super().start_query_execution(**kwargs)


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
    athena = DailyAndUntaggedAthena()

    class FakeDynamo:
        puts: list[dict[str, Any]] = []

        def put_item(self, **kwargs: Any) -> None:
            self.puts.append(kwargs)

    dynamo = FakeDynamo()
    monkeypatch.setattr(ingest, "_client", lambda name: athena if name == "athena" else dynamo)
    assert ingest.handler({"months": ["2026-10"]}, None) == {"written": {"2026-10": 1}, "alerts": []}
    assert dynamo.puts[0]["TableName"] == "costs" and dynamo.puts[0]["Item"]["SK"] == {"S": "DAY#2026-10-01"}
    assert [p["Item"]["PK"]["S"] for p in dynamo.puts] == ["COST#2026-10", "HYGIENE#2026-10"]  # the untagged summary too


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


def test_digest_publishes_the_weekly_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    sns, dynamo = Sns(), Marked()
    monkeypatch.setattr(ingest, "_client", lambda name: sns)
    dynamo.stored["COST#2026-09"] = [_stored(f"2026-09-{d}", "1") for d in range(24, 31)]
    dynamo.stored["COST#2026-10"] = [_stored(f"2026-10-0{d}", "2") for d in range(1, 9)]
    assert ingest._digest(dynamo, dt.date(2026, 10, 9)) is True
    assert sns.published[0]["Subject"] == "CostWatcher weekly: $14.00 over the last 7 days"
    assert sns.published[0]["TopicArn"] == "arn:topic" and dynamo.queried == ["COST#2026-09", "COST#2026-10"]


def test_digest_is_skipped_with_no_data_or_a_stale_export(monkeypatch: pytest.MonkeyPatch) -> None:
    sns, dynamo = Sns(), Marked()
    monkeypatch.setattr(ingest, "_client", lambda name: sns)
    assert ingest._digest(dynamo, dt.date(2026, 10, 9)) is False
    dynamo.stored["COST#2026-10"] = [_stored("2026-10-01", "2")]
    assert ingest._digest(dynamo, dt.date(2026, 10, 20)) is False
    assert sns.published == []


def test_the_digest_event_skips_the_athena_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "_client", lambda name: object())
    monkeypatch.setattr(ingest, "_digest", lambda client, today: True)
    assert ingest.handler({"digest": True}, None) == {"digest": True}


def test_untagged_query_is_bound_to_one_partition_and_keeps_only_untagged_cost() -> None:
    sql = ingest._untagged_sql("2026-10")
    assert "billing_period = '2026-10'" in sql and '"costwatcher"."cur"' in sql
    assert "NULLIF(element_at(resource_tags, 'user_project'), '') IS NULL" in sql and "HAVING SUM(" in sql
    with pytest.raises(ValueError, match="month"):
        ingest._untagged_sql("2026-10' OR 1=1 --")


def test_untagged_ranks_resources_and_counts_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "UNTAGGED_LISTED", 2)
    rows = [
        {"resource": "vol-1", "service": "AmazonEC2", "cost": "2"},
        {"resource": ingest.NO_RESOURCE, "service": "AWSSupport", "cost": "1"},
        {"resource": ingest.NO_RESOURCE, "service": "AmazonEC2", "cost": "2"},
        {"resource": ingest.NO_RESOURCE, "service": "AmazonS3", "cost": "0.5"},
        {"resource": "fn", "service": "AWSLambda", "cost": "0.25"},
        {"resource": "log", "service": "AmazonCloudWatch", "cost": "0.1"},
    ]
    summary = ingest._untagged(rows)
    assert summary["resources"] == [
        {"id": ingest.NO_RESOURCE, "service": "AmazonEC2, AWSSupport +1 more", "cost": Decimal("3.5")},
        {"id": "vol-1", "service": "AmazonEC2", "cost": Decimal(2)},
    ]
    assert summary["otherCount"] == 2 and summary["otherCost"] == Decimal("0.35")


def test_untagged_rejects_non_finite_costs() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        ingest._untagged([{"resource": "x", "service": "s", "cost": "NaN"}])


def test_untagged_item_has_its_own_key_and_rounds_money() -> None:
    summary = {
        "resources": [{"id": "vol-1", "service": "AmazonEC2", "cost": Decimal("1.5")}],
        "otherCount": 4,
        "otherCost": Decimal("0.3"),
    }
    item = ingest._untagged_item("2026-10", summary)
    assert item["PK"] == {"S": "HYGIENE#2026-10"} and item["SK"] == {"S": "UNTAGGED"}
    assert item["resources"]["L"][0]["M"]["cost"] == {"N": "1.500000"} and item["otherCount"] == {"N": "4"}


def test_a_failed_untagged_summary_still_sends_alerts_then_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    class Athena(DailyAndUntaggedAthena):
        def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
            if "HAVING" in self.started[-1]["QueryString"]:
                return {"QueryExecution": {"Status": {"State": "FAILED", "StateChangeReason": "boom"}}}
            return super().get_query_execution(**kwargs)

    puts: list[str] = []
    notified: list[bool] = []

    class Dynamo:
        def put_item(self, **kwargs: Any) -> None:
            puts.append(kwargs["Item"]["PK"]["S"])

    monkeypatch.setattr(ingest, "_client", lambda name: Athena() if name == "athena" else Dynamo())
    monkeypatch.setattr(ingest, "_notify", lambda *args: notified.append(True) or [])
    with pytest.raises(RuntimeError, match="summaries failed for 2026-10"):
        ingest.handler({"months": ["2026-10"]}, None)
    assert puts == ["COST#2026-10"] and notified == [True]


def test_untagged_queries_stop_before_the_lambda_does() -> None:
    class Context:
        def __init__(self, ms: int) -> None:
            self.ms = ms

        def get_remaining_time_in_millis(self) -> int:
            return self.ms

    assert ingest._spare_seconds(None) == 120 and ingest._spare_seconds(Context(300_000)) == 120
    assert ingest._spare_seconds(Context(50_000)) == 30
    with pytest.raises(TimeoutError):
        ingest._spare_seconds(Context(15_000))


def test_known_marketplace_codes_keep_their_stored_names() -> None:
    sql = ingest._service_sql()
    assert "WHEN '9svps415mdwhhoxlp2tx3hald' THEN 'Bedrock Claude Sonnet'" in sql
    assert "WHEN '3jkxki7g66gt6v02m7pqlqyyx' THEN 'Bedrock Claude Opus'" in sql


def test_names_with_quotes_are_escaped_in_the_service_expression(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "PRODUCT_NAMES", {"x'y": "Sonnet's"})
    assert "WHEN 'x''y' THEN 'Sonnet''s'" in ingest._service_sql()


def test_opaque_marketplace_codes_take_the_product_name() -> None:
    for sql in (ingest._sql("2026-10"), ingest._untagged_sql("2026-10")):
        assert "regexp_like(line_item_product_code, '^[a-z0-9]{25}$')" in sql
        assert "ELSE COALESCE(NULLIF(element_at(product, 'product_name'), ''), " in sql
        assert "NULLIF(element_at(product, 'model'), ''), line_item_product_code) END" in sql
        assert "ELSE COALESCE(NULLIF(line_item_product_code, ''), 'unknown') END AS service" in sql


STAGE = "arn:aws:apigateway:us-east-2::/apis/4u2uhqbi7e/stages/$default"  # as the export names an HTTP API stage
STAGE_PROD = "arn:aws:apigateway:us-east-2::/apis/4u2uhqbi7e/stages/prod"


def test_api_stage_rows_get_the_project_tag_of_their_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("PROJECT_TAG", "project")

    class Tagging:
        def get_resources(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["ResourceARNList"] == ["arn:aws:apigateway:us-east-2::/apis/4u2uhqbi7e"]
            tags = [{"Key": "Project", "Value": "portfolio-tracker"}]
            return {"ResourceTagMappingList": [{"ResourceARN": kwargs["ResourceARNList"][0], "Tags": tags}]}

    monkeypatch.setattr(ingest, "_client", lambda name: Tagging())
    resources = [
        {"id": STAGE, "service": "AmazonApiGateway", "cost": Decimal("0.001")},
        {"id": "vol-1", "service": "AmazonEC2", "cost": Decimal(2)},
        {"id": STAGE_PROD, "service": "AmazonApiGateway", "cost": Decimal("0.001")},
    ]
    ingest._name_projects(resources)
    assert resources[0]["project"] == resources[2]["project"] == "portfolio-tracker" and "project" not in resources[1]
    item = ingest._untagged_item("2026-10", {"resources": resources, "otherCount": 0, "otherCost": Decimal(0)})
    assert item["resources"]["L"][0]["M"]["project"] == {"S": "portfolio-tracker"}


def test_stages_from_other_regions_and_bare_ids_are_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("PROJECT_TAG", "project")
    asked: list[list[str]] = []

    class Tagging:
        def get_resources(self, **kwargs: Any) -> dict[str, Any]:
            asked.append(kwargs["ResourceARNList"])
            tags = [{"Key": "project", "Value": "tennis-tracker"}]
            arn = "arn:aws:apigateway:us-east-2::/apis/bare1"
            return {"ResourceTagMappingList": [{"ResourceARN": arn, "Tags": tags}]}

    monkeypatch.setattr(ingest, "_client", lambda name: Tagging())
    elsewhere = {"id": "arn:aws:apigateway:us-west-2::/apis/far1/stages/x", "service": "AmazonApiGateway", "cost": Decimal(1)}
    bare = {"id": "/apis/bare1/stages/x", "service": "AmazonApiGateway", "cost": Decimal(1)}
    ingest._name_projects([elsewhere, bare])
    assert asked == [["arn:aws:apigateway:us-east-2::/apis/bare1"]]  # the other region's stage is not asked about
    assert "project" not in elsewhere and bare["project"] == "tennis-tracker"


def test_a_failed_project_lookup_leaves_the_rows_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("PROJECT_TAG", "project")

    class Tagging:
        def get_resources(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDenied")

    monkeypatch.setattr(ingest, "_client", lambda name: Tagging())
    resources = [{"id": STAGE, "service": "AmazonApiGateway", "cost": Decimal("0.001")}]
    ingest._name_projects(resources)
    assert "project" not in resources[0]
    item = ingest._untagged_item("2026-10", {"resources": resources, "otherCount": 0, "otherCost": Decimal(0)})
    assert "project" not in item["resources"]["L"][0]["M"]


def test_a_missing_region_does_not_fail_the_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    resources = [{"id": "/apis/4u2uhqbi7e/stages/$default", "service": "AmazonApiGateway", "cost": Decimal("0.001")}]
    ingest._name_projects(resources)
    assert "project" not in resources[0]


def test_no_tag_lookup_without_api_stage_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setattr(ingest, "_client", lambda name: pytest.fail("no lookup expected"))
    ingest._name_projects([{"id": "vol-1", "service": "AmazonEC2", "cost": Decimal(2)}])


def _usage(**fields: Any) -> dict[str, Any]:
    return {
        "service": "AWS Lambda",
        "operation": "Invoke",
        "usageType": "Lambda-Requests",
        "region": "us-east-2",
        "unit": "Requests",
        "freeTierType": "Always Free",
        "limit": 1000000.0,
        "actualUsageAmount": 460000.0,
        "forecastedUsageAmount": 550000.0,
        **fields,
    }


class FakeFreeTier:
    def __init__(self, *pages: list[dict[str, Any]]) -> None:
        self.pages = list(pages)
        self.requests: list[dict[str, Any]] = []

    def get_free_tier_usage(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        page: dict[str, Any] = {"freeTierUsages": self.pages[len(self.requests) - 1]}
        if len(self.requests) < len(self.pages):
            page["nextToken"] = str(len(self.requests))
        return page


def test_free_tier_usages_read_every_page_and_skip_allowances_without_a_limit() -> None:
    client = FakeFreeTier(
        [_usage(), _usage(limit=0.0, usageType="none")], [_usage(usageType="Lambda-GB-Second", unit="GB-Seconds")]
    )
    usages = ingest._free_tier_usages(client)
    assert [u["usageType"] for u in usages] == ["Lambda-Requests", "Lambda-GB-Second"]
    assert client.requests[1]["nextToken"] == "1"
    assert usages[0]["limit"] == Decimal("1000000.0") and usages[0]["forecast"] == Decimal("550000.0")


def test_a_forecast_below_usage_so_far_is_raised_to_it_and_a_missing_one_equals_it() -> None:
    low = _usage(forecastedUsageAmount=1.0)
    missing = {k: v for k, v in _usage().items() if k != "forecastedUsageAmount"}
    assert [u["forecast"] for u in ingest._free_tier_usages(FakeFreeTier([low, missing]))] == [Decimal("460000.0")] * 2


def test_free_tier_item_is_one_item_sorted_by_projected_share() -> None:
    usages = ingest._free_tier_usages(FakeFreeTier([_usage(), _usage(usageType="Logs", forecastedUsageAmount=990000.0)]))
    item = ingest._free_tier_item("2026-10", usages)
    assert item["PK"] == {"S": "FREETIER"} and item["SK"] == {"S": "CURRENT"} and item["month"] == {"S": "2026-10"}
    rows = item["usages"]["L"]
    assert [r["M"]["usageType"]["S"] for r in rows] == ["Logs", "Lambda-Requests"]
    assert rows[0]["M"]["limit"] == {"N": "1000000.0"} and rows[0]["M"]["unit"] == {"S": "Requests"}


class Marker:
    """A table that refuses a second put of the same alert marker, as the real condition does."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.keys: set[tuple[str, str]] = set()

    def put_item(self, **kwargs: Any) -> None:
        item = kwargs["Item"]
        if "ConditionExpression" in kwargs:
            key = (item["PK"]["S"], item["SK"]["S"])
            if key in self.keys:
                error = Exception("exists")
                error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}  # pyright: ignore[reportAttributeAccessIssue]
                raise error
            self.keys.add(key)
        self.items.append(item)


def test_free_tier_run_stores_the_item_and_alerts_once_per_month(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    class Sns:
        def publish(self, **kwargs: Any) -> None:
            sent.append(kwargs["Subject"])

    table = Marker()
    over = _usage(
        service="Amazon CloudWatch",
        usageType="DataProcessing-Bytes",
        unit="GB",
        limit=5.0,
        actualUsageAmount=5.8,
        forecastedUsageAmount=6.9,
    )
    pace = _usage(service="Amazon API Gateway", limit=1000000.0, actualUsageAmount=820000.0, forecastedUsageAmount=980000.0 * 1.1)
    fine = _usage()
    monkeypatch.setattr(ingest, "_client", lambda name: Sns())
    today = dt.date(2026, 10, 25)
    first = ingest._free_tier(FakeFreeTier([over, pace, fine]), table, today)
    assert len(first) == 2 and sent == [
        "Amazon CloudWatch is over its free tier",
        "Amazon API Gateway is on pace to pass its free tier",
    ]
    assert table.items[0]["PK"] == {"S": "FREETIER"}
    assert ingest._free_tier(FakeFreeTier([over, pace, fine]), table, today) == []  # tomorrow's run stays quiet
    assert len(sent) == 2


def test_an_early_month_pace_forecast_waits_for_a_quarter_of_the_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "_client", lambda name: pytest.fail("no email expected"))
    early = _usage(actualUsageAmount=100000.0, forecastedUsageAmount=3000000.0)
    assert ingest._free_tier_alerts(ingest._free_tier_usages(FakeFreeTier([early])), "2026-10", Marker()) == []


def test_amounts_are_readable() -> None:
    assert ingest._amount(Decimal("1234567.0")) == "1,234,567"
    assert ingest._amount(Decimal("5.8")) == "5.80"


def test_the_free_tier_client_is_the_us_east_1_one(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    made: list[tuple[str, dict[str, Any]]] = []
    fake = types.SimpleNamespace(client=lambda name, **kwargs: made.append((name, kwargs)))
    botocore = types.ModuleType("botocore")
    config = types.ModuleType("botocore.config")
    config.Config = lambda **kwargs: kwargs  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", config)
    ingest._client("freetier")
    ingest._client("sns")
    bounds = {"connect_timeout": 5, "read_timeout": 10, "retries": {"max_attempts": 2}}
    assert made == [("freetier", {"region_name": "us-east-1", "config": bounds}), ("sns", {})]


def test_a_failed_free_tier_lookup_still_writes_costs_then_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    class Dynamo(Marker):
        def query(self, **kwargs: Any) -> dict[str, Any]:
            return {"Items": []}

    class Broken:
        def get_free_tier_usage(self, **kwargs: Any) -> None:
            raise RuntimeError("denied")

    clients: dict[str, Any] = {"athena": DailyAndUntaggedAthena(), "dynamodb": Dynamo(), "freetier": Broken(), "sns": object()}
    monkeypatch.setattr(ingest, "_client", lambda name: clients[name])
    monkeypatch.setattr(ingest, "_notify", lambda *args: [])
    with pytest.raises(RuntimeError, match="summaries failed for free tier"):
        ingest.handler({}, None)
    assert any(i["PK"]["S"].startswith("COST#") for i in clients["dynamodb"].items)  # the day was written before the failure


def test_a_hand_run_backfill_skips_the_free_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    clients: dict[str, Any] = {"athena": DailyAndUntaggedAthena(), "dynamodb": Marker()}
    monkeypatch.setattr(ingest, "_client", lambda name: clients[name])  # a "freetier" lookup would KeyError
    monkeypatch.setattr(ingest, "_notify", lambda *args: [])
    assert ingest.handler({"months": ["2026-10"]}, None)["written"] == {"2026-10": 1}


def test_a_malformed_entry_is_skipped_and_the_list_is_capped() -> None:
    bad = _usage(limit=None, usageType="bad")
    junk = _usage(limit="NaN", usageType="nan")
    many = [_usage(usageType=f"t{i}", actualUsageAmount=float(i)) for i in range(ingest.FREE_TIER_LISTED + 5)]
    usages = ingest._free_tier_usages(FakeFreeTier([bad, junk, *many]))
    assert len(usages) == len(many)  # the two bad ones are gone
    item = ingest._free_tier_item("2026-10", usages)
    assert len(item["usages"]["L"]) == ingest.FREE_TIER_LISTED and item["total"] == {"N": str(len(many))}
    assert ingest._free_tier_usages(type("Empty", (), {"get_free_tier_usage": lambda self, **k: {}})()) == []


def test_allowances_that_differ_only_by_type_alert_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        ingest, "_client", lambda name: type("Sns", (), {"publish": lambda self, **k: sent.append(k["Subject"])})()
    )
    a = _usage(actualUsageAmount=2000000.0, freeTierType="Always Free")
    b = _usage(actualUsageAmount=2000000.0, freeTierType="12 Months Free")
    assert len(ingest._free_tier_alerts(ingest._free_tier_usages(FakeFreeTier([a, b])), "2026-10", Marker())) == 2
