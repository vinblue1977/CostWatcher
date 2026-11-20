"""The weekly waste scan: each check against faked AWS answers, and the handler's new-versus-known email logic."""

import datetime as dt
import sys
import types
from decimal import Decimal
from typing import Any

import pytest
from waste import handler as waste

NOW = dt.datetime(2026, 11, 22, 15, 0, tzinfo=dt.UTC)
OLD = NOW - dt.timedelta(days=40)


class Client:
    """A boto3 client stand-in: paginators answer from `pages` by operation name; other calls from `answers`."""

    def __init__(self, pages: dict[str, list[dict[str, Any]]] | None = None, **answers: Any) -> None:
        self.pages = pages or {}
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_paginator(self, operation: str) -> Any:
        outer = self

        class Paginator:
            def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                outer.calls.append((operation, kwargs))
                return outer.pages[operation]

        return Paginator()

    def describe_addresses(self) -> dict[str, Any]:
        return self.answers["addresses"]

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_metric_statistics", kwargs))
        return {"Datapoints": self.answers["traffic"].get(kwargs["Dimensions"][0]["Value"], [])}


def _traffic(**by_id: float) -> Client:
    return Client(traffic={k: [{"Sum": v}] for k, v in by_id.items()})


def test_idle_nat_gateway_needs_age_and_zero_bytes() -> None:
    nats = {
        "NatGateways": [
            {"NatGatewayId": "nat-idle", "CreateTime": OLD},
            {"NatGatewayId": "nat-busy", "CreateTime": OLD},
            {"NatGatewayId": "nat-new", "CreateTime": NOW - dt.timedelta(days=2)},
        ]
    }
    ec2 = Client({"describe_nat_gateways": [nats]})
    found = waste.idle_nat_gateways(ec2, _traffic(**{"nat-idle": 0, "nat-busy": 9e9, "nat-new": 0}), "us-east-2", NOW)
    assert [(f["id"], f["monthly"]) for f in found] == [("nat-idle", 32.85)]
    assert found[0]["key"] == "idle-nat#us-east-2#nat-idle"
    assert ec2.calls[0][1]["Filter"] == [{"Name": "state", "Values": ["available"]}]


def test_idle_load_balancer_uses_its_own_arn_suffix_and_skips_gateway_balancers() -> None:
    def lb(name: str, kind: str, created: dt.datetime = OLD) -> dict[str, Any]:
        arn = f"arn:aws:elasticloadbalancing:us-east-2:1:loadbalancer/{'app' if kind == 'application' else 'net'}/{name}/abc"
        return {"LoadBalancerName": name, "LoadBalancerArn": arn, "Type": kind, "CreatedTime": created}

    elb = Client(
        {
            "describe_load_balancers": [
                {"LoadBalancers": [lb("a", "application"), lb("n", "network"), lb("g", "gateway"), lb("new", "application", NOW)]}
            ]
        }
    )
    cloudwatch = Client(traffic={})  # no datapoints at all: nothing moved
    found = waste.idle_load_balancers(elb, cloudwatch, "us-east-2", NOW)
    assert [f["id"] for f in found] == ["a", "n"] and found[0]["monthly"] == 16.43
    asked = [(c[1]["Namespace"], c[1]["Dimensions"][0]["Value"]) for c in cloudwatch.calls]
    assert asked == [("AWS/ApplicationELB", "app/a/abc"), ("AWS/NetworkELB", "net/n/abc")]


def test_unattached_volume_is_priced_by_type_with_a_default() -> None:
    volumes = {
        "Volumes": [
            {"VolumeId": "vol-1", "Size": 20, "VolumeType": "gp3"},
            {"VolumeId": "vol-2", "Size": 10, "VolumeType": "odd"},
        ]
    }
    ec2 = Client({"describe_volumes": [volumes]})
    found = waste.unattached_volumes(ec2, "us-east-2")
    assert [(f["id"], f["monthly"]) for f in found] == [("vol-1", 1.6), ("vol-2", 1.0)]
    assert ec2.calls[0][1]["Filters"] == [{"Name": "status", "Values": ["available"]}]


