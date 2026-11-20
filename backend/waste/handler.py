"""Weekly waste scan: idle or unused resources in us-east-1 and us-east-2, read-only.

Findings are suggestions with a list-price monthly estimate. The latest scan is one DynamoDB item (PK WASTE, SK LATEST);
the dashboard reads it, and only findings absent from the previous scan are emailed, once.
"""

import datetime as dt
import json
import os
import re
from decimal import Decimal
from typing import Any

REGIONS = ("us-east-1", "us-east-2")
IDLE_DAYS = 14  # a NAT gateway or load balancer must be this old and have moved no bytes in this long
SNAPSHOT_DAYS = 90
SEEN_LIMIT = 2000  # first-seen dates kept (about 100 bytes each); a finding past this is emailed again when the scan sees it
LISTED = 300  # findings kept, largest estimate first, so the one item stays far under the 400 KB limit
EMAILED = 25  # lines in one email; the rest are counted
HOURS_PER_MONTH = 730
# List prices in us-east-1 and us-east-2 (per hour, or per GB-month); estimates, not a bill.
NAT_MONTHLY = round(0.045 * HOURS_PER_MONTH, 2)
LOAD_BALANCER_MONTHLY = round(0.0225 * HOURS_PER_MONTH, 2)
ELASTIC_IP_MONTHLY = round(0.005 * HOURS_PER_MONTH, 2)
SNAPSHOT_GB_MONTH = 0.05  # an upper bound: snapshots are incremental
VOLUME_GB_MONTH = {"gp3": 0.08, "gp2": 0.10, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015, "standard": 0.05}
CHECKS = (
    ("idle-nat", "Idle NAT gateways"),
    ("idle-lb", "Idle load balancers"),
    ("unattached-ebs", "Unattached EBS volumes"),
    ("unattached-eip", "Unattached Elastic IPs"),
    ("stopped-instance", "Stopped instances"),
    ("old-snapshot", "Old snapshots (90+ days)"),
    ("no-retention", "Log groups, no retention"),
    ("untagged", "Untagged resources"),
)
KEY = {"PK": "WASTE", "SK": "LATEST"}


def _client(name: str, region: str) -> Any:
    import boto3  # pyright: ignore[reportMissingImports]  # in the Lambda runtime; imported late so tests need not have it

    return boto3.client(name, region_name=region)


def _table() -> Any:
    import boto3  # pyright: ignore[reportMissingImports]

    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    table = _table()
    previous = table.get_item(Key=KEY, ConsistentRead=True).get("Item") or {}
    findings, errors, failed = scan(now, os.environ.get("PROJECT_TAG", "project"))
    # `seen` holds every finding's first-seen date, not just the stored top LISTED, so a cut-off finding is not "new" weekly.
    seen = previous.get("seen") or {f["key"]: f.get("firstSeen") for f in previous.get("findings", [])}
    known = {key for key in seen if key not in set(previous.get("unsent", []))}  # an email that failed leaves its findings new
    today = now.date().isoformat()
    for f in findings:
        f["firstSeen"] = seen.get(f["key"]) or today
        f["new"] = f["key"] not in known
    current = {f["key"] for f in findings}
    # A check that failed this week says nothing about its findings: keep last week's rather than report them gone.
    findings += [{**f, "new": False} for f in previous.get("findings", []) if f["key"] not in current and _failed(f, failed)]
    new = [f for f in findings if f["new"]]
    unsent: list[str] = []
    if new or errors:  # nothing new and nothing broken: no email
        try:
            _publish(_email(new, errors, now))
        except Exception as error:  # noqa: BLE001  # the scan is still saved, so the dashboard is not left stale
            errors.append(f"email: {type(error).__name__}")
            unsent = [f["key"] for f in new]
    table.put_item(Item=_item(now, findings, errors, unsent))
    if errors:
        raise RuntimeError(f"{len(errors)} waste problems: {'; '.join(errors)[:500]}")  # fails the run, so the alarm emails
    return {"findings": len(findings), "new": len(new)}


def _failed(finding: dict[str, Any], failed: set[tuple[str, str]]) -> bool:
    """Untagged findings carry their ARN's region (or "global"), not the region that was scanned, so match on the check."""
    if finding["check"] == "untagged":
        return any(check == "untagged" for check, _ in failed)
    return (finding["check"], finding["region"]) in failed


