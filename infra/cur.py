"""The cost data pipeline: export bucket, Athena over it, and the daily Lambda that fills the table."""

from aws_cdk import Aws, Duration, RemovalPolicy, Stack
from aws_cdk import aws_athena as athena
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from cdk_nag import NagPackSuppression, NagSuppressions, RegexAppliesTo
from constructs import Construct

EXPORT_NAME = "costwatcher"
EXPORT_PREFIX = "cur"  # Data Exports writes <prefix>/<export name>/data/BILLING_PERIOD=<yyyy-MM>/*.parquet
DATABASE = "costwatcher"
TABLE = "cur"
WORKGROUP = "costwatcher"
SCAN_LIMIT_BYTES = 1_000_000_000  # a month of one account's Parquet is megabytes; this stops a runaway query

# The export's columns (see QUERY below): the Glue table must list the same names and types.
COLUMNS = (
    ("line_item_usage_start_date", "timestamp"),
    ("line_item_product_code", "string"),
    ("line_item_usage_type", "string"),
    ("line_item_resource_id", "string"),
    ("line_item_line_item_type", "string"),
    ("line_item_unblended_cost", "double"),
    ("resource_tags", "map<string,string>"),
)
QUERY = f"SELECT {', '.join(name for name, _ in COLUMNS)} FROM COST_AND_USAGE_REPORT"  # noqa: S608  # constants


def bucket_name() -> str:
    """Fixed, so the us-east-1 export stack can name the bucket without a cross-region reference."""
    return f"costwatcher-cur-{Aws.ACCOUNT_ID}"


