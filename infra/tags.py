"""The project cost allocation tag every stack puts on its resources."""

from aws_cdk import Tags
from constructs import Construct

PROJECT_NAME = "costwatcher"


def project_tag_key(scope: Construct) -> str:
    """`project` unless the app is run with -c projectTag=<key>; the ingest Lambda reads the same key from the CUR."""
    return scope.node.try_get_context("projectTag") or "project"


def tag_project(scope: Construct) -> None:
    """Tags everything under `scope`, so this app's own spend lands under its own project in the dashboard."""
    Tags.of(scope).add(project_tag_key(scope), PROJECT_NAME)
