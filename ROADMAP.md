# Roadmap

Ideas and bugs found while building CostWatcher. The phased build plan is in [docs/PLAN.md](docs/PLAN.md).

## Ideas

- **Dependency view.** A graph of which service used which (Lambda -> DynamoDB -> KMS) with cost on the edges. Deferred past v1.
- **Multiple accounts.** AWS Organizations-wide CUR with an account filter in the dashboard.
- **Cost Explorer backfill.** A one-off pull of up to 13 months of history (about a dozen calls, roughly $0.10), since the export has no data before it is created.
- **Per-project budgets.** Alert when one tagged project passes its own limit, not just the account.
- **Tag hygiene report.** List the top untagged resources by cost so the "unattributed" bucket shrinks.
- **Weekly email digest.** Top movers and the forecast, through SNS or SES.
- **Custom domain.** Optional later; the CloudFront URL is fine for personal use.

- **Unit costs.** Cost per 1,000 Lambda invocations or per GB stored needs usage quantities; add `line_item_usage_amount` to the export columns and the ingest query first.
- **Waste finder.** Idle NAT gateways, unattached EBS volumes, old snapshots and log groups without retention need a resource inventory (EC2, CloudWatch Logs APIs or AWS Config), which the export alone can't give.
- **Per-resource detail.** The treemap needs cost per resource, which the daily item doesn't hold yet; it would need its own items to stay under the 400 KB item limit.

## Bugs and risks

- **Alert thresholds are constants.** Spike (2x the 7-day average and over $0.50), service rise (1.5x and over $1) and the new-service rule live in `backend/ingest/analysis.py`; tune them after a few weeks of real data. Every rule stays silent until 7 days of history exist.
- **A "new service" alert fires once per service, ever.** Its marker item is never cleared, so a service that goes quiet and returns won't alert again.
- **The alert baseline is thin early in a month.** It uses the last 7 days read in that run (this month only after day 5), not 7 calendar days, so rules are quiet for about the first week of a month and gaps in the export stretch the window. Reading the stored previous month would fix it.
- **Alerts use the latest day, which may be partial.** Each is sent once per date with that day's figure at 08:30 UTC; later revisions aren't reported. A crash between writing the marker and publishing loses that alert, and one failed publish skips the rest of that run's alerts.
- **Alert markers are never deleted.** One tiny `ALERT#<month>` item per alert sent; negligible, but they accumulate.
- **The budget alert duplicates AWS Budgets.** Both warn when the month is forecast to pass the limit; CostWatcher's version also names the range.

- **Cost data is global, not per user.** Any signed-in user reads all of it; safe only while sign-up stays closed.
- **`GET /costs/{month}` is unbounded.** It returns every item under the month's key; paginate or summarize if per-resource items share it.
- **An empty month looks like zero cost.** The dashboard should show "no data yet", not $0, when `days` is empty.
- **SPA fallback hides missing files.** CloudFront turns every S3 403/404 into a 200 `index.html`, so a missing `config.json` or hashed asset returns HTML. Scope the fallback to extensionless paths with a CloudFront Function when the real site ships.
- **Alerts vanish if `alertEmail` is omitted.** The budget and email subscription only exist when `-c alertEmail=...` is passed, so a local `cdk deploy` without it removes them. CI always passes it; always pass it locally too.
- **The API Lambda has read-only table access.** The ingest Lambda (phase 3) needs its own write grant; keep the two roles separate.
- **Local site development needs `-c devOrigin=true`.** localhost:5173 is not allowed on the deployed pool or API by default.
- **CodeQL has no JavaScript/TypeScript yet.** Default setup scans `actions` and `python` only, because the repo has no JS/TS source until the site ships; a push run on main failed on 2026-10-20 while it still listed JS/TS. Add `javascript-typescript` back when the React site lands.
- **The deploy role is created by the stack.** The first deploy is a manual bootstrap and deploy (ask first), after which `AWS_DEPLOY_ROLE_ARN` and `DEPLOY_ENABLED` can be set.
- **Data Exports must be created in us-east-1,** while the rest of the stack is in us-east-2. The `CostWatcherExport` stack needs us-east-1 bootstrapped too (ask first), and the deploy role already trusts its bootstrap roles.
- **The CUR schema is unverified until the first export lands.** The Glue table columns (`infra/cur.py`) and the `BILLING_PERIOD=` folder layout follow the CUR 2.0 docs from memory; compare them with the first Parquet file. In particular check the timestamp type of `line_item_usage_start_date`.
- **Cross-region delivery is untested.** The export (us-east-1) writes to a us-east-2 bucket. The bucket policy must cover the whole bucket, as AWS's required policy does; a `cur/*`-only policy fails the create with "S3 bucket permission validation failed". If it fails again, check the `aws:SourceArn` patterns next. A failed first create leaves `CostWatcherExport` in ROLLBACK_COMPLETE, which must be deleted (turn off termination protection first) before redeploying.
- **Project attribution needs the tag activated.** Activate the `project` cost allocation tag in the Billing console (or pass `-c projectTag=<key>`); until then every cost is "unattributed". Tags apply going forward only.
- **The ingest never deletes a day.** If AWS drops a day from a revised month, its old item stays in DynamoDB.
- **The export is kept on stack delete.** The bucket has a retain policy because history can't be re-fetched; delete it by hand if the project is abandoned.