def scan(now: dt.datetime, tag_key: str) -> tuple[list[dict[str, Any]], list[str], set[tuple[str, str]]]:
    """Every check in every region. A check that fails is reported (and listed as failed) and the others still run."""
    findings: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    failed: set[tuple[str, str]] = set()
    for region in REGIONS:
        found, region_errors = _scan_region(region, now, tag_key)
        for f in found:
            findings.setdefault(f["key"], f)  # global resources (IAM, CloudFront) show up in both regions
        errors += [message for _, message in region_errors]
        failed |= {(check, region) for check, _ in region_errors}
    return list(findings.values()), errors, failed


def _scan_region(region: str, now: dt.datetime, tag_key: str) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    ec2, cloudwatch = _client("ec2", region), _client("cloudwatch", region)
    runs = [
        ("idle-nat", lambda: idle_nat_gateways(ec2, cloudwatch, region, now)),
        ("idle-lb", lambda: idle_load_balancers(_client("elbv2", region), cloudwatch, region, now)),
        ("unattached-ebs", lambda: unattached_volumes(ec2, region)),
        ("unattached-eip", lambda: unattached_addresses(ec2, region)),
        ("stopped-instance", lambda: stopped_instances(ec2, region, now)),
        ("old-snapshot", lambda: old_snapshots(ec2, region, now)),
        ("no-retention", lambda: no_retention(_client("logs", region), region)),
        ("untagged", lambda: untagged(_client("resourcegroupstaggingapi", region), region, tag_key)),
    ]
    found: list[dict[str, Any]] = []
    errors: list[tuple[str, str]] = []
    for check, run in runs:
        try:
            found += run()
        except Exception as error:  # noqa: BLE001  # one denied or failing API must not hide every other check
            errors.append((check, f"{check} in {region}: {type(error).__name__}"))
    return found, errors


def _finding(check: str, region: str, resource: str, summary: str, detail: str, monthly: float | None) -> dict[str, Any]:
    return {
        "key": f"{check}#{region}#{resource}",
        "check": check,
        "region": region,
        "id": resource,
        "summary": summary,
        "detail": detail,
        "monthly": None if monthly is None else round(monthly, 2),
    }


def _pages(client: Any, operation: str, **kwargs: Any) -> Any:
    yield from client.get_paginator(operation).paginate(**kwargs)


def _moved_nothing(cloudwatch: Any, namespace: str, metric: str, dimensions: list[dict[str, str]], now: dt.datetime) -> bool:
    stats = cloudwatch.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric,
        Dimensions=dimensions,
        StartTime=now - dt.timedelta(days=IDLE_DAYS),
        EndTime=now,
        Period=86400,
        Statistics=["Sum"],
    )
    return sum(p["Sum"] for p in stats["Datapoints"]) == 0


def _old_enough(created: dt.datetime, now: dt.datetime) -> bool:
    return now - created >= dt.timedelta(days=IDLE_DAYS)


def idle_nat_gateways(ec2: Any, cloudwatch: Any, region: str, now: dt.datetime) -> list[dict[str, Any]]:
    found = []
    for page in _pages(ec2, "describe_nat_gateways", Filter=[{"Name": "state", "Values": ["available"]}]):
        for nat in page["NatGateways"]:
            dimensions = [{"Name": "NatGatewayId", "Value": nat["NatGatewayId"]}]
            if _old_enough(nat["CreateTime"], now) and _moved_nothing(
                cloudwatch, "AWS/NATGateway", "BytesOutToDestination", dimensions, now
            ):
                found.append(
                    _finding(
                        "idle-nat",
                        region,
                        nat["NatGatewayId"],
                        "Idle NAT gateway",
                        f"0 bytes processed in {IDLE_DAYS} days",
                        NAT_MONTHLY,
                    )
                )
    return found


