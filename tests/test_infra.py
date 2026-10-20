"""Infrastructure tests: the stack synthesizes, passes cdk-nag, and its table, API and deploy role are locked down."""

import functools
import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template
from cdk_nag import AwsSolutionsChecks

from infra.stack import CostWatcherStack

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
    assert by_key == {"GET /costs/{month}": "JWT"}
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
    assert "cognito-idp.us-east-2.amazonaws.com" in csp and "style-src 'self';" in csp and "unsafe-inline" not in csp


def test_api_lambda_can_only_read_the_table() -> None:
    template = _template()
    api = next(
        f
        for f in template.find_resources("AWS::Lambda::Function").values()
        if f["Properties"]["Handler"] == "api.handler.handler"
    )
    assert "TABLE_NAME" in api["Properties"]["Environment"]["Variables"]
    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert "dynamodb:Query" in policies
    for write in ("PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem"):
        assert f"dynamodb:{write}" not in policies


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
