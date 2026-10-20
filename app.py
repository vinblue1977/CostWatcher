#!/usr/bin/env python3
"""CDK entry point. `cdk synth` also runs cdk-nag's AWS Solutions security checks."""

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from infra.export_stack import CurExportStack
from infra.stack import CostWatcherStack

app = cdk.App()
main = CostWatcherStack(
    app,
    "CostWatcher",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region="us-east-2",  # fixed: the CDK CLI defaults to us-east-1 without credentials
    ),
    termination_protection=True,
    description="CostWatcher: AWS cost analysis from the Cost and Usage Report",
)
export = CurExportStack(
    app,
    "CostWatcherExport",
    bucket_region=main.region,
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-1"),  # Data Exports exists only here
    termination_protection=True,
    description="CostWatcher: the Cost and Usage Report export (Data Exports is us-east-1 only)",
)
export.add_stack_dependency(main)  # the bucket and its delivery policy must exist before the export
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()