def idle_load_balancers(elb: Any, cloudwatch: Any, region: str, now: dt.datetime) -> list[dict[str, Any]]:
    """Application and network balancers (Classic ELBs are a separate API and are not checked; see ROADMAP)."""
    found = []
    for page in _pages(elb, "describe_load_balancers"):
        for lb in page["LoadBalancers"]:
            if lb["Type"] not in ("application", "network") or not _old_enough(lb["CreatedTime"], now):
                continue
            namespace = "AWS/ApplicationELB" if lb["Type"] == "application" else "AWS/NetworkELB"
            dimensions = [{"Name": "LoadBalancer", "Value": lb["LoadBalancerArn"].split("loadbalancer/", 1)[1]}]
            if _moved_nothing(cloudwatch, namespace, "ProcessedBytes", dimensions, now):
                found.append(
                    _finding(
                        "idle-lb",
                        region,
                        lb["LoadBalancerName"],
                        "Idle load balancer",
                        f"0 bytes processed in {IDLE_DAYS} days",
                        LOAD_BALANCER_MONTHLY,
                    )
                )
    return found


def _volume_monthly(volume: dict[str, Any]) -> float:
    return volume["Size"] * VOLUME_GB_MONTH.get(volume["VolumeType"], 0.10)


def unattached_volumes(ec2: Any, region: str) -> list[dict[str, Any]]:
    found = []
    for page in _pages(ec2, "describe_volumes", Filters=[{"Name": "status", "Values": ["available"]}]):
        for v in page["Volumes"]:
            detail = f"{v['Size']} GiB {v['VolumeType']}, not attached to anything"
            found.append(_finding("unattached-ebs", region, v["VolumeId"], "Unattached EBS volume", detail, _volume_monthly(v)))
    return found


def unattached_addresses(ec2: Any, region: str) -> list[dict[str, Any]]:
    """describe_addresses has no paginator and returns every address at once."""
    return [
        _finding(
            "unattached-eip",
            region,
            a["AllocationId"],
            "Unattached Elastic IP",
            "Not associated with anything",
            ELASTIC_IP_MONTHLY,
        )
        for a in ec2.describe_addresses()["Addresses"]
        if "AssociationId" not in a and a.get("Domain", "vpc") == "vpc" and "AllocationId" in a
    ]


def stopped_instances(ec2: Any, region: str, now: dt.datetime) -> list[dict[str, Any]]:
    """Instances stopped for IDLE_DAYS or more. A stopped instance costs only its disks; the estimate is those at list price."""
    stopped: dict[str, list[str]] = {}
    for page in _pages(ec2, "describe_instances", Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]):
        for reservation in page["Reservations"]:
            for i in reservation["Instances"]:
                if _stopped_recently(i.get("StateTransitionReason", ""), now):
                    continue
                stopped[i["InstanceId"]] = [m["Ebs"]["VolumeId"] for m in i.get("BlockDeviceMappings", []) if "Ebs" in m]
    costs: dict[str, float] = {}
    ids = list(stopped)
    for start in range(0, len(ids), 200):  # filtering by attachment, not volume ID, so a volume deleted mid-scan is not an error
        chunk = ids[start : start + 200]
        for page in _pages(ec2, "describe_volumes", Filters=[{"Name": "attachment.instance-id", "Values": chunk}]):
            costs.update({v["VolumeId"]: _volume_monthly(v) for v in page["Volumes"]})
    return [
        _finding(
            "stopped-instance",
            region,
            instance,
            "Stopped instance",
            f"{len(volumes)} attached volume(s) still billed",
            sum(costs.get(v, 0) for v in volumes),
        )
        for instance, volumes in stopped.items()
    ]


STOPPED_AT = re.compile(r"\((\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) GMT\)")


def _stopped_recently(reason: str, now: dt.datetime) -> bool:
    """`User initiated (2026-11-01 10:00:00 GMT)`. A reason that doesn't parse counts as not recent: report it."""
    match = STOPPED_AT.search(reason)
    if match is None:
        return False
    stopped = dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.UTC)
    return not _old_enough(stopped, now)


