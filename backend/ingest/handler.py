"""Daily ingest: Athena over the CUR export, then one DynamoDB item per day (overwritten as AWS revises the figures)."""

import datetime as dt
import os
import re
import time
from decimal import Decimal
from typing import Any

MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
REVISION_DAYS = 5  # early in a month, the previous month is still being revised
POLL_SECONDS = 2
UNATTRIBUTED = "unattributed"


def _client(name: str) -> Any:
    import boto3  # pyright: ignore[reportMissingImports]  # in the Lambda runtime; imported late so tests need not have it

    return boto3.client(name)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Re-reads this month (and last month early in a month). `{"months": ["2026-09"]}` backfills by hand."""
    months = _months(event, dt.datetime.now(dt.UTC).date())
    athena, dynamodb = _client("athena"), _client("dynamodb")
    written = {}
    for month in months:
        days = _days(_query(athena, _sql(month)))
        for date, item in days.items():
            dynamodb.put_item(TableName=os.environ["TABLE_NAME"], Item=_item(month, date, item))
        written[month] = len(days)
    return {"written": written}


def _months(event: dict[str, Any], today: dt.date) -> list[str]:
    if "months" in event:
        months = [str(m) for m in event["months"]]
        if not all(MONTH.fullmatch(m) for m in months):
            raise ValueError("months must look like 2026-10")
        return months
    current = today.strftime("%Y-%m")
    if today.day > REVISION_DAYS:
        return [current]
    return [(today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m"), current]


def _tag_column() -> str:
    """The resource_tags key for the project tag: CUR 2.0 names a user tag `user_<key>` in lower snake case."""
    key = re.sub(r"[^a-z0-9]+", "_", os.environ.get("PROJECT_TAG", "project").lower()).strip("_")
    return f"user_{key}"


def _sql(month: str) -> str:
    if not MONTH.fullmatch(month):
        raise ValueError("month must look like 2026-10")
    table = f'"{os.environ["ATHENA_DATABASE"]}"."{os.environ["ATHENA_TABLE"]}"'
    # Only the validated month, the normalized tag key and the deployed table name are interpolated.
    return (
        "SELECT CAST(date(line_item_usage_start_date) AS varchar) AS day, "  # noqa: S608
        "COALESCE(NULLIF(line_item_product_code, ''), 'unknown') AS service, "
        "COALESCE(NULLIF(line_item_usage_type, ''), 'unknown') AS usage_type, "
        f"COALESCE(NULLIF(element_at(resource_tags, '{_tag_column()}'), ''), '{UNATTRIBUTED}') AS project, "
        "SUM(line_item_unblended_cost) AS cost "
        f"FROM {table} WHERE billing_period = '{month}' GROUP BY 1, 2, 3, 4"
    )


def _query(athena: Any, sql: str) -> list[dict[str, str]]:
    """Runs one query in the cost-capped workgroup and returns its rows as dicts of strings."""
    run = athena.start_query_execution(QueryString=sql, WorkGroup=os.environ["ATHENA_WORKGROUP"])["QueryExecutionId"]
    while True:
        status = athena.get_query_execution(QueryExecutionId=run)["QueryExecution"]["Status"]
        if status["State"] == "SUCCEEDED":
            break
        if status["State"] in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"Athena query {status['State'].lower()}: {status.get('StateChangeReason', 'no reason given')}")
        time.sleep(POLL_SECONDS)
    rows: list[dict[str, str]] = []
    header: list[str] = []
    request: dict[str, Any] = {"QueryExecutionId": run}
    while True:
        page = athena.get_query_results(**request)
        for row in page["ResultSet"]["Rows"]:
            values = [d.get("VarCharValue", "") for d in row["Data"]]
            if not header:
                header = values  # the first row of the first page names the columns
            else:
                rows.append(dict(zip(header, values, strict=True)))
        if "NextToken" not in page:
            return rows
        request["NextToken"] = page["NextToken"]


def _days(rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """Totals per day, and per service, project and usage type within it."""
    days: dict[str, dict[str, Any]] = {}
    for row in rows:
        cost = Decimal(row["cost"] or "0")
        day = days.setdefault(row["day"], {"total": Decimal(0), "services": {}, "projects": {}, "usageTypes": {}})
        day["total"] += cost
        for group, key in (("services", "service"), ("projects", "project"), ("usageTypes", "usage_type")):
            day[group][row[key]] = day[group].get(row[key], Decimal(0)) + cost
    return days


def _money(value: Decimal) -> dict[str, str]:
    return {"N": str(value.quantize(Decimal("0.000001")))}


def _item(month: str, date: str, day: dict[str, Any]) -> dict[str, Any]:
    """The DynamoDB form of one day, keyed so the API's query for the month finds it."""
    return {
        "PK": {"S": f"COST#{month}"},
        "SK": {"S": f"DAY#{date}"},
        "total": _money(day["total"]),
        **{group: {"M": {k: _money(v) for k, v in day[group].items()}} for group in ("services", "projects", "usageTypes")},
    }
