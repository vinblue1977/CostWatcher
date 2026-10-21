"""Infrastructure tests: the stack synthesizes, passes cdk-nag, and its table, API and deploy role are locked down."""

import functools
import json
import shutil
import subprocess
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template
from cdk_nag import AwsSolutionsChecks

from infra.cur import COLUMNS, QUERY
from infra.export_stack import CurExportStack
from infra.stack import CostWatcherStack
from infra.web import SPA_FALLBACK

REPO = "test-owner@1/test-repo@2"
# The same feature flags as a real synth, so tests render the template the way a deploy does.
CDK_CONTEXT = {k: v for k, v in json.loads(Path("cdk.json").read_text())["context"].items() if k.startswith("@")}
CDK_CONTEXT["siteDir"] = "tests/fixtures/site"  # a stand-in app, so tests don't need a frontend build


def _stack(context: dict | None = None, account: str | None = "123456789012") -> CostWatcherStack:
    app = cdk.App(context=CDK_CONTEXT | ({"githubRepo": REPO} if context is None else context))
    stack = CostWatcherStack(app, "CostWatcher", env=cdk.Environment(account=account, region="us-east-2"))
    cdk.Aspects.of(app).add(AwsSolutionsChecks())
    return stack


@functools.cache
def _synthesized(context: str | None = None, account: str | None = "123456789012") -> tuple[CostWatcherStack, Template]:
    """A stack and its template, built once per context (as JSON) and account: synthesizing is the slow part of these tests."""
    stack = _stack(None if context is None else json.loads(context), account)
    return stack, Template.from_stack(stack)


def _template(context: dict | None = None) -> Template:
    return _synthesized(None if context is None else json.dumps(context, sort_keys=True))[1]


def test_synthesizes_in_us_east_2() -> None:
    assert _synthesized()[0].region == "us-east-2"


@pytest.mark.parametrize("account", ["123456789012", None])  # None: CI synth without AWS credentials
def test_no_cdk_nag_findings(account: str | None) -> None:
    stack, _ = _synthesized(account=account)  # synthesizing runs the aspects
    annotations = Annotations.from_stack(stack)
    assert annotations.find_error("*", Match.string_like_regexp("AwsSolutions-.*")) == []
    assert annotations.find_warning("*", Match.string_like_regexp("AwsSolutions-.*")) == []


def test_table_is_protected_and_on_demand() -> None:
    _template().has_resource(
        "AWS::DynamoDB::GlobalTable",
        {
            "DeletionPolicy": "Retain",
            "Properties": Match.object_like(
                {
                    "BillingMode": "PAY_PER_REQUEST",
                    "Replicas": [
                        Match.object_like(
                            {
                                "DeletionProtectionEnabled": True,
                                "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
                            }
                        )
                    ],
                }
            ),
        },
    )


def test_deploy_role_trusts_only_this_repo_production_environment() -> None:
    template = _template()
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": Match.object_like(
                {
                    "Statement": [
                        Match.object_like(
                            {
                                "Action": "sts:AssumeRoleWithWebIdentity",
                                "Condition": {
                                    "StringEquals": {
                                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                                        "token.actions.githubusercontent.com:sub": f"repo:{REPO}:environment:production",
                                    }
                                },
                            }
                        )
                    ]
                }
            )
        },
    )
    # The provider is shared with other stacks in the account, so this stack must not create one.
    template.resource_count_is("Custom::AWSCDKOpenIdConnectProvider", 0)
    template.resource_count_is("AWS::IAM::OIDCProvider", 0)


def test_no_deploy_role_without_repo() -> None:
    web_identity = {
        "AssumeRolePolicyDocument": Match.object_like(
            {"Statement": [Match.object_like({"Action": "sts:AssumeRoleWithWebIdentity"})]}
        )
    }
    _template({}).resource_properties_count_is("AWS::IAM::Role", web_identity, 0)


def test_rejects_malformed_repo() -> None:
    with pytest.raises(ValueError, match="githubRepo"):
        _stack({"githubRepo": "not a repo"})


def test_api_alarms_and_budget() -> None:
    template = _template({"alertEmail": "alerts@example.com"})
    for metric in ("Errors", "Throttles"):
        template.has_resource_properties("AWS::CloudWatch::Alarm", {"MetricName": metric, "Threshold": 3, "Period": 300})
    template.has_resource_properties(
        "AWS::Budgets::Budget", {"Budget": Match.object_like({"BudgetLimit": {"Amount": 5, "Unit": "USD"}})}
    )
    template.has_resource_properties("AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "alerts@example.com"})
    _template({}).resource_count_is("AWS::Budgets::Budget", 0)


def test_api_routes_require_sign_in_and_site_is_private() -> None:
    template = _template()
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    by_key = {r["Properties"]["RouteKey"]: r["Properties"]["AuthorizationType"] for r in routes.values()}
    assert by_key == {"GET /costs/{month}": "JWT", "GET /tags/{month}": "JWT"}
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )
    template.has_resource_properties("AWS::Cognito::UserPool", {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}})
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        Match.object_like({"DistributionConfig": Match.object_like({"DefaultRootObject": "index.html"})}),
    )


