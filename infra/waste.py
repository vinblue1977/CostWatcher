"""The weekly waste scan: a read-only Lambda over EC2, CloudWatch, Logs and the tagging API in two regions."""

from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

# Every call the scan makes. All are read-only, and none of these APIs take resource-level permissions.
SCAN_ACTIONS = [
    "ec2:DescribeNatGateways",
    "ec2:DescribeVolumes",
    "ec2:DescribeAddresses",
    "ec2:DescribeInstances",
    "ec2:DescribeSnapshots",
    "ec2:DescribeImages",
    "elasticloadbalancing:DescribeLoadBalancers",
    "cloudwatch:GetMetricStatistics",
    "logs:DescribeLogGroups",
    "tag:GetResources",
]


class Waste(Construct):
    def __init__(
        self, scope: Construct, construct_id: str, *, table: dynamodb.ITable, topic: sns.ITopic, project_tag: str
    ) -> None:
        super().__init__(scope, construct_id)
        fn = lambda_.Function(
            self,
            "Scan",
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="waste.handler.handler",
            code=lambda_.Code.from_asset("backend", exclude=["**/__pycache__"]),
            memory_size=256,
            timeout=Duration.minutes(5),
            environment={"TABLE_NAME": table.table_name, "TOPIC_ARN": topic.topic_arn, "PROJECT_TAG": project_tag},
            log_group=logs.LogGroup(self, "ScanLogs", retention=logs.RetentionDays.TWO_WEEKS),
        )
        table.grant(fn, "dynamodb:GetItem", "dynamodb:PutItem")  # one item: the latest scan
        topic.grant_publish(fn)  # pyright: ignore[reportArgumentType]
        fn.add_to_role_policy(iam.PolicyStatement(actions=SCAN_ACTIONS, resources=["*"]))

        # Sunday 15:00 UTC: 9:00 in Central standard time, 10:00 in daylight time.
        events.Rule(
            self,
            "Weekly",
            schedule=events.Schedule.cron(minute="0", hour="15", week_day="SUN"),
            targets=[targets.LambdaFunction(fn, retry_attempts=0)],  # pyright: ignore[reportArgumentType]  # a retry would repeat the emails
        )
        # A failed or partly failed scan raises, so one error emails the alert topic.
        fn.metric_errors(period=Duration.hours(1)).create_alarm(
            self,
            "ScanErrors",
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="The weekly waste scan failed or could not run every check",
        ).add_alarm_action(cw_actions.SnsAction(topic))  # pyright: ignore[reportArgumentType]

        NagSuppressions.add_resource_suppressions(
            fn,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="Basic execution role only writes this function's logs",
                    applies_to=["Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
                ),
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Read-only Describe, List and Get calls that accept no resource ARN",
                    applies_to=["Resource::*"],
                ),
            ],
            apply_to_children=True,
        )