def test_unattached_address_ignores_associated_and_classic_ones() -> None:
    ec2 = Client(
        addresses={
            "Addresses": [
                {"AllocationId": "eipalloc-free", "Domain": "vpc"},
                {"AllocationId": "eipalloc-used", "AssociationId": "eipassoc-1"},
                {"PublicIp": "1.2.3.4", "Domain": "standard"},
            ]
        }
    )
    assert [(f["id"], f["monthly"]) for f in waste.unattached_addresses(ec2, "us-east-1")] == [("eipalloc-free", 3.65)]


def test_stopped_instance_costs_its_attached_volumes_and_skips_recent_stops() -> None:
    def instance(iid: str, reason: str, *volumes: str) -> dict[str, Any]:
        return {
            "InstanceId": iid,
            "StateTransitionReason": reason,
            "BlockDeviceMappings": [{"Ebs": {"VolumeId": v}} for v in volumes] + [{"DeviceName": "x"}],
        }

    instances = {
        "Reservations": [
            {
                "Instances": [
                    instance("i-1", "User initiated (2026-10-01 10:00:00 GMT)", "vol-a", "vol-b"),
                    instance("i-2", ""),  # no reason to parse: reported
                    instance("i-new", "User initiated (2026-11-21 10:00:00 GMT)", "vol-c"),
                ]
            }
        ]
    }
    volumes = {
        "Volumes": [
            {"VolumeId": "vol-a", "Size": 10, "VolumeType": "gp3"},
            {"VolumeId": "vol-b", "Size": 10, "VolumeType": "gp2"},
        ]
    }
    ec2 = Client({"describe_instances": [instances], "describe_volumes": [volumes]})
    found = waste.stopped_instances(ec2, "us-east-2", NOW)
    assert [(f["id"], f["monthly"], f["detail"]) for f in found] == [
        ("i-1", 1.8, "2 attached volume(s) still billed"),
        ("i-2", 0.0, "0 attached volume(s) still billed"),
    ]
    # Volumes are found by what they are attached to, so one deleted mid-scan cannot fail the check.
    assert ec2.calls[1][1]["Filters"] == [{"Name": "attachment.instance-id", "Values": ["i-1", "i-2"]}]


def test_old_snapshots_skip_recent_ones_and_ones_an_ami_needs() -> None:
    def snap(sid: str, started: dt.datetime) -> dict[str, Any]:
        return {"SnapshotId": sid, "StartTime": started, "VolumeSize": 6}

    images = {"Images": [{"BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-ami"}}, {"DeviceName": "x"}, {"Ebs": {}}]}]}
    snapshots = {
        "Snapshots": [
            snap("snap-old", NOW - dt.timedelta(days=100)),
            snap("snap-ami", OLD - dt.timedelta(days=200)),
            snap("snap-new", NOW - dt.timedelta(days=10)),
        ]
    }
    found = waste.old_snapshots(Client({"describe_images": [images], "describe_snapshots": [snapshots]}), "us-east-2", NOW)
    assert [(f["id"], f["monthly"], f["detail"]) for f in found] == [("snap-old", 0.3, "6 GiB, created Aug 14 (100 days)")]


def test_log_groups_without_retention() -> None:
    groups = {
        "logGroups": [
            {"logGroupName": "/a", "storedBytes": 42_000_000},
            {"logGroupName": "/b", "retentionInDays": 14},
            {"logGroupName": "/c"},
        ]
    }
    found = waste.no_retention(Client({"describe_log_groups": [groups]}), "us-east-2")
    assert [(f["id"], f["monthly"], f["detail"]) for f in found] == [
        ("/a", None, "42.0 MB stored, never expires"),
        ("/c", None, "0.0 MB stored, never expires"),
    ]


