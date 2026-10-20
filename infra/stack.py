"""The CostWatcher stack: daily cost table, sign-in, API, hosting, alerts, budget and the GitHub deploy role."""

import re

from aws_cdk import CfnOutput, DefaultStackSynthesizer, Duration, RemovalPolicy, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from cdk_nag import NagPackSuppression, NagSuppressions, RegexAppliesTo
from constructs import Construct

from infra.cur import Cur
from infra.web import Web

GITHUB = "token.actions.githubusercontent.com"


class CostWatcherStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Daily cost aggregates: PK COST#<YYYY-MM>, SK DAY#<date>. Filled once a day by the ingest Lambda.
        self.table = dynamodb.TableV2(
            self,
            "Table",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        CfnOutput(self, "TableName", value=self.table.table_name)

        topic = self._alert_topic()
        site_dir = self.node.try_get_context("siteDir") or "site"
        Web(self, "Web", site_dir=site_dir, topic=topic, table=self.table)  # pyright: ignore[reportArgumentType]
        project_tag: str = self.node.try_get_context("projectTag") or "project"
        Cur(self, "Cur", table=self.table, topic=topic, project_tag=project_tag)  # pyright: ignore[reportArgumentType]
        self._budget()

        github_repo: str = self.node.try_get_context("githubRepo") or ""
        if github_repo:
            self._github_deploy_role(github_repo)

    def _budget(self) -> None:
        """Emails the alert address at 80% of the monthly limit (actual) and 100% (forecast). Two budgets are free."""
        alert_email: str = self.node.try_get_context("alertEmail") or ""
        if not alert_email:
            return
        limit = float(self.node.try_get_context("monthlyBudgetUsd") or 5)

        def notify(threshold: int, kind: str) -> budgets.CfnBudget.NotificationWithSubscribersProperty:
            return budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    comparison_operator="GREATER_THAN", notification_type=kind, threshold=threshold, threshold_type="PERCENTAGE"
                ),
                subscribers=[budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=alert_email)],
            )

        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=limit, unit="USD"),
            ),
            notifications_with_subscribers=[notify(80, "ACTUAL"), notify(100, "FORECASTED")],
        )

    def _alert_topic(self) -> sns.Topic:
        topic = sns.Topic(self, "AlertTopic", enforce_ssl=True)
        alert_email: str = self.node.try_get_context("alertEmail") or ""
        if alert_email:
            topic.add_subscription(subs.EmailSubscription(alert_email))  # pyright: ignore[reportArgumentType]
        NagSuppressions.add_resource_suppressions(
            topic,
            [NagPackSuppression(id="AwsSolutions-SNS2", reason="Alerts hold no secrets; a customer-managed key costs $1/month")],
        )
        return topic

    def _github_deploy_role(self, github_repo: str) -> None:
        """Role for GitHub Actions in this repo's production environment. Reuses the account's OIDC provider."""
        if not re.fullmatch(r"[\w.-]+(@\d+)?/[\w.-]+(@\d+)?", github_repo):
            raise ValueError("githubRepo must be owner/repo or owner@id/repo@id")
        synthesizer = self.synthesizer
        qualifier = (
            synthesizer.bootstrap_qualifier if isinstance(synthesizer, DefaultStackSynthesizer) else None
        ) or DefaultStackSynthesizer.DEFAULT_QUALIFIER
        provider_arn = f"arn:{self.partition}:iam::{self.account}:oidc-provider/{GITHUB}"
        role = iam.Role(
            self,
            "GitHubDeployRole",
            description=f"GitHub Actions deploys from {github_repo} (production environment only)",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(  # pyright: ignore[reportArgumentType]
                provider_arn,
                conditions={
                    "StringEquals": {
                        f"{GITHUB}:aud": "sts.amazonaws.com",
                        f"{GITHUB}:sub": f"repo:{github_repo}:environment:production",
                    }
                },
            ),
        )
        # Deploys go through the CDK bootstrap roles.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                # The export stack deploys to us-east-1, so its bootstrap roles are needed too.
                resources=[
                    f"arn:{self.partition}:iam::{self.account}:role/cdk-{qualifier}-*-{self.account}-{region}"
                    for region in (self.region, "us-east-1")
                ],
            )
        )
        self.table.grant(role, "dynamodb:DescribeTable")  # smoke test
        NagSuppressions.add_resource_suppressions(
            role,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Only sts:AssumeRole on this account's CDK bootstrap roles",
                    # Partition and account render as literals or tokens depending on flags and credentials.
                    applies_to=[RegexAppliesTo(regex=rf"/^Resource::arn:.+:iam::.+:role\/cdk-{qualifier}-\*-.+$/g")],
                )
            ],
            apply_to_children=True,
        )
        CfnOutput(self, "GitHubDeployRoleArn", value=role.role_arn)
