# Roadmap

Ideas and bugs found while building CostWatcher. The phased build plan is in [docs/PLAN.md](docs/PLAN.md).

## Ideas

- **Dependency view.** A graph of which service used which (Lambda -> DynamoDB -> KMS) with cost on the edges. Deferred past v1.
- **Multiple accounts.** AWS Organizations-wide CUR with an account filter in the dashboard.
- **Cost Explorer backfill.** A one-off pull of up to 13 months of history (about a dozen calls, roughly $0.10), since the export has no data before it is created.
- **Per-project budgets.** Alert when one tagged project passes its own limit, not just the account.
- **Custom domain.** Optional later; the CloudFront URL is fine for personal use.

- **Unit costs.** Cost per 1,000 Lambda invocations or per GB stored needs usage quantities; add `line_item_usage_amount` to the export columns and the ingest query first.
- **Waste finder.** Idle NAT gateways, unattached EBS volumes, old snapshots and log groups without retention need a resource inventory (EC2, CloudWatch Logs APIs or AWS Config), which the export alone can't give.
- **Per-resource detail.** The treemap needs cost per resource, which the daily item doesn't hold yet; it would need its own items to stay under the 400 KB item limit.
- **Anomaly feedback.** A "not a spike" button on an alert that stores the dismissal and tunes thresholds per service.
- **Savings tracker.** Record a service's monthly cost before and after a change (say, removing a NAT gateway) and show what it saved.
- **Month-over-month compare.** The same service across two months side by side, biggest movers first.
- **Cost of CostWatcher itself.** The card the Breakdown mockup wanted; needs the stack's spend to be separable by the `project` tag.
- **Pace-to-budget line.** A daily chart line for the budget pace, so a projected overrun shows before the forecast alert fires.
- **Tag hygiene report.** Services with untagged spend, ranked by dollars.
- **Push or Slack alerts.** SNS to a phone push or webhook, not just email.
- **CSV export.** A download button on the Breakdown and Spikes pages.
- **Data freshness badge.** When the last ingest ran and whether the latest day is partial.
- **Month-to-date from Cost Explorer.** The export refreshes up to three times a day at times AWS picks, so the dashboard can trail the billing console (3.06 against 3.14 on 2026-10-26). One `ce:GetCostAndUsage` call a day for the month-to-date total only would close the gap for about $0.30 a month; one per 3-hour run costs about $2.40 a month, more than the rest of the pipeline. Keep the export for breakdowns (Cost Explorer lacks line-item detail), and note that the headline would then differ from the sum of the services between refreshes. A cheaper first step is labelling the dashboard with the export's delivery time (see the freshness badge).

## Bugs and risks

- **Alert thresholds are constants.** Spike (2x the 7-day average and over $0.50), service rise (1.5x and over $1) and the new-service rule live in `backend/ingest/analysis.py`; tune them after a few weeks of real data. Every rule stays silent until 7 days of history exist.
- **A "new service" alert fires once per service, ever.** Its marker item is never cleared, so a service that goes quiet and returns won't alert again.
- **Alerts use the latest day, which may be partial.** Each is sent once per date with that day's figure at 08:30 UTC; later revisions aren't reported. A crash between writing the marker and publishing loses that alert, and one failed publish skips the rest of that run's alerts.
- **Alert baselines count days, not calendar days.** The 7-day window is the last 7 entries in hand, so gaps in the export (or in last month's stored days) stretch it, and a quiet service can look "new" again.
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
- **Free tier numbers come from AWS, unchecked.** The Free tier page and its alerts use the Free Tier API's usage, limit and forecast as given. Its forecast swings early in a month, so a "on pace to pass" email waits until a quarter of the allowance is used; tune `FREE_TIER_PACE_MIN` after a few months.
- **Free tier alerts fire once per allowance per month and kind** (on pace, then over). The page shows the first of several allowances in its banner only; the rest are in the list below it.
- **The Free tier page is untested with real data in a browser.** It was built from the approved mockup and unit-tested on the model only. The mockup's "cost past the free tier" card is not built: the API reports usage, not dollars.
- **After the `product` export column deploys (#44): validate and clean up.** Opaque marketplace codes not in `PRODUCT_NAMES` take `product['product_name']`, else `product['model']`, from the export. Neither key has been seen in this account's data, and months exported before the column existed keep raw codes.
  - [ ] Once the export re-delivers, run `SELECT DISTINCT map_keys(product) FROM costwatcher.cur` in the `costwatcher` workgroup, and check what name `7zgyu5r4uonlsrnaq20qq63eo` (Sonnet 4.6) gets. If it is still a raw code, fix the key in `_service_sql`.
  - [ ] Check that the `^[a-z0-9]{25}$` pattern catches every marketplace code on the bill and no native AWS code, and that two models don't merge into one name.
  - [ ] Expect at most one "billed for the first time" email per newly named model. Confirm that is all that arrives.
  - [ ] Look at the Bedrock names in the dashboard and the alert emails (commas and parentheses in free-form names).
  - [ ] Check the Athena bytes scanned per run. The whole `product` map is exported: narrow the export to the one key if Data Exports allows it.
  - [ ] Delete the temporary `costwatcher-probe` export (`aws bcm-data-exports delete-export`, us-east-1) and the `probe/` prefix in `costwatcher-cur-<account>`.
  - [ ] Later, remove `PRODUCT_NAMES` once the days stored under the old names have aged out.

## Found while moving the ingest to every 3 hours (#42)

- **Alerts can fire on a half-finished day.** Each alert is sent once (marker item), and the ingest now runs 8 times a day, so a spike or budget alert could fire on figures a later export refresh revises down. Consider alerting only on days before today, or holding the newest day back.
- **Skip unchanged exports.** Most runs re-query Athena and rewrite identical items; checking the export's newest object time first would skip them. Cost is well under a cent a month, so this is low priority.
- **Rename the `Daily` rule ID** in `infra/cur.py` (it replaces the rule on deploy, so do it with another infra change).
