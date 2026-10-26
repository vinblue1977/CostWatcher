"""HTTP API Lambda: the signed-in user's costs, untagged resources and free-tier usage, read from DynamoDB, never Athena."""

import json
import os
import re
from decimal import Decimal
from typing import Any

MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
_table: Any = None


def _days_table() -> Any:
    global _table
    if _table is None:
        import boto3  # pyright: ignore[reportMissingImports]  # in the Lambda runtime; imported late so tests need not have it

        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        _ = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except KeyError, TypeError:
        return _json(401, {"error": "unauthorized"})
    if event.get("routeKey") == "GET /costs/{month}":
        return _costs((event.get("pathParameters") or {}).get("month", ""))
    if event.get("routeKey") == "GET /tags/{month}":
        return _untagged((event.get("pathParameters") or {}).get("month", ""))
    if event.get("routeKey") == "GET /freetier":
        return _free_tier()
    return _json(404, {"error": "not found"})


def _costs(month: str, table: Any = None) -> dict[str, Any]:
    """One item per day under PK COST#<month>, SK DAY#<date>; the ingest Lambda overwrites a day as AWS revises it."""
    if not MONTH.fullmatch(month):
        return _json(400, {"error": "month must look like 2026-10"})
    table = table or _days_table()
    items: list[dict[str, Any]] = []
    query: dict[str, Any] = {"KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)", "ConsistentRead": True}
    query["ExpressionAttributeValues"] = {":pk": f"COST#{month}", ":sk": "DAY#"}
    while True:
        page = table.query(**query)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    days = [{**{k: v for k, v in i.items() if k not in ("PK", "SK")}, "date": i["SK"].removeprefix("DAY#")} for i in items]
    return _json(200, {"month": month, "days": days})


def _untagged(month: str, table: Any = None) -> dict[str, Any]:
    """The month's costliest resources without a project tag (PK HYGIENE#<month>), which the ingest Lambda rewrites daily."""
    if not MONTH.fullmatch(month):
        return _json(400, {"error": "month must look like 2026-10"})
    table = table or _days_table()
    page = table.query(
        KeyConditionExpression="PK = :pk AND SK = :sk",
        ExpressionAttributeValues={":pk": f"HYGIENE#{month}", ":sk": "UNTAGGED"},
        ConsistentRead=True,
    )
    item = page["Items"][0] if page["Items"] else {"resources": [], "otherCount": 0, "otherCost": 0}
    return _json(
        200,
        {
            "month": month,
            "resources": item.get("resources", []),
            "otherCount": item.get("otherCount", 0),
            "otherCost": item.get("otherCost", 0),
        },
    )


def _free_tier(table: Any = None) -> dict[str, Any]:
    """This month's free-tier allowances (PK FREETIER), which the ingest Lambda rewrites daily."""
    table = table or _days_table()
    page = table.query(
        KeyConditionExpression="PK = :pk AND SK = :sk",
        ExpressionAttributeValues={":pk": "FREETIER", ":sk": "CURRENT"},
        ConsistentRead=True,
    )
    item = page["Items"][0] if page["Items"] else {}
    usages = item.get("usages", [])
    return _json(200, {"month": item.get("month"), "total": item.get("total", len(usages)), "usages": usages})


def _json(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=_number),
    }


def _number(value: Any) -> int | float:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")