def test_untagged_means_no_project_tag_even_with_other_tags() -> None:
    resources = {
        "ResourceTagMappingList": [
            {"ResourceARN": "arn:aws:lambda:us-east-2:1:function:a", "Tags": [{"Key": "project", "Value": "x"}]},
            {"ResourceARN": "arn:aws:lambda:us-east-2:1:function:b", "Tags": [{"Key": "owner", "Value": "me"}]},
            {"ResourceARN": "arn:aws:iam::1:role/r", "Tags": []},
        ]
    }
    found = waste.untagged(Client({"get_resources": [resources]}), "us-east-2", "project")
    assert [(f["region"], f["id"], f["detail"]) for f in found] == [
        ("us-east-2", "arn:aws:lambda:us-east-2:1:function:b", "No project tag"),
        ("global", "arn:aws:iam::1:role/r", "No project tag"),
    ]


def _empty_world(
    monkeypatch: pytest.MonkeyPatch, broken: str | None = None, extra: dict[str, list[dict[str, Any]]] | None = None
) -> None:
    def make(name: str, region: str) -> Client:
        if name == broken:
            raise PermissionError("denied")
        pages = {
            "describe_nat_gateways": [{"NatGateways": []}],
            "describe_load_balancers": [{"LoadBalancers": []}],
            "describe_volumes": [{"Volumes": []}],
            "describe_instances": [{"Reservations": []}],
            "describe_images": [{"Images": []}],
            "describe_snapshots": [{"Snapshots": []}],
            "describe_log_groups": [{"logGroups": []}],
            "get_resources": [{"ResourceTagMappingList": []}],
        } | (extra or {})
        return Client(pages, addresses={"Addresses": []}, traffic={})

    monkeypatch.setattr(waste, "_client", make)


def test_scan_reports_a_failed_check_and_still_runs_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    _empty_world(monkeypatch, broken="elbv2", extra={"describe_log_groups": [{"logGroups": [{"logGroupName": "/a"}]}]})
    findings, errors, failed = waste.scan(NOW, "project")
    assert errors == ["idle-lb in us-east-1: PermissionError", "idle-lb in us-east-2: PermissionError"]
    assert failed == {("idle-lb", "us-east-1"), ("idle-lb", "us-east-2")}
    assert sorted(f["key"] for f in findings) == ["no-retention#us-east-1#/a", "no-retention#us-east-2#/a"]


def test_scan_lists_a_global_resource_once(monkeypatch: pytest.MonkeyPatch) -> None:
    role = {"ResourceTagMappingList": [{"ResourceARN": "arn:aws:iam::1:role/r", "Tags": []}]}
    _empty_world(monkeypatch, extra={"get_resources": [role]})
    findings, errors, failed = waste.scan(NOW, "project")
    assert errors == [] and [f["id"] for f in findings] == ["arn:aws:iam::1:role/r"]


class FakeTable:
    def __init__(self, item: dict[str, Any] | None = None) -> None:
        self.item = item
        self.puts: list[dict[str, Any]] = []
        self.log: list[str] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"Key": {"PK": "WASTE", "SK": "LATEST"}, "ConsistentRead": True}
        return {"Item": self.item} if self.item else {}

    def put_item(self, **kwargs: Any) -> None:
        self.log.append("put")
        self.puts.append(kwargs["Item"])