def test_only_extensionless_paths_fall_back_to_the_app() -> None:
    template = _template()
    config = next(iter(template.find_resources("AWS::CloudFront::Distribution").values()))["Properties"]["DistributionConfig"]
    assert "CustomErrorResponses" not in config  # a missing file must stay a 403/404, not a 200 index.html
    template.has_resource_properties("AWS::CloudFront::Function", {"FunctionCode": SPA_FALLBACK})
    assert config["DefaultCacheBehavior"]["FunctionAssociations"][0]["EventType"] == "viewer-request"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runs the CloudFront Function")
def test_fallback_function_rewrites_by_extension() -> None:
    uris = ["/", "/overview", "/config.json", "/assets/index-abc123.js", "/assets/v1.2/app", "/favicon.ico", "/.well-known/x"]
    script = (
        SPA_FALLBACK
        + f"for (const u of {json.dumps(uris)}) console.log(handler({{request: {{uri: u, querystring: {{}}}}}}).uri);"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout  # noqa: S603, S607
    assert out.split() == [
        "/index.html",
        "/index.html",
        "/config.json",
        "/assets/index-abc123.js",
        "/index.html",
        "/favicon.ico",
        "/index.html",
    ]


def test_localhost_is_allowed_only_when_asked() -> None:
    def origins(template: Template) -> str:
        return json.dumps(
            [
                *(c["Properties"]["CallbackURLs"] for c in template.find_resources("AWS::Cognito::UserPoolClient").values()),
                *(a["Properties"]["CorsConfiguration"] for a in template.find_resources("AWS::ApiGatewayV2::Api").values()),
            ]
        )

    assert "localhost" not in origins(_template())
    assert "localhost:5173" in origins(_template({"devOrigin": "true"}))


def test_csp_allows_cognito_api_and_blocks_inline_styles() -> None:
    policies = _template().find_resources("AWS::CloudFront::ResponseHeadersPolicy")
    csp = json.dumps([p["Properties"] for p in policies.values()])
    assert csp.count("cognito-idp.us-east-2.amazonaws.com") == 1 and "style-src 'self';" in csp and "unsafe-inline" not in csp


def test_api_lambda_can_only_read_the_table() -> None:
    template = _template()
    api = next(
        f
        for f in template.find_resources("AWS::Lambda::Function").values()
        if f["Properties"]["Handler"] == "api.handler.handler"
    )
    assert "TABLE_NAME" in api["Properties"]["Environment"]["Variables"]
    policies = json.dumps({k: v for k, v in template.find_resources("AWS::IAM::Policy").items() if "ApiServiceRole" in k})
    assert "dynamodb:Query" in policies
    for broad in ("Scan", "GetItem", "BatchGetItem"):
        assert f"dynamodb:{broad}" not in policies
    for write in ("PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem"):
        assert f"dynamodb:{write}" not in policies


def test_only_the_ingest_lambda_writes_the_table() -> None:
    template = _template()
    writers = [k for k, v in template.find_resources("AWS::IAM::Policy").items() if "dynamodb:PutItem" in json.dumps(v)]
    assert len(writers) == 1 and "IngestServiceRole" in writers[0]


def test_export_bucket_is_private_kept_and_open_only_to_this_accounts_exports() -> None:
    template = _template()
    template.has_resource(
        "AWS::S3::Bucket",
        {
            "DeletionPolicy": "Retain",
            "Properties": Match.object_like(
                {
                    "BucketName": {"Fn::Join": ["", ["costwatcher-cur-", {"Ref": "AWS::AccountId"}]]},
                    "PublicAccessBlockConfiguration": Match.object_like({"BlockPublicPolicy": True}),
                }
            ),
        },
    )
    statements = [
        s
        for p in template.find_resources("AWS::S3::BucketPolicy").values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    # Only the billing export services may write, and only on behalf of this account's exports.
    services = {"bcm-data-exports.amazonaws.com", "billingreports.amazonaws.com"}
    delivery = next(s for s in statements if services & set(s.get("Principal", {}).get("Service", [])))
    assert set(delivery["Principal"]["Service"]) == services
    assert delivery["Condition"]["StringEquals"] == {"aws:SourceAccount": "123456789012"}
    assert "aws:SourceArn" in delivery["Condition"]["ArnLike"]
    # AWS's policy covers the whole bucket: the delivery statement's resources are the bucket and every object in it.
    bucket, objects = delivery["Resource"]
    assert bucket["Fn::GetAtt"][1] == "Arn"
    assert objects["Fn::Join"][1] == [bucket, "/*"]


def test_athena_is_capped_and_encrypted_and_reads_partitions_by_projection() -> None:
    template = _template()
    template.has_resource_properties(
        "AWS::Athena::WorkGroup",
        {
            "WorkGroupConfiguration": Match.object_like(
                {
                    "EnforceWorkGroupConfiguration": True,
                    "BytesScannedCutoffPerQuery": 1_000_000_000,
                    "ResultConfiguration": Match.object_like({"EncryptionConfiguration": {"EncryptionOption": "SSE_S3"}}),
                }
            )
        },
    )
    template.has_resource_properties(
        "AWS::Glue::Table",
        {"TableInput": Match.object_like({"Parameters": Match.object_like({"projection.enabled": "true"})})},
    )


def test_ingest_runs_daily_and_failures_email_the_alert_topic() -> None:
    template = _template()
    template.has_resource_properties("AWS::Events::Rule", {"ScheduleExpression": "cron(30 8 * * ? *)"})
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "ScheduleExpression": "cron(0 9 ? * SUN *)",
            "Targets": Match.array_with([Match.object_like({"Input": '{"digest":true}'})]),
        },
    )
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm", {"MetricName": "Errors", "Threshold": 1, "Namespace": "AWS/Lambda", "Period": 3600}
    )


