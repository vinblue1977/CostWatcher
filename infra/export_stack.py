"""The Data Export definition. Billing Data Exports only exists in us-east-1, so it gets a stack of its own."""

from aws_cdk import Stack
from aws_cdk import aws_bcmdataexports as bcm
from constructs import Construct

from infra.cur import EXPORT_NAME, EXPORT_PREFIX, QUERY, bucket_name
from infra.tags import tag_project


class CurExportStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, bucket_region: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        tag_project(self)
        bcm.CfnExport(
            self,
            "Export",
            export=bcm.CfnExport.ExportProperty(
                name=EXPORT_NAME,
                description="CUR 2.0 for CostWatcher: daily, with resource IDs, as Parquet",
                data_query=bcm.CfnExport.DataQueryProperty(
                    query_statement=QUERY,
                    table_configurations={
                        "COST_AND_USAGE_REPORT": {
                            "TIME_GRANULARITY": "DAILY",
                            "INCLUDE_RESOURCES": "TRUE",
                            "INCLUDE_MANUAL_DISCOUNT_COMPATIBILITY": "FALSE",
                            "INCLUDE_SPLIT_COST_ALLOCATION_DATA": "FALSE",
                        }
                    },
                ),
                destination_configurations=bcm.CfnExport.DestinationConfigurationsProperty(
                    s3_destination=bcm.CfnExport.S3DestinationProperty(
                        s3_bucket=bucket_name(),
                        s3_prefix=EXPORT_PREFIX,
                        s3_region=bucket_region,
                        s3_output_configurations=bcm.CfnExport.S3OutputConfigurationsProperty(
                            compression="PARQUET", format="PARQUET", output_type="CUSTOM", overwrite="OVERWRITE_REPORT"
                        ),
                    )
                ),
                refresh_cadence=bcm.CfnExport.RefreshCadenceProperty(frequency="SYNCHRONOUS"),
            ),
        )
