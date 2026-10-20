#!/usr/bin/env python3
"""CDK entry point. `cdk synth` also runs cdk-nag's AWS Solutions security checks."""

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from infra.stack import CostWatcherStack

app = cdk.App()
CostWatcherStack(
    app,
    "CostWatcher",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region="us-east-2",  # fixed: the CDK CLI defaults to us-east-1 without credentials
    ),
    termination_protection=True,
    description="Dark-mode home control PWA: SmartThings, garage doors and LG ThinQ appliances",
)
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()
