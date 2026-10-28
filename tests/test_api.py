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
    assert body["days"] == [{"date": "2026-10-01", "total": 1.25}, {"date": "2026-10-02", "total": 2}]
    assert table.queries[1]["ExclusiveStartKey"] == {"PK": "x", "SK": "y"}
    assert table.queries[0]["ExpressionAttributeValues"] == {":pk": "COST#2026-10", ":sk": "DAY#"}


def test_handler_routes_to_costs_and_reads_consistently(monkeypatch: Any) -> None:
    table = FakeTable({"Items": [{"PK": "COST#2026-10", "SK": "DAY#2026-10-03", "date": "bogus", "n": Decimal("3")}]})
    monkeypatch.setattr(api, "_days_table", lambda: table)
    response = api.handler(_event(), None)
    assert json.loads(response["body"])["days"] == [{"date": "2026-10-03", "n": 3}]
    assert table.queries[0]["ConsistentRead"] is True


def test_untagged_returns_the_stored_summary_or_an_empty_one() -> None:
    item = {
        "PK": "HYGIENE#2026-10",
        "SK": "UNTAGGED",
        "resources": [{"id": "vol-1", "service": "AmazonEC2", "cost": Decimal("1.5")}],
        "otherCount": Decimal(3),
        "otherCost": Decimal("0.25"),
    }
    table = FakeTable({"Items": [item]}, {"Items": []})
    body = json.loads(api._untagged("2026-10", table)["body"])
    assert body == {
        "month": "2026-10",
        "resources": [{"id": "vol-1", "service": "AmazonEC2", "cost": 1.5}],
        "otherCount": 3,
        "otherCost": 0.25,
    }
    assert table.queries[0]["ExpressionAttributeValues"] == {":pk": "HYGIENE#2026-10", ":sk": "UNTAGGED"}
    assert table.queries[0]["ConsistentRead"] is True
    assert json.loads(api._untagged("2026-09", table)["body"]) == {
        "month": "2026-09",
        "resources": [],
        "otherCount": 0,
        "otherCost": 0,
    }


def test_handler_routes_tags_and_validates_the_month(monkeypatch: Any) -> None:
    table = FakeTable({"Items": []})
    monkeypatch.setattr(api, "_days_table", lambda: table)
    assert api.handler(_event("GET /tags/{month}"), None)["statusCode"] == 200
    assert api.handler(_event("GET /tags/{month}", month="nope"), None)["statusCode"] == 400


def test_untagged_tolerates_a_partial_item() -> None:
    body = json.loads(api._untagged("2026-10", FakeTable({"Items": [{"PK": "HYGIENE#2026-10", "SK": "UNTAGGED"}]}))["body"])
    assert body == {"month": "2026-10", "resources": [], "otherCount": 0, "otherCost": 0}


def test_free_tier_returns_the_stored_allowances_or_an_empty_list() -> None:
    item = {
        "PK": "FREETIER",
        "SK": "CURRENT",
        "month": "2026-10",
        "usages": [{"service": "AWS Lambda", "limit": Decimal("1000000.0")}],
    }
    table = FakeTable({"Items": [item]}, {"Items": []})
    assert json.loads(api._free_tier(table)["body"]) == {
        "month": "2026-10",
        "total": 1,
        "usages": [{"service": "AWS Lambda", "limit": 1000000}],
    }
    assert table.queries[0]["ExpressionAttributeValues"] == {":pk": "FREETIER", ":sk": "CURRENT"}
    assert table.queries[0]["ConsistentRead"] is True
    assert json.loads(api._free_tier(table)["body"]) == {"month": None, "total": 0, "usages": []}


def test_handler_routes_free_tier(monkeypatch: Any) -> None:
    monkeypatch.setattr(api, "_days_table", lambda: FakeTable({"Items": []}))
    assert api.handler(_event("GET /freetier"), None)["statusCode"] == 200


def test_freshness_returns_the_stored_times_or_nulls() -> None:
    item = {"PK": "META", "SK": "FRESHNESS", "exportAt": "2026-10-28T08:26:15+00:00", "ingestAt": "2026-10-28T12:30:04+00:00"}
    table = FakeTable({"Items": [item]}, {"Items": []})
    assert json.loads(api._freshness(table)["body"]) == {
        "exportAt": "2026-10-28T08:26:15+00:00",
        "ingestAt": "2026-10-28T12:30:04+00:00",
    }
    assert table.queries[0]["ExpressionAttributeValues"] == {":pk": "META", ":sk": "FRESHNESS"}
    assert json.loads(api._freshness(table)["body"]) == {"exportAt": None, "ingestAt": None}


def test_handler_routes_freshness(monkeypatch: Any) -> None:
    monkeypatch.setattr(api, "_days_table", lambda: FakeTable({"Items": []}))
    assert api.handler(_event("GET /freshness"), None)["statusCode"] == 200
