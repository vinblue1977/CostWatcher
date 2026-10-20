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

## Bugs and risks

- **Cost data is global, not per user.** Any signed-in user reads all of it; safe only while sign-up stays closed.
- **`GET /costs/{month}` is unbounded.** It returns every item under the month's key; paginate or summarize if per-resource items share it.
- **An empty month looks like zero cost.** The dashboard should show "no data yet", not $0, when `days` is empty.
- **SPA fallback hides missing files.** CloudFront turns every S3 403/404 into a 200 `index.html`, so a missing `config.json` or hashed asset returns HTML. Scope the fallback to extensionless paths with a CloudFront Function when the real site ships.
- **Alerts vanish if `alertEmail` is omitted.** The budget and email subscription only exist when `-c alertEmail=...` is passed, so a local `cdk deploy` without it removes them. CI always passes it; always pass it locally too.
- **The API Lambda has read-only table access.** The ingest Lambda (phase 3) needs its own write grant; keep the two roles separate.
- **Local site development needs `-c devOrigin=true`.** localhost:5173 is not allowed on the deployed pool or API by default.
- **CodeQL has no JavaScript/TypeScript yet.** Default setup scans `actions` and `python` only, because the repo has no JS/TS source until the site ships; a push run on main failed on 2026-10-20 while it still listed JS/TS. Add `javascript-typescript` back when the React site lands.
- **The deploy role is created by the stack.** The first deploy is a manual bootstrap and deploy (ask first), after which `AWS_DEPLOY_ROLE_ARN` and `DEPLOY_ENABLED` can be set.
- **Data Exports must be created in us-east-1,** while the rest of the stack is in us-east-2.
