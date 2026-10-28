"""Daily ingest: Athena over the CUR export, then one DynamoDB item per day (overwritten as AWS revises the figures)."""

import datetime as dt
import os
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from ingest import analysis

MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
REVISION_DAYS = 5  # early in a month, the previous month is still being revised
POLL_SECONDS = 2
QUERY_SECONDS = 120  # per query; the untagged queries also stop short of the Lambda's own timeout (see _spare_seconds)
NO_RESOURCE = "(no resource ID)"  # data transfer, support and similar items carry no resource ID
UNTAGGED_LISTED = 25  # resources kept per month; the rest are counted and summed
UNATTRIBUTED = "unattributed"
# An HTTP API stage's resource ID is an ARN ending /apis/<id>/stages/<stage>: opaque, so its project is looked up.
API_STAGE = re.compile(r"(?:arn:aws:apigateway:([a-z0-9-]+)::)?/apis/([a-z0-9]+)/stages/[^/]+")
# Marketplace products (the Anthropic models on Bedrock) are billed under an opaque 25-character code.
OPAQUE_CODE = "^[a-z0-9]{25}$"
# Names already stored in the history; kept so those days and their alerts don't split. Newer codes take the export's name.
PRODUCT_NAMES = {
    "3jkxki7g66gt6v02m7pqlqyyx": "Bedrock Claude Opus",
    "7g37zhparap7eesm9k78jrzqc": "Bedrock Claude Haiku",
    "9svps415mdwhhoxlp2tx3hald": "Bedrock Claude Sonnet",
}
FREE_TIER_REGION = "us-east-1"  # the Free Tier API has a single endpoint, whatever region the usage is in
FREE_TIER_LISTED = 200  # rows kept, worst first, so the one item stays far under the 400 KB limit
FREE_TIER_PACE_MIN = 4  # a pace alert waits until a quarter of the allowance is used: early-month forecasts swing wildly
FRESHNESS_KEY = {
    "PK": {"S": "META"},
    "SK": {"S": "FRESHNESS"},
}  # when AWS last delivered the export, and when this ingest last ran
ALERT_FRESH_DAYS = 3  # a hand-run backfill of old months must not email about them


def _client(name: str) -> Any:
    import boto3  # pyright: ignore[reportMissingImports]  # in the Lambda runtime; imported late so tests need not have it

    if name == "freetier":  # optional work that runs before the Athena queries: bound its time
        from botocore.config import Config  # pyright: ignore[reportMissingImports]

        config = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})
        return boto3.client(name, region_name=FREE_TIER_REGION, config=config)
    return boto3.client(name)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Re-reads this month (and last month early in a month). `{"months": ["2026-09"]}` backfills by hand.

    `{"digest": true}` (the weekly schedule) emails the week's summary from the stored days instead.
    """
    if event.get("digest") is True:
        return {"digest": _digest(_client("dynamodb"), dt.datetime.now(dt.UTC).date())}
    months = _months(event, dt.datetime.now(dt.UTC).date())
    athena, dynamodb = _client("athena"), _client("dynamodb")
    written = {}
    seen: dict[str, dict[str, Any]] = {}
    for month in months:
        days = _days(_query(athena, _sql(month)))
        for date, item in days.items():
            dynamodb.put_item(TableName=os.environ["TABLE_NAME"], Item=_item(month, date, item))
        written[month] = len(days)
        seen.update(days)
    sent = _notify(seen, dt.datetime.now(dt.UTC).date(), dynamodb)
    # The summaries come last and are isolated: a failure must not cost the day's alerts, but must still fail the run.
    # The free-tier lookup is quick, so it goes before the Athena queries that can use up the Lambda's time.
    failed = []
    if "months" not in event:  # a hand-run backfill of old months has no bearing on this month's allowances
        try:
            sent += _free_tier(_client("freetier"), dynamodb, dt.datetime.now(dt.UTC).date())
        except Exception as error:
            print(f"Free tier usage failed: {error!r}")
            failed.append("free tier")
    if "months" not in event:
        try:
            _freshness(_client("s3"), dynamodb, months, dt.datetime.now(dt.UTC))
        except Exception as error:
            print(f"Freshness failed: {error!r}")
            failed.append("freshness")
    for month in months:
        try:
            summary = _untagged(_query(athena, _untagged_sql(month), _spare_seconds(context)))
            _name_projects(summary["resources"])
            dynamodb.put_item(TableName=os.environ["TABLE_NAME"], Item=_untagged_item(month, summary))
        except Exception as error:
            print(f"Untagged summary for {month} failed: {error!r}")
            failed.append(month)
    if failed:
        raise RuntimeError(f"The summaries failed for {', '.join(failed)}; costs and alerts were written")
    return {"written": written, "alerts": sent}


def _freshness(s3: Any, dynamodb: Any, months: list[str], now: dt.datetime) -> None:
    """Stores the newest export file's delivery time (the dashboard can only be as fresh as that) and this run's time."""
    delivered = _export_delivered(s3, months)
    item: dict[str, Any] = {**FRESHNESS_KEY, "ingestAt": {"S": now.isoformat(timespec="seconds")}}
    if delivered is not None:
        item["exportAt"] = {"S": delivered.astimezone(dt.UTC).isoformat(timespec="seconds")}
    dynamodb.put_item(TableName=os.environ["TABLE_NAME"], Item=item)


def _export_delivered(s3: Any, months: list[str]) -> dt.datetime | None:
    newest: dt.datetime | None = None
    for month in months:
        prefix = f"{os.environ['EXPORT_DATA_PREFIX']}/BILLING_PERIOD={month}/"
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=os.environ["BUCKET_NAME"], Prefix=prefix):
            for obj in page.get("Contents", []):
                if newest is None or obj["LastModified"] > newest:
                    newest = obj["LastModified"]
    return newest


def _free_tier(client: Any, dynamodb: Any, today: dt.date) -> list[str]:
    """Stores this month's free-tier allowances and emails once for each that is projected to run out or already has."""
    usages = _free_tier_usages(client)
    dynamodb.put_item(TableName=os.environ["TABLE_NAME"], Item=_free_tier_item(today.strftime("%Y-%m"), usages))
    return _free_tier_alerts(usages, today.strftime("%Y-%m"), dynamodb)