def _finding(check: str = "idle-nat", resource: str = "nat-1", monthly: float | None = 32.85) -> dict[str, Any]:
    return waste._finding(check, "us-east-2", resource, "Idle NAT gateway", "0 bytes processed in 14 days", monthly)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    table: FakeTable,
    findings: list[dict[str, Any]],
    errors: list[str] | None = None,
    failed: set[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    monkeypatch.setenv("TOPIC_ARN", "arn:topic")
    monkeypatch.setattr(waste, "_table", lambda: table)
    monkeypatch.setattr(waste, "scan", lambda now, tag: (findings, list(errors or []), set(failed or [])))
    monkeypatch.setattr(waste, "_publish", lambda email: (table.log.append("email"), sent.append(email)))
    return sent


def test_first_scan_emails_everything_and_stores_first_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    table = FakeTable()
    sent = _run(monkeypatch, table, [_finding(), _finding("no-retention", "/a", None)])
    assert waste.handler({}, None) == {"findings": 2, "new": 2}
    assert len(sent) == 1 and sent[0][0].startswith("CostWatcher waste scan: 2 new finding(s), about $32.85/month")
    assert table.log == ["email", "put"]  # a failed email must leave the findings "new" for the next run
    stored = table.puts[0]
    assert (stored["PK"], stored["SK"], stored["total"], stored["newCount"]) == ("WASTE", "LATEST", 2, 2)
    assert {f["firstSeen"] for f in stored["findings"]} == {NOW.date().isoformat()} or len(
        {f["firstSeen"] for f in stored["findings"]}
    ) == 1
    assert stored["monthly"] == Decimal("32.85") and stored["counts"]["idle-nat"] == 1 and stored["counts"]["idle-lb"] == 0


def test_a_known_finding_keeps_its_first_seen_and_sends_no_email(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = {"findings": [{**_finding(), "firstSeen": "2026-11-08", "new": True}]}
    table = FakeTable(previous)
    sent = _run(monkeypatch, table, [_finding()])
    assert waste.handler({}, None) == {"findings": 1, "new": 0}
    assert sent == []
    stored = table.puts[0]["findings"][0]
    assert stored["firstSeen"] == "2026-11-08" and stored["new"] is False and table.puts[0]["newCount"] == 0


def test_only_new_findings_are_emailed(monkeypatch: pytest.MonkeyPatch) -> None:
    table = FakeTable({"findings": [{**_finding(), "firstSeen": "2026-11-08"}]})
    sent = _run(monkeypatch, table, [_finding(), _finding("unattached-eip", "eipalloc-1", 3.65)])
    assert waste.handler({}, None) == {"findings": 2, "new": 1}
    assert "eipalloc-1" in sent[0][1] and "nat-1" not in sent[0][1]


def test_failed_checks_email_and_fail_the_run_after_saving(monkeypatch: pytest.MonkeyPatch) -> None:
    table = FakeTable()
    sent = _run(monkeypatch, table, [], ["idle-nat in us-east-1: PermissionError"])
    with pytest.raises(RuntimeError, match="1 waste problems"):
        waste.handler({}, None)
    assert sent[0][0] == "CostWatcher waste scan: some checks failed" and "Could not check: idle-nat in us-east-1" in sent[0][1]
    assert table.puts[0]["errors"] == ["idle-nat in us-east-1: PermissionError"]  # the dashboard still gets the partial scan


def test_a_quiet_scan_sends_nothing_but_still_records_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    table = FakeTable()
    sent = _run(monkeypatch, table, [])
    assert waste.handler({}, None) == {"findings": 0, "new": 0}
    assert sent == [] and table.puts[0]["total"] == 0 and table.puts[0]["findings"] == []


def test_a_long_email_lists_the_largest_and_counts_the_rest() -> None:
    new = [_finding("unattached-ebs", f"vol-{i}", float(i)) for i in range(30)]
    subject, message = waste._email(new, [], NOW)
    lines = message.splitlines()
    assert lines[2].startswith("- Idle NAT gateway: vol-29") and "... and 5 more, smaller or without a direct cost." in lines
    assert len([line for line in lines if line.startswith("- ")]) == waste.EMAILED
    assert "30 new finding(s)" in subject
    assert "no direct cost" in waste._email([_finding("no-retention", "/a", None)], [], NOW)[1]


def test_stored_item_is_capped_and_ranked_but_counts_everything() -> None:
    findings = [_finding("unattached-ebs", f"vol-{i:04d}", i / 100) for i in range(waste.LISTED + 20)]
    item = waste._item(NOW, [{**f, "firstSeen": "2026-11-22"} for f in findings], [], [])
    assert len(item["findings"]) == waste.LISTED and item["total"] == waste.LISTED + 20
    assert item["findings"][0]["id"] == f"vol-{waste.LISTED + 19:04d}" and item["counts"]["unattached-ebs"] == waste.LISTED + 20
    assert isinstance(item["findings"][0]["monthly"], Decimal)  # DynamoDB takes Decimal, not float
    assert item["scannedAt"] == "2026-11-22T15:00:00+00:00"


def test_clients_and_table_use_the_regions_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[Any] = []

    def client(name: str, **kwargs: Any) -> Any:
        made.append((name, kwargs))
        return types.SimpleNamespace(publish=lambda **kw: made.append(("published", kw)))

    fake = types.SimpleNamespace(client=client, resource=lambda name: types.SimpleNamespace(Table=lambda t: ("table", t)))
    monkeypatch.setitem(sys.modules, "boto3", fake)  # the Lambda runtime has boto3; the test environment does not
    monkeypatch.setenv("TABLE_NAME", "costs")
    monkeypatch.setenv("TOPIC_ARN", "arn:topic")
    waste._client("ec2", "us-east-1")
    assert waste._table() == ("table", "costs")
    waste._publish(("subject", "body"))
    assert made == [
        ("ec2", {"region_name": "us-east-1"}),
        ("sns", {}),
        ("published", {"TopicArn": "arn:topic", "Subject": "subject", "Message": "body"}),
    ]


def test_findings_cut_from_the_stored_list_are_not_emailed_again(monkeypatch: pytest.MonkeyPatch) -> None:
    many = [_finding("untagged", f"arn:{i:04d}", None) for i in range(waste.LISTED + 5)]
    table = FakeTable()
    _run(monkeypatch, table, [dict(f) for f in many])
    waste.handler({}, None)
    stored = table.puts[0]
    assert len(stored["findings"]) == waste.LISTED and len(stored["seen"]) == waste.LISTED + 5
    table2 = FakeTable(stored)
    sent = _run(monkeypatch, table2, [dict(f) for f in many])
    assert waste.handler({}, None) == {"findings": waste.LISTED + 5, "new": 0} and sent == []


def test_a_failed_check_keeps_its_previous_findings_instead_of_reporting_them_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    nat, eip, untagged = (
        _finding(),
        _finding("unattached-eip", "eipalloc-1", 3.65),
        _finding("untagged", "arn:aws:iam::1:role/r", None),
    )
    previous = {"findings": [{**f, "firstSeen": "2026-11-08"} for f in (nat, eip, untagged)]}
    table = FakeTable(previous)
    # The NAT check failed in us-east-2 and the untagged check failed in us-east-1; the EIP check ran and found nothing.
    sent = _run(
        monkeypatch, table, [], ["idle-nat in us-east-2: Throttling"], {("idle-nat", "us-east-2"), ("untagged", "us-east-1")}
    )
    with pytest.raises(RuntimeError):
        waste.handler({}, None)
    kept = {f["id"]: f for f in table.puts[0]["findings"]}
    assert set(kept) == {"nat-1", "arn:aws:iam::1:role/r"}  # the EIP is genuinely gone
    assert kept["nat-1"]["firstSeen"] == "2026-11-08" and kept["nat-1"]["new"] is False
    assert "Could not check" in sent[0][1] and "Idle NAT gateway: nat-1" not in sent[0][1]


def test_an_email_that_fails_still_saves_the_scan_and_retries_those_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    table = FakeTable()
    _run(monkeypatch, table, [_finding()])

    def broken(email: tuple[str, str]) -> None:
        raise RuntimeError("sns down")

    monkeypatch.setattr(waste, "_publish", broken)
    with pytest.raises(RuntimeError, match="email: RuntimeError"):
        waste.handler({}, None)
    stored = table.puts[0]
    assert (
        stored["total"] == 1 and stored["errors"] == ["email: RuntimeError"] and stored["unsent"] == ["idle-nat#us-east-2#nat-1"]
    )
    table2 = FakeTable(stored)  # next week SNS works: the finding is new again, so it is finally emailed
    sent = _run(monkeypatch, table2, [_finding()])
    assert waste.handler({}, None) == {"findings": 1, "new": 1} and len(sent) == 1 and table2.puts[0]["unsent"] == []