def old_snapshots(ec2: Any, region: str, now: dt.datetime) -> list[dict[str, Any]]:
    """Snapshots this account owns, older than SNAPSHOT_DAYS, that no owned AMI still needs."""
    in_use = {
        m["Ebs"]["SnapshotId"]
        for page in _pages(ec2, "describe_images", Owners=["self"])
        for image in page["Images"]
        for m in image.get("BlockDeviceMappings", [])
        if "Ebs" in m and "SnapshotId" in m["Ebs"]
    }
    found = []
    for page in _pages(ec2, "describe_snapshots", OwnerIds=["self"]):
        for s in page["Snapshots"]:
            age = (now - s["StartTime"]).days
            if age >= SNAPSHOT_DAYS and s["SnapshotId"] not in in_use:
                detail = f"{s['VolumeSize']} GiB, created {s['StartTime']:%b} {s['StartTime'].day} ({age} days)"
                found.append(
                    _finding("old-snapshot", region, s["SnapshotId"], "Old snapshot", detail, s["VolumeSize"] * SNAPSHOT_GB_MONTH)
                )
    return found


def no_retention(logs: Any, region: str) -> list[dict[str, Any]]:
    found = []
    for page in _pages(logs, "describe_log_groups"):
        for g in page["logGroups"]:
            if "retentionInDays" not in g:
                megabytes = g.get("storedBytes", 0) / 1_000_000
                found.append(
                    _finding(
                        "no-retention",
                        region,
                        g["logGroupName"],
                        "Log group, no retention",
                        f"{megabytes:.1f} MB stored, never expires",
                        None,
                    )
                )
    return found


def untagged(tagging: Any, region: str, tag_key: str) -> list[dict[str, Any]]:
    found = []
    for page in _pages(tagging, "get_resources"):
        for r in page["ResourceTagMappingList"]:
            if tag_key not in {t["Key"] for t in r["Tags"]}:
                arn = r["ResourceARN"]
                found.append(
                    _finding("untagged", arn.split(":")[3] or "global", arn, "Untagged resource", f"No {tag_key} tag", None)
                )
    return found


def _email(new: list[dict[str, Any]], errors: list[str], now: dt.datetime) -> tuple[str, str]:
    saving = sum(f["monthly"] or 0 for f in new)
    subject = (
        f"CostWatcher waste scan: {len(new)} new finding(s), about ${saving:,.2f}/month"
        if new
        else "CostWatcher waste scan: some checks failed"
    )
    lines = [f"Scan of {', '.join(REGIONS)} on {now:%Y-%m-%d}. Suggestions only; nothing was changed.", ""]
    for f in sorted(new, key=lambda f: -(f["monthly"] or 0))[:EMAILED]:
        cost = f"${f['monthly']:,.2f}/month" if f["monthly"] is not None else "no direct cost"
        lines.append(f"- {f['summary']}: {f['id']} ({f['region']}), {f['detail']}, {cost}")
    if len(new) > EMAILED:
        lines.append(f"... and {len(new) - EMAILED} more, smaller or without a direct cost.")
    if errors:
        lines += ["", "Could not check: " + "; ".join(errors)]
    return subject[:100], "\n".join(lines)


def _publish(email: tuple[str, str]) -> None:
    import boto3  # pyright: ignore[reportMissingImports]

    boto3.client("sns").publish(TopicArn=os.environ["TOPIC_ARN"], Subject=email[0], Message=email[1])


def _item(now: dt.datetime, findings: list[dict[str, Any]], errors: list[str], unsent: list[str]) -> dict[str, Any]:
    """Largest estimate first; counts cover every finding even when the list is cut to LISTED. `seen` covers them too."""
    ranked = sorted(findings, key=lambda f: (-(f["monthly"] or 0), f["check"], f["id"]))
    counts = {check: sum(1 for f in findings if f["check"] == check) for check, _ in CHECKS}
    monthly = {check: round(sum(f["monthly"] or 0 for f in findings if f["check"] == check), 2) for check, _ in CHECKS}
    body = {
        "scannedAt": now.isoformat(timespec="seconds"),
        "total": len(findings),
        "newCount": sum(1 for f in findings if f.get("new")),
        "monthly": round(sum(f["monthly"] or 0 for f in findings), 2),
        "counts": counts,
        "checkMonthly": monthly,
        "errors": errors,
        "unsent": unsent,
        "findings": ranked[:LISTED],
        "seen": dict(sorted((f["key"], f["firstSeen"]) for f in findings)[:SEEN_LIMIT]),
    }
    return KEY | json.loads(json.dumps(body), parse_float=Decimal)
