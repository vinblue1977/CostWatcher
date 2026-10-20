"""The costs API: sign-in is required, months are validated, and a month's days are read from the table."""

import json
from decimal import Decimal
from typing import Any

from api import handler as api


class FakeTable:
    def __init__(self, *pages: dict[str, Any]) -> None:
        self.pages = list(pages)
        self.queries: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(dict(kwargs))
        return self.pages.pop(0)


def _event(route: str = "GET /costs/{month}", month: str | None = "2026-10", signed_in: bool = True) -> dict[str, Any]:
    event: dict[str, Any] = {"routeKey": route, "pathParameters": {"month": month} if month else None}
    event["requestContext"] = {"authorizer": {"jwt": {"claims": {"sub": "u1"}}} if signed_in else None}
    return event


def test_requires_sign_in() -> None:
    assert api.handler(_event(signed_in=False), None)["statusCode"] == 401


def test_unknown_route_is_404() -> None:
    assert api.handler(_event("GET /devices", None), None)["statusCode"] == 404


def test_malformed_authorizer_is_401_not_500() -> None:
    event = {"routeKey": "GET /costs/{month}", "requestContext": {}}
    assert api.handler(event, None)["statusCode"] == 401


def test_rejects_bad_months() -> None:
    for month in ("", "2026-13", "2026-1", "2026-10-01", "x#y"):
        assert api.handler(_event(month=month), None)["statusCode"] == 400, month


def test_reads_every_page_of_days() -> None:
    day = {"PK": "COST#2026-10", "SK": "DAY#2026-10-01", "total": Decimal("1.25")}
    table = FakeTable(
        {"Items": [day], "LastEvaluatedKey": {"PK": "x", "SK": "y"}},
        {"Items": [{**day, "SK": "DAY#2026-10-02", "total": Decimal("2")}]},
    )
    response = api._costs("2026-10", table)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["days"] == [{"date": "2026-10-01", "total": 1.25}, {"date": "2026-10-02", "total": 2.0}]
    assert table.queries[1]["ExclusiveStartKey"] == {"PK": "x", "SK": "y"}
    assert table.queries[0]["ExpressionAttributeValues"] == {":pk": "COST#2026-10", ":sk": "DAY#"}
