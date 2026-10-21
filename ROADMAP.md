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
- **Alerts use the latest day, which may be partial.** Each is sent once per date with that day's figure at 08:30 UTC; later revisions aren't reported. A crash between writing the marker and publishing loses that alert, and one failed publish skips the rest of that run's alerts.
- **Alert markers are never deleted.** One tiny `ALERT#<month>` item per alert sent; negligible, but they accumulate.
- **The budget alert duplicates AWS Budgets.** Both warn when the month is forecast to pass the limit; CostWatcher's version also names the range.

- **Cost data is global, not per user.** Any signed-in user reads all of it; safe only while sign-up stays closed.
- **`GET /costs/{month}` is unbounded.** It returns every item under the month's key; paginate or summarize if per-resource items share it.
- **The dashboard is untested against real data.** It was built from the approved mockups and unit-tested on the data model only; no browser check ran with real billing data (none had arrived). Look at all three pages once the first ingest has run.
- **The Breakdown page differs from the mockup.** The treemap and "which resource drove each service" table need per-resource data the daily item doesn't hold, so Breakdown shows ranked bars by project, service and usage type. The mockup's "CostWatcher's own cost" card is replaced by an average-day card until that cost can be told apart.
- **The dashboard forecast trusts the newest month.** If ingest stalls across a month boundary, "Month to date" shows the last month with data under its own dates, and missing days within a month are averaged over the days present.
- **Home-screen app sign-in is untested on a phone.** The dashboard installs from Safari's Share > Add to Home Screen. Sign-in leaves the app for the Cognito page and returns; check that it comes back inside the app and that the passkey prompt works there. If it doesn't, a native app is the fallback.
- **Tokens live in localStorage.** Refresh tokens last 21 days (Cognito does not extend them with use), so you sign in again every three weeks; the CSP (`script-src 'self'`) is what protects them. Revisit if the CSP ever loosens.
- **Alerts vanish if `alertEmail` is omitted.** The budget and email subscription only exist when `-c alertEmail=...` is passed, so a local `cdk deploy` without it removes them. CI always passes it; always pass it locally too.
- **The API Lambda has read-only table access.** The ingest Lambda (phase 3) needs its own write grant; keep the two roles separate.
- **Local site development needs `-c devOrigin=true`.** localhost:5173 is not allowed on the deployed pool or API by default.
- **The deploy role is created by the stack.** The first deploy is a manual bootstrap and deploy (ask first), after which `AWS_DEPLOY_ROLE_ARN` and `DEPLOY_ENABLED` can be set.
- **Data Exports must be created in us-east-1,** while the rest of the stack is in us-east-2. The `CostWatcherExport` stack needs us-east-1 bootstrapped too (ask first), and the deploy role already trusts its bootstrap roles.
- **The CUR schema is unverified until the first export lands.** The Glue table columns (`infra/cur.py`) and the `BILLING_PERIOD=` folder layout follow the CUR 2.0 docs from memory; compare them with the first Parquet file. In particular check the timestamp type of `line_item_usage_start_date`.
- **Cross-region delivery is untested.** The export (us-east-1) writes to a us-east-2 bucket. The bucket policy must cover the whole bucket, as AWS's required policy does; a `cur/*`-only policy fails the create with "S3 bucket permission validation failed". If it fails again, check the `aws:SourceArn` patterns next. A failed first create leaves `CostWatcherExport` in ROLLBACK_COMPLETE, which must be deleted (turn off termination protection first) before redeploying.
- **Other stacks carry the `project` tag.** Added to the sibling repos on 2026-10-20; their spend is attributed once they redeploy and a day of billing lands. A resource their stacks don't tag still shows as "unattributed".
- **Project attribution applies going forward only.** The `project` cost allocation tag is activated (2026-10-20), but AWS tags only costs from activation on; earlier spend stays "unattributed".
- **The ingest never deletes a day.** If AWS drops a day from a revised month, its old item stays in DynamoDB.
- **The export is kept on stack delete.** The bucket has a retain policy because history can't be re-fetched; delete it by hand if the project is abandoned.
