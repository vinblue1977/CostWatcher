# HomeHub: PWA + AWS serverless backend

## Context
User wants one app to control SmartThings, Aladdin Connect (garage) and LG ThinQ appliances. Xfinity Home stays separate (no public API). Backend on AWS. User does not want to pay the $99 Apple Developer fee, so the client is an installable PWA (Add to Home Screen on iPhone) instead of a native app. No custom domain (use the CloudFront URL). No customer-managed KMS key and no Secrets Manager.

Why a backend: vendor OAuth secrets/refresh tokens must not live in the browser; backend also normalizes three vendor APIs into one device model and sends push alerts.

## Stack
Decision (2026-10-19): Python for infra and backend, TypeScript for the frontend, mirroring the sibling repos `Stocks` and `TennisTracker` so their proven CI, deploy and lockfile setup is reused. The UI is dark mode only; approved mockups are linked from the README.

**Frontend (`frontend/`)**: TypeScript, React + Vite, `vite-plugin-pwa` (Workbox) or a hand-written service worker as in TennisTracker, TanStack Query, React Router, Cognito sign-in. Web Push (VAPID) via service worker; works on iOS 16.4+ only when installed to the home screen. Dark theme only.

**Backend (`backend/`)**: Lambda (Python 3.14, arm64), API Gateway HTTP API with Cognito JWT authorizer, DynamoDB on-demand, SSM Parameter Store SecureString for vendor client secrets and VAPID keys (AWS-managed key, no custom KMS), EventBridge for polling/alerts, `pywebpush`-style Web Push (SNS cannot send Web Push).

**Infra (`infra/`)**: AWS CDK (Python) with cdk-nag. Static site on S3 + CloudFront (default `*.cloudfront.net` domain, HTTPS included). **All Lambdas stay outside a VPC** (hard rule: no `vpc` config in CDK, no NAT gateway, no VPC endpoints). They reach DynamoDB, SSM and vendor APIs over the public AWS/internet endpoints.

**Device model**: defined once in the backend (`backend/api/devices.py`) and mirrored in `frontend/src/types.ts`, kept in sync by a contract test. Hashed lockfiles, ruff, pyright, pytest, Vitest, ESLint.

## Layout
```
HomeHub/
  infra/     CDK stack (Cognito, API GW, Lambdas, DynamoDB, SSM params, S3/CloudFront, EventBridge)
  backend/   handlers + adapters (smartthings, lgthinq)
  frontend/  React + Vite PWA (dark mode only)
  docs/      plan, vendor API notes
  scripts/   helper scripts (connectivity check)
  tests/     pytest (infra + backend)
```

## API
- `GET /devices`, `GET /devices/{id}`, `POST /devices/{id}/commands`
- `GET /integrations`, `GET /integrations/{vendor}/connect` (start OAuth), `GET /oauth/{vendor}/callback`
- `POST /push/subscribe`

## Adapter layer
One interface per vendor: `listDevices()`, `getState()`, `sendCommand()`, mapped to a common model (`id, vendor, name, type, room, state, capabilities`).
- SmartThings: official REST API, OAuth2 (OAuth-In app).
- Aladdin Connect: door status + open/close (highest risk, see below).
- LG ThinQ: official ThinQ Connect API (personal access token, region specific).

## Phases
1. **Verify vendor access first (blocker check): DONE 2026-10-17** via `connectivity/check.mjs` (read-only).
   - SmartThings: PAT works, 9 devices (lights/plugs, sensors, Schlage lock, hub, tv). Note: new PATs last 24h, so the app needs the OAuth-In flow.
   - Aladdin Connect: no separate API needed. Both garage doors ("Shria door", "Krish door") appear in SmartThings as "Aladdin Connect (Test)" devices with `doorControl`, `refresh`, `healthCheck`; both online.
   - LG ThinQ: PAT works (US region `aic`), 2 devices: Kitchen Refrigerator, Dishwasher. Requires accepting LG service terms in the ThinQ app before the PAT site works.
   - Still untested: sending `doorControl` open/close (do it with the user at the door).
2. **Skeleton**: pnpm monorepo, CDK stack (Cognito, API GW, DynamoDB, S3/CloudFront), `/devices` with mock data, deploy to dev.
3. **PWA shell**: login, dashboard, installable manifest + service worker, deployed to CloudFront.
4. **SmartThings adapter**: OAuth connect flow, list + control devices, including garage doors via `doorControl` (open/close with confirm prompt in UI).
5. ~~Aladdin adapter~~ dropped: covered by the SmartThings adapter. Fallback if the "(Test)" integration breaks: ratgdo local controller.
6. **LG ThinQ adapter**: appliance status/controls.
7. **Push alerts**: VAPID, subscribe endpoint, EventBridge check for "garage open too long".
8. Hardening: token refresh, rate limiting, error states, log hygiene (never log tokens).
9. **Siri (deferred, decided later)**: PWAs can't register Siri intents, so use iOS Shortcuts ("Get Contents of URL") calling the API. Needs per-action revocable tokens (hashed in DynamoDB) and a safer rule for garage open (off by default or confirm). Mock up the "Siri & Shortcuts" screen first.

## Cost (estimates, unverified)
~$0-2/month: Lambda, Cognito, DynamoDB, SSM standard, EventBridge, CloudFront/S3 are free-tier or pennies; API Gateway ~$0-1. No KMS key, no Secrets Manager, no domain, no Apple fee. Set an AWS Budget alert at $5-10 to catch runaway polling.

## Risks
- Garage doors depend on SmartThings' "Aladdin Connect (Test)" integration, which could change or stop; monitor, with ratgdo as fallback.
- LG ThinQ uses a long-lived PAT stored in SSM; scopes limited to what the app needs.
- iOS web push only works for home-screen-installed PWAs; service worker cache staleness after deploys (use auto-update).
- Garage control is physical access: require auth + confirmation.
- Without a custom domain the URL is a random cloudfront.net name (fine for personal use).

## Verification
- Backend: unit tests per adapter with recorded fixtures; `cdk deploy`, then `curl` with a Cognito token against `/devices`.
- OAuth: link SmartThings from the PWA, confirm devices appear, toggle a light and verify physically.
- PWA: Lighthouse PWA audit; install on iPhone via Safari; verify push arrives; test garage open/close on the real door.

## Status (2026-10-17) - resume here
- Phase 1 complete (see Phases). `connectivity/check.mjs` is a read-only device lister for SmartThings (`SMARTTHINGS_TOKEN`) and LG ThinQ (`LG_PAT`).
- Default region us-east-2. Nothing deployed yet.
- Revoke the SmartThings and LG tokens used during testing (they appeared in chat).
- Mockups approved 2026-10-19 (5 dark screens: dashboard, garage confirm, device detail, accounts, alerts).
- Next: (1) phase 2 scaffold PR (CDK stack, mock `/devices`, CI), (2) ask before first `cdk bootstrap`/`deploy`, (3) PWA shell PR from the approved mockups, (4) test garage `doorControl` open/close with the user at the door.
- Ideas and bugs found along the way go in `ROADMAP.md`.