def _free_tier_usages(client: Any) -> list[dict[str, Any]]:
    """Every free-tier allowance with its usage so far and AWS's month-end forecast; allowances with no limit are skipped."""
    usages: list[dict[str, Any]] = []
    request: dict[str, Any] = {"maxResults": 1000}
    while True:
        page = client.get_free_tier_usage(**request)
        for u in page.get("freeTierUsages", []):
            try:
                limit, actual = Decimal(str(u.get("limit") or 0)), Decimal(str(u.get("actualUsageAmount") or 0))
                forecast = Decimal(str(u.get("forecastedUsageAmount") or actual))
            except InvalidOperation:  # one malformed entry must not discard the rest
                print(f"Skipped a malformed free-tier entry: {u.get('service')} {u.get('usageType')}")
                continue
            if not (limit.is_finite() and actual.is_finite()) or limit <= 0:
                continue
            usages.append(
                {
                    "service": u.get("service", "unknown"),
                    "operation": u.get("operation", ""),
                    "usageType": u.get("usageType", ""),
                    "region": u.get("region", ""),
                    "unit": u.get("unit", ""),
                    "type": u.get("freeTierType", ""),
                    "limit": limit,
                    "actual": actual,
                    "forecast": max(forecast, actual) if forecast.is_finite() else actual,
                }
            )
        if not page.get("nextToken"):
            return usages
        request["nextToken"] = page["nextToken"]


def _free_tier_item(month: str, usages: list[dict[str, Any]]) -> dict[str, Any]:
    """One item, rewritten on every run, under its own key so `GET /costs/{month}` stays small."""
    numbers = ("limit", "actual", "forecast")
    ordered = sorted(usages, key=lambda u: (-u["forecast"] / u["limit"], u["service"], u["usageType"]))[:FREE_TIER_LISTED]
    return {
        "PK": {"S": "FREETIER"},
        "SK": {"S": "CURRENT"},
        "month": {"S": month},
        "total": {"N": str(len(usages))},  # the page says "x of total" even when the list was cut
        "usages": {"L": [{"M": {k: ({"N": str(v)} if k in numbers else {"S": str(v)}) for k, v in u.items()}} for u in ordered]},
    }