class Cur(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        topic: sns.ITopic,
        project_tag: str,
        budget_usd: float,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)

        # Billing history can't be re-fetched (no backfill), so the bucket is kept if the stack is deleted.
        bucket = s3.Bucket(
            self,
            "Bucket",
            bucket_name=bucket_name(),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[s3.LifecycleRule(prefix="athena/", expiration=Duration.days(1))],  # query results
        )
        # Billing's export services write into this bucket, only for this account's exports (confused-deputy guard).
        delivery = iam.PolicyStatement(
            actions=["s3:GetBucketPolicy", "s3:PutObject"],
            # The whole bucket, as AWS's required policy has it: its create-time check fails on a narrower prefix.
            resources=[bucket.bucket_arn, bucket.arn_for_objects("*")],
            principals=[iam.ServicePrincipal(p) for p in ("bcm-data-exports.amazonaws.com", "billingreports.amazonaws.com")],  # pyright: ignore[reportArgumentType]
            conditions={
                "StringEquals": {"aws:SourceAccount": stack.account},
                "ArnLike": {
                    "aws:SourceArn": [
                        f"arn:{stack.partition}:bcm-data-exports:us-east-1:{stack.account}:export/*",
                        f"arn:{stack.partition}:cur:us-east-1:{stack.account}:definition/*",
                    ]
                },
            },
        )
        bucket.add_to_resource_policy(delivery)

        database = glue.CfnDatabase(
            self, "Database", catalog_id=stack.account, database_input=glue.CfnDatabase.DatabaseInputProperty(name=DATABASE)
        )
        location = f"s3://{bucket.bucket_name}/{EXPORT_PREFIX}/{EXPORT_NAME}/data"
        glue_table = glue.CfnTable(
            self,
            "Table",
            catalog_id=stack.account,
            database_name=DATABASE,
            table_input=glue.CfnTable.TableInputProperty(
                name=TABLE,
                table_type="EXTERNAL_TABLE",
                partition_keys=[glue.CfnTable.ColumnProperty(name="billing_period", type="string")],
                # Partition projection: Athena computes the monthly partitions, so there is no crawler to run or pay for.
                parameters={
                    "classification": "parquet",
                    "projection.enabled": "true",
                    "projection.billing_period.type": "date",
                    "projection.billing_period.format": "yyyy-MM",
                    "projection.billing_period.range": "2025-01,NOW",
                    "projection.billing_period.interval": "1",
                    "projection.billing_period.interval.unit": "MONTHS",
                    "storage.location.template": f"{location}/BILLING_PERIOD=${{billing_period}}",
                },
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    columns=[glue.CfnTable.ColumnProperty(name=n, type=t) for n, t in COLUMNS],
                    location=f"{location}/",
                    input_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
                    ),
                ),
            ),
        )
        glue_table.add_resource_dependency(database)

        workgroup = athena.CfnWorkGroup(
            self,
            "Workgroup",
            name=WORKGROUP,
            recursive_delete_option=True,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                bytes_scanned_cutoff_per_query=SCAN_LIMIT_BYTES,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{bucket.bucket_name}/athena/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(encryption_option="SSE_S3"),
                ),
            ),
        )

        fn = lambda_.Function(
            self,
            "Ingest",
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="ingest.handler.handler",
            code=lambda_.Code.from_asset("backend", exclude=["**/__pycache__"]),
            memory_size=256,
            timeout=Duration.minutes(5),
            environment={
                "TABLE_NAME": table.table_name,
                "ATHENA_WORKGROUP": WORKGROUP,
                "ATHENA_DATABASE": DATABASE,
                "ATHENA_TABLE": TABLE,
                "PROJECT_TAG": project_tag,
                "TOPIC_ARN": topic.topic_arn,
                "BUDGET_USD": str(budget_usd),
            },
            log_group=logs.LogGroup(self, "IngestLogs", retention=logs.RetentionDays.TWO_WEEKS),
        )
        fn.node.add_dependency(workgroup)
        # The only writer; the API Lambda stays read-only. Alert markers are put once and deleted if the email fails;
        # Query reads last month's stored days so early-month alerts have a full baseline.
        table.grant(fn, "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query")
        topic.grant_publish(fn)  # pyright: ignore[reportArgumentType]
        bucket.grant_read(fn, f"{EXPORT_PREFIX}/*")
        bucket.grant_read_write(fn, "athena/*")
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "athena:StartQueryExecution",
                    "athena:StopQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                ],
                resources=[f"arn:{stack.partition}:athena:{stack.region}:{stack.account}:workgroup/{WORKGROUP}"],
            )
        )
        # Names the project of the untagged API stages (read-only; the tagging API has no resource-level permissions).
        fn.add_to_role_policy(iam.PolicyStatement(actions=["tag:GetResources"], resources=["*"]))
        glue_arn = f"arn:{stack.partition}:glue:{stack.region}:{stack.account}"
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"],
                resources=[f"{glue_arn}:catalog", f"{glue_arn}:database/{DATABASE}", f"{glue_arn}:table/{DATABASE}/{TABLE}"],
            )
        )

        # Once a day, after the export's usual morning refreshes.
        events.Rule(
            self,
            "Daily",
            schedule=events.Schedule.cron(minute="30", hour="8"),
            targets=[targets.LambdaFunction(fn)],  # pyright: ignore[reportArgumentType]
        )
        # Sunday mornings: the same Lambda emails the weekly digest. The newest stored day is usually Saturday (the export
        # lags), sometimes earlier; the email names the 7 days it covers and says when some have no data.
        events.Rule(
            self,
            "Weekly",
            schedule=events.Schedule.cron(minute="0", hour="9", week_day="SUN"),
            targets=[targets.LambdaFunction(fn, event=events.RuleTargetInput.from_object({"digest": True}))],  # pyright: ignore[reportArgumentType]
        )
        # A failed run leaves the dashboard a day stale, so one failure emails the alert topic.
        fn.metric_errors(period=Duration.hours(1)).create_alarm(
            self,
            "IngestErrors",
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="The daily cost ingest failed",
        ).add_alarm_action(cw_actions.SnsAction(topic))  # pyright: ignore[reportArgumentType]

        NagSuppressions.add_resource_suppressions(
            bucket, [NagPackSuppression(id="AwsSolutions-S1", reason="Billing exports only; access logging costs extra")]
        )
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
                    reason="Object-level access under the export and Athena result prefixes of one bucket",
                    applies_to=[RegexAppliesTo(regex=r"/^(Action::s3:.*\*|Resource::.*\/(cur|athena)\/\*)$/g")],
                ),
            ],
            apply_to_children=True,
        )