def test_export_stack_is_in_us_east_1_and_delivers_parquet_to_the_main_bucket() -> None:
    app = cdk.App(context=CDK_CONTEXT)
    export = CurExportStack(
        app, "Export", bucket_region="us-east-2", env=cdk.Environment(account="123456789012", region="us-east-1")
    )
    template = Template.from_stack(export)
    template.has_resource_properties(
        "AWS::BCMDataExports::Export",
        {
            "Export": Match.object_like(
                {
                    "DataQuery": Match.object_like(
                        {
                            "TableConfigurations": {
                                "COST_AND_USAGE_REPORT": Match.object_like(
                                    {"INCLUDE_RESOURCES": "TRUE", "TIME_GRANULARITY": "DAILY"}
                                )
                            }
                        }
                    ),
                    "DestinationConfigurations": {
                        "S3Destination": Match.object_like(
                            {
                                "S3Bucket": {"Fn::Join": ["", ["costwatcher-cur-", {"Ref": "AWS::AccountId"}]]},
                                "S3Region": "us-east-2",
                                "S3OutputConfigurations": Match.object_like(
                                    {"Format": "PARQUET", "Overwrite": "OVERWRITE_REPORT"}
                                ),
                            }
                        )
                    },
                }
            )
        },
    )


def test_export_columns_match_the_table() -> None:
    assert QUERY.split(" FROM ")[0].removeprefix("SELECT ").split(", ") == [name for name, _ in COLUMNS]


def test_lambdas_stay_outside_a_vpc() -> None:
    functions = _template().find_resources("AWS::Lambda::Function")
    assert functions
    assert all("VpcConfig" not in f["Properties"] for f in functions.values())
    _template().resource_count_is("AWS::EC2::VPC", 0)
    _template().resource_count_is("AWS::EC2::NatGateway", 0)


def test_sign_in_page_is_dark() -> None:
    _template().has_resource_properties(
        "AWS::Cognito::ManagedLoginBranding",
        {
            "UseCognitoProvidedValues": False,
            "Settings": Match.object_like(
                {"categories": Match.object_like({"global": Match.object_like({"colorSchemeMode": "DARK"})})}
            ),
        },
    )


def test_resources_carry_the_project_tag_the_ingest_reads() -> None:
    expected = [{"Key": "project", "Value": "costwatcher"}]
    template = _template()
    template.has_resource_properties(  # a TableV2 keeps its tags on the replica
        "AWS::DynamoDB::GlobalTable",
        {"Replicas": Match.array_with([Match.object_like({"Tags": Match.array_with(expected)})])},
    )
    template.has_resource_properties("AWS::S3::Bucket", {"Tags": Match.array_with(expected)})
    template.has_resource_properties("AWS::Lambda::Function", {"Tags": Match.array_with(expected)})
    template.has_resource_properties("AWS::SNS::Topic", {"Tags": Match.array_with(expected)})
    template.has_resource_properties("AWS::CloudFront::Distribution", {"Tags": Match.array_with(expected)})
    template.has_resource_properties("AWS::Athena::WorkGroup", {"Tags": Match.array_with(expected)})
    template.has_resource_properties("AWS::Logs::LogGroup", {"Tags": Match.array_with(expected)})
    template.has_resource_properties("AWS::Cognito::UserPool", {"UserPoolTags": {"project": "costwatcher"}})


def test_project_tag_key_follows_the_context_the_ingest_uses() -> None:
    template = _template({"projectTag": "team"})
    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        {
            "Replicas": Match.array_with(
                [Match.object_like({"Tags": Match.array_with([{"Key": "team", "Value": "costwatcher"}])})]
            )
        },
    )


def test_export_is_tagged_too() -> None:
    app = cdk.App(context=CDK_CONTEXT)
    export = CurExportStack(
        app, "Export", bucket_region="us-east-2", env=cdk.Environment(account="123456789012", region="us-east-1")
    )
    Template.from_stack(export).has_resource_properties(
        "AWS::BCMDataExports::Export", {"Tags": Match.array_with([{"Key": "project", "Value": "costwatcher"}])}
    )