def _free_tier_alerts(usages: list[dict[str, Any]], month: str, dynamodb: Any) -> list[str]:
    """One email per allowance per month and per kind: projected to run out, then already over."""
    sent = []
    for u in usages:
        if u["actual"] > u["limit"]:
            kind, subject = "over", f"{u['service']} is over its free tier"
            detail = f"{_amount(u['actual'])} of {_amount(u['limit'])} {u['unit']} used, so usage past the limit is billed."
        elif u["forecast"] > u["limit"] and u["actual"] * FREE_TIER_PACE_MIN >= u["limit"]:
            kind, subject = "pace", f"{u['service']} is on pace to pass its free tier"
            used = f"{_amount(u['actual'])} of {_amount(u['limit'])} {u['unit']} used"
            detail = f"{used}; AWS forecasts {_amount(u['forecast'])} by month end."
        else:
            continue
        message = f"{u['service']} ({u['usageType'] or u['operation']}, {u['region']}, {u['type']}): {detail}"
        key = f"freetier#{kind}#{u['service']}#{u['operation']}#{u['usageType']}#{u['region']}#{u['type']}"
        if _publish_once(dynamodb, f"ALERT#{month}", analysis.Alert(key, subject, message)):
            sent.append(key)
    return sent


def _amount(value: Decimal) -> str:
    return f"{value:,.0f}" if value >= 100 else f"{value:,.2f}"


def _name_projects(resources: list[dict[str, Any]]) -> None:
    """Adds `project` to API stage rows from the API's current project tag; a failed lookup only leaves the rows as they were."""
    try:
        rows: dict[str, list[dict[str, Any]]] = {}  # an API can have several stages, each its own row
        for resource in resources:
            match = API_STAGE.fullmatch(resource["id"])
            if match:
                region = os.environ["AWS_REGION"]  # the tagging client is regional: a stage from another region is left alone
                if match[1] in (None, region):
                    rows.setdefault(f"arn:aws:apigateway:{region}::/apis/{match[2]}", []).append(resource)
        if not rows:
            return
        key = os.environ["PROJECT_TAG"].lower()
        found = _client("resourcegroupstaggingapi").get_resources(ResourceARNList=list(rows))["ResourceTagMappingList"]
        for entry in found:
            for tag in entry["Tags"]:
                if tag["Key"].lower() == key and tag["Value"]:
                    for resource in rows.get(entry["ResourceARN"], []):
                        resource["project"] = tag["Value"]
    except Exception as error:
        print(f"Project lookup failed: {error!r}")


def _spare_seconds(context: Any) -> int:
    """How long the optional queries may run: the usual limit, or what the Lambda has left minus a margin for the write."""
    left = getattr(context, "get_remaining_time_in_millis", lambda: QUERY_SECONDS * 1000 + 20_000)() // 1000 - 20
    if left < 1:
        raise TimeoutError("no time left in the Lambda for the untagged summary")
    return min(QUERY_SECONDS, left)


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


def _notify(days: dict[str, dict[str, Any]], today: dt.date, dynamodb: Any) -> list[str]:
    """Emails each new alert once. A marker item per alert key stops tomorrow's run from repeating it."""
    if not days:
        return []
    if (today - dt.date.fromisoformat(max(days))).days > ALERT_FRESH_DAYS:
        print(f"No alerts: the newest cost day is {max(days)}, so the export looks stale")
        return []
    month, sent = max(days)[:7], []  # a first-time service alerts once ever, so its marker is not per month
    try:
        days = _with_history(days, dynamodb)
    except Exception as error:  # a short baseline only quiets the rules; it must not lose the alerts the run can still send
        print(f"Alerting without last month's history: {error!r}")
    for alert in analysis.alerts(days, Decimal(os.environ["BUDGET_USD"])):
        if _publish_once(dynamodb, "ALERT#global" if alert.key.startswith("new-service#") else f"ALERT#{month}", alert):
            sent.append(alert.key)
    return sent


