"""HTTP API Lambda: the signed-in user's daily cost aggregates, read from DynamoDB (the dashboard never calls Athena)."""

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
    return _json(404, {"error": "not found"})


def _costs(month: str, table: Any = None) -> dict[str, Any]:
    """One item per day under PK COST#<month>, SK DAY#<date>; the ingest Lambda overwrites a day as AWS revises it."""
    if not MONTH.fullmatch(month):
        return _json(400, {"error": "month must look like 2026-10"})
    table = table or _days_table()
    items: list[dict[str, Any]] = []
    query: dict[str, Any] = {"KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)"}
    query["ExpressionAttributeValues"] = {":pk": f"COST#{month}", ":sk": "DAY#"}
    while True:
        page = table.query(**query)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    days = [{"date": i["SK"].removeprefix("DAY#"), **{k: v for k, v in i.items() if k not in ("PK", "SK")}} for i in items]
    return _json(200, {"month": month, "days": days})


def _json(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=_number),
    }


def _number(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")
