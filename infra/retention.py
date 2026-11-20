"""Gives every Lambda without an explicit log group a managed one that expires after 14 days.

Lambda creates `/aws/lambda/<name>` itself on first run, with no expiry, for any function that doesn't name a log group.
CDK's helper functions (the site BucketDeployment, S3 auto-delete) are such functions and can't be given a `log_group`
through their constructs, so this aspect points them at a log group of the stack's own.
"""

import jsii
from aws_cdk import CfnResource, IAspect, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import IConstruct

LAMBDA_FUNCTION = "AWS::Lambda::Function"


@jsii.implements(IAspect)
class HelperLogRetention:
    """Run on a stack: `Aspects.of(stack).add(HelperLogRetention())`."""

    def visit(self, node: IConstruct) -> None:
        if not isinstance(node, CfnResource) or node.cfn_resource_type != LAMBDA_FUNCTION or _names_a_log_group(node):
            return
        stack = Stack.of(node)
        # node.node.addr is a stable hash of the function's path, so the log group's logical ID doesn't move between synths.
        group_id = f"HelperLogs{node.node.addr}"
        if stack.node.try_find_child(group_id) is not None:
            return  # CDK re-runs aspects when they add constructs; this function is already done
        group = logs.LogGroup(stack, group_id, retention=logs.RetentionDays.TWO_WEEKS, removal_policy=RemovalPolicy.DESTROY)
        node.add_property_override("LoggingConfig.LogGroup", group.log_group_name)


def _names_a_log_group(function: CfnResource) -> bool:
    """True when the function already has a log group (an L2 `Function(log_group=...)`). Provider functions never do."""
    if not isinstance(function, lambda_.CfnFunction):
        return False
    config = function.logging_config
    if isinstance(config, lambda_.CfnFunction.LoggingConfigProperty):
        return config.log_group is not None  # only a log format set: still needs a group
    return config is not None  # a token we can't read: leave the author's setting alone
