# CostWatcher: AWS cost analysis, deeper than Cost Explorer

## Context
One dark-mode dashboard that answers "what did I spend, on which service, and what used it?" for a single AWS account, in more depth than the console gives. The cost source is the Cost and Usage Report (CUR 2.0, via Data Exports), which has a line item per resource. **No Cost Explorer API calls** (they cost $0.01 each, and its resource-level data only covers 14 days). No custom domain (use the CloudFront URL). No customer-managed KMS key unless cdk-nag requires one. All Lambdas stay outside a VPC (no `vpc` config, no NAT gateway, no VPC endpoints).

## Stack
Mirrors the sibling repos `Stocks` and `TennisTracker`, so their CI, deploy and lockfile setup is reused: Python 3.14 (arm64 Lambdas), AWS CDK (Python) with cdk-nag, DynamoDB on-demand, API Gateway HTTP API with a Cognito JWT authorizer, and a TypeScript + React + Vite site on S3 + CloudFront. Hashed lockfiles, ruff, pyright, pytest. The UI is dark mode only.

## Data flow
```
Data Exports (CUR 2.0, Parquet, daily, resource IDs) --> S3 (us-east-1)
EventBridge (daily) --> Lambda --> Athena (partition projection, no crawler)
                                      |
                                      v
                           DynamoDB (daily aggregates)
                              |                 |
                              v                 v
                    SNS email alerts     API Gateway --> dashboard
```
- The export must be created in `us-east-1` (Data Exports lives there); the rest stays in `us-east-2`.
- The dashboard reads only DynamoDB, never Athena or AWS billing APIs directly.
- Delivery lag: the export updates a few times a day, and the first file can take up to 24 hours. There is no same-day figure.
- No history before the export is created (no Cost Explorer backfill).

## Analysis (beyond what AWS shows)
1. **Attribution:** project -> service -> resource, with untagged spend shown as "unattributed". Needs cost allocation tags activated in the Billing console (they apply going forward only).
2. **Drivers:** for a spike, show what changed, such as "+$3.10 from NAT Gateway data processing", by diffing usage types against the prior period.
3. **Unit costs:** cost per 1,000 Lambda invocations, per GB stored, per million requests.
4. **Forecast:** month-end projection with a range, which needs a few weeks of history to be useful. Until then use the linear projection (month to date / days elapsed x days in month).
5. **Waste finder:** idle NAT gateways, unattached EBS volumes, old snapshots, log groups with no retention.
6. **Alerts (SNS email):** daily total over 2x the trailing 7-day average, a service up by more than a threshold, month on track to pass budget, a service billed for the first time.
7. **Dependency view** (deferred past v1): which service used which (Lambda -> DynamoDB -> KMS), with cost on the edges.

## Dashboard (mockups first, dark mode only)
- Stacked area chart: daily cost by service, with a toggle to group by project or usage type.
- Treemap: project -> service -> resource ("which service used it").
- Sankey: project -> service -> usage type.
- Spike drill-down: pick a day, see the top drivers and their change.
- Forecast line against the budget, plus CostWatcher's own cost.

## Phases
1. **Docs and mockups** (this PR): plan, roadmap, README, then dark mockups for approval.
2. **Replace the scaffold:** remove the mock devices API, keep Cognito, hosting, alerts, budget and the deploy role. Add the DynamoDB aggregate table.
3. **Export and query:** S3 bucket, Data Exports definition, Athena workgroup and table, and the daily ingest Lambda. Ask before the first `cdk bootstrap`/`cdk deploy`.
4. **Analysis and alerts:** drivers, unit costs, forecast, waste rules, SNS.
5. **Dashboard:** built from the approved mockups.
6. **Hardening:** 14-day log retention, error states, own-cost line, and tests with Athena result fixtures.

## Cost (estimates, unverified, list prices as known)
About $0.10-0.50/month: S3 and Athena for one account's Parquet are cents, DynamoDB and API Gateway are cents, Lambda, EventBridge, SNS, Cognito and CloudFront are free tier, and CloudWatch Logs is the largest line (kept small with a 14-day retention). Possible extras: a customer-managed KMS key ($1/month) if cdk-nag requires it. The existing $5 monthly budget alarm guards the tool's own spend.

## Risks
- **Data Exports setup:** the CDK resource (`bcmdataexports.CfnExport`) and its exact table options (daily granularity, resource IDs, Parquet) are unverified; check them before writing the stack.
- **Delivery lag and no backfill:** early charts are thin; set that expectation in the UI.
- **Estimated data:** daily figures can be revised until the month closes, so overwrite rather than append.
- **Untagged resources:** attribution is only as good as the tags; the unattributed bucket must stay visible.
- **Athena cost creep:** keep Parquet, partition projection, and a bytes-scanned limit on the workgroup.

## Verification
- Unit tests for each analysis rule against small recorded Athena result fixtures.
- After the first approved deploy: confirm the first export file lands in S3, then run the ingest Lambda by hand and compare one day's total with the Billing console.
- Dashboard: compare the chart totals with the aggregates in DynamoDB.

## Status (2026-10-20) - resume here
- Renamed from HomeHub; PR #1 (scaffold, CI, rename) merged. Nothing is deployed. The deploy job is off until the repository variable `DEPLOY_ENABLED` is `true`.
- Decisions: single account, per-resource detail from CUR, no Cost Explorer calls, no backfill, no custom domain, dependency view deferred.
- Mockups approved 2026-10-20 (5 dark boards: overview, breakdown, spike drill-down, iPhone overview and breakdown). Sankey view not mocked up; add later if wanted.
- Phase 2 (replace the scaffold) merged (#5): devices API removed, `GET /costs/{month}` reads the table (PK `COST#YYYY-MM`, SK `DAY#date`) with read-only access.
- Phase 3 (export and query) is in PR `feat/cur-export-and-ingest`, not deployed. Layout decision: the bucket, Glue table, Athena workgroup and ingest Lambda are in the main us-east-2 stack; a small `CostWatcherExport` stack in us-east-1 holds only the Data Export, which delivers across regions to `costwatcher-cur-<account>`. The ingest Lambda runs daily at 08:30 UTC, re-reads the current month (and the previous one for the first 5 days), and overwrites one item per day: `total`, `services`, `projects`, `usageTypes`. Not yet stored: per-resource detail for the treemap (phase 4). Next: the first approved bootstrap and deploy (us-east-2 and us-east-1), then check the first export file and the table schema.
- Ideas and bugs go in `ROADMAP.md`.