def _publish_once(dynamodb: Any, partition: str, alert: analysis.Alert) -> bool:
    """Puts the alert's marker, then publishes. False when the marker exists; the marker is released if the email fails."""
    marker = {"PK": {"S": partition}, "SK": {"S": alert.key}}
    try:
        dynamodb.put_item(TableName=os.environ["TABLE_NAME"], Item=marker, ConditionExpression="attribute_not_exists(SK)")
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False  # already sent
        raise
    try:
        _client("sns").publish(TopicArn=os.environ["TOPIC_ARN"], Subject=alert.subject[:100], Message=alert.message)
    except Exception:
        dynamodb.delete_item(TableName=os.environ["TABLE_NAME"], Key=marker)  # let the next run retry
        raise
    return True


def _digest(dynamodb: Any, today: dt.date) -> bool:
    """Publishes the weekly summary of the stored days; skipped when the export looks stale."""
    month = today.strftime("%Y-%m")
    previous = (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    days = {**_stored_days(dynamodb, previous, 2 * analysis.TRAILING_DAYS), **_stored_days(dynamodb, month, 31)}
    summary = analysis.digest(days, Decimal(os.environ["BUDGET_USD"]))
    if summary is None or (today - dt.date.fromisoformat(max(days))).days > ALERT_FRESH_DAYS:
        print("No digest: the stored days are missing or stale")
        return False
    subject, message = summary
    _client("sns").publish(TopicArn=os.environ["TOPIC_ARN"], Subject=subject[:100], Message=message)
    return True


def _with_history(days: dict[str, dict[str, Any]], dynamodb: Any) -> dict[str, dict[str, Any]]:
    """Adds the last stored days of the month before the newest day's, when the alert baseline would otherwise be short.

    Early in a month the run reads only this month, so the 7-day baseline would be thin; the previous month's items
    are already in the table. Days read this run win over stored ones.
    """
    latest = max(days)
    if sum(1 for d in days if d < latest) >= analysis.TRAILING_DAYS:
        return days
    previous = (dt.date.fromisoformat(latest).replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    return {**_stored_days(dynamodb, previous), **days}


def _stored_days(dynamodb: Any, month: str, limit: int = analysis.TRAILING_DAYS) -> dict[str, dict[str, Any]]:
    """The month's newest `limit` days (one page: a month of items is far under 1 MB) in the shape `_days` builds."""
    page = dynamodb.query(
        TableName=os.environ["TABLE_NAME"],
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={":pk": {"S": f"COST#{month}"}, ":sk": {"S": "DAY#"}},
        ScanIndexForward=False,  # newest first, so the limit keeps the days nearest this month
        Limit=limit,
        ConsistentRead=True,
    )
    return {
        item["SK"]["S"].removeprefix("DAY#"): {
            "total": Decimal(item["total"]["N"]),
            **{g: {k: Decimal(v["N"]) for k, v in item[g]["M"].items()} for g in ("services", "projects", "usageTypes")},
        }
        for item in page["Items"]
    }


def _tag_column() -> str:
    """The resource_tags key for the project tag: CUR 2.0 names a user tag `user_<key>` in lower snake case."""
    key = re.sub(r"[^a-z0-9]+", "_", os.environ.get("PROJECT_TAG", "project").lower()).strip("_")
    return f"user_{key}"


def _quote(value: str) -> str:
    return value.replace("'", "''")


def _service_sql() -> str:
    """The service column: the product code; an opaque marketplace code becomes its stored name or the export's product name."""
    known = "".join(f"WHEN '{_quote(code)}' THEN '{_quote(name)}' " for code, name in PRODUCT_NAMES.items())
    return (
        f"CASE WHEN regexp_like(line_item_product_code, '{OPAQUE_CODE}') "
        f"THEN CASE line_item_product_code {known}"
        "ELSE COALESCE(NULLIF(element_at(product, 'product_name'), ''), NULLIF(element_at(product, 'model'), ''), "
        "line_item_product_code) END "
        "ELSE COALESCE(NULLIF(line_item_product_code, ''), 'unknown') END"
    )


def _sql(month: str) -> str:
    if not MONTH.fullmatch(month):
        raise ValueError("month must look like 2026-10")
    table = f'"{os.environ["ATHENA_DATABASE"]}"."{os.environ["ATHENA_TABLE"]}"'
    # Only the validated month, the normalized tag key and the deployed table name are interpolated.
    return (
        "SELECT CAST(date(line_item_usage_start_date) AS varchar) AS day, "  # noqa: S608
        f"{_service_sql()} AS service, "
        "COALESCE(NULLIF(line_item_usage_type, ''), 'unknown') AS usage_type, "
        f"COALESCE(NULLIF(element_at(resource_tags, '{_tag_column()}'), ''), '{UNATTRIBUTED}') AS project, "
        "SUM(line_item_unblended_cost) AS cost "
        f"FROM {table} WHERE billing_period = '{month}' GROUP BY 1, 2, 3, 4"
    )


def _untagged_sql(month: str) -> str:
    """Cost per resource and service among the line items that carry no project tag."""
    if not MONTH.fullmatch(month):
        raise ValueError("month must look like 2026-10")
    table = f'"{os.environ["ATHENA_DATABASE"]}"."{os.environ["ATHENA_TABLE"]}"'
    return (
        "SELECT COALESCE(NULLIF(line_item_resource_id, ''), '') AS resource, "  # noqa: S608
        f"{_service_sql()} AS service, "
        "SUM(line_item_unblended_cost) AS cost "
        f"FROM {table} WHERE billing_period = '{month}' "
        f"AND NULLIF(element_at(resource_tags, '{_tag_column()}'), '') IS NULL "
        "GROUP BY 1, 2 HAVING SUM(line_item_unblended_cost) > 0"
    )


def _untagged(rows: list[dict[str, str]]) -> dict[str, Any]:
    """The costliest untagged resources, with the rest counted and summed. Items with no resource ID share one entry."""
    found: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        cost = Decimal(row["cost"] or "0")
        if not cost.is_finite():
            raise ValueError(f"non-finite cost for {row['resource']} {row['service']}: {row['cost']}")
        services = found.setdefault(row["resource"], {})
        services[row["service"]] = services.get(row["service"], Decimal(0)) + cost
    ranked = sorted(((sum(s.values(), Decimal(0)), rid, s) for rid, s in found.items()), key=lambda t: (-t[0], t[1]))
    resources = []
    for cost, rid, services in ranked[:UNTAGGED_LISTED]:
        names = sorted(services, key=lambda n: (-services[n], n))
        service = ", ".join(names[:2]) + (f" +{len(names) - 2} more" if len(names) > 2 else "")
        resources.append({"id": rid, "service": service, "cost": cost})
    rest = ranked[UNTAGGED_LISTED:]
    return {"resources": resources, "otherCount": len(rest), "otherCost": sum((c for c, _, _ in rest), Decimal(0))}


def _project(resource: dict[str, Any]) -> dict[str, Any]:
    return {"project": {"S": resource["project"]}} if resource.get("project") else {}


def _untagged_item(month: str, summary: dict[str, Any]) -> dict[str, Any]:
    """One item per month under its own key, so `GET /costs/{month}` stays small."""
    return {
        "PK": {"S": f"HYGIENE#{month}"},
        "SK": {"S": "UNTAGGED"},
        "resources": {
            "L": [
                {"M": {"id": {"S": r["id"]}, "service": {"S": r["service"]}, "cost": _money(r["cost"]), **_project(r)}}
                for r in summary["resources"]
            ]
        },
        "otherCount": {"N": str(summary["otherCount"])},
        "otherCost": _money(summary["otherCost"]),
    }


def _query(athena: Any, sql: str, seconds: int | None = None) -> list[dict[str, str]]:
    """Runs one query in the cost-capped workgroup and returns its rows as dicts of strings."""
    run = athena.start_query_execution(QueryString=sql, WorkGroup=os.environ["ATHENA_WORKGROUP"])["QueryExecutionId"]
    limit = QUERY_SECONDS if seconds is None else seconds
    deadline = time.monotonic() + limit
    while True:
        status = athena.get_query_execution(QueryExecutionId=run)["QueryExecution"]["Status"]
        if status["State"] == "SUCCEEDED":
            break
        if status["State"] in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"Athena query {status['State'].lower()}: {status.get('StateChangeReason', 'no reason given')}")
        if time.monotonic() > deadline:
            athena.stop_query_execution(QueryExecutionId=run)  # don't leave a billed query running past the Lambda
            raise RuntimeError(f"Athena query still {status['State'].lower()} after {limit} seconds; cancelled")
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
        if not cost.is_finite():
            raise ValueError(f"non-finite cost for {row['day']} {row['service']}: {row['cost']}")
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
