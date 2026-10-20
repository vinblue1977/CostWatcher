# Roadmap

Ideas and bugs found while building CostWatcher. The phased build plan is in [docs/PLAN.md](docs/PLAN.md).

## Ideas

- **Siri via Shortcuts** (deferred by choice). A PWA can't register Siri intents, so iOS Shortcuts call the API. Needs per-action, revocable tokens (hashed in DynamoDB) and a safer rule for garage open (off by default, or confirm). Mock up the "Siri & Shortcuts" screen first.
- **Close-the-garage reminder.** Push "Krish door is still open" when the phone leaves home (needs a location signal; Shortcuts automation could post to the API).
- **Activity log.** Who opened or closed which door and when (the confirm sheet already says the action is logged), with a screen to read it.
- **Household members.** A second Cognito user with a role (view only, or can close but not open the garage).
- **Offline banner.** The service worker shows the last known state with a "stale" label instead of a blank screen.
- **Scenes.** One tap: "Goodnight" closes the doors, checks the lock and turns lights off.
- **Local fallback for garage doors.** If the SmartThings "Aladdin Connect (Test)" integration disappears, a ratgdo controller gives local control (see the risk in the plan).
- **Custom domain.** Optional later; the CloudFront URL is fine for personal use.

## Bugs and risks

- **SPA fallback hides missing files.** CloudFront turns every S3 403/404 into a 200 `index.html`, so a missing `config.json` or hashed asset returns HTML. Scope the fallback to extensionless paths with a CloudFront Function when the real PWA ships.
- **Alerts vanish if `alertEmail` is omitted.** The budget and email subscription only exist when `-c alertEmail=...` is passed, so a local `cdk deploy` without it removes them. CI always passes it; always pass it locally too (or move it into context once private config is decided).
- **The API Lambda has no table access.** It was given none on purpose until the adapters store tokens; add the narrowest grant then (and the cdk-nag index suppression).
- **Garage open is gated by `confirm=true` in the API.** The real adapter must keep that rule, and `/devices/{id}/commands` should log who sent it once the user id is passed to handlers.
- **Local PWA development needs `-c devOrigin=true`.** localhost:5173 is not allowed on the deployed pool or API by default.
- **SmartThings personal tokens last 24 hours.** The app must use the OAuth-In flow; do not rely on a stored personal token.
- **Garage doors depend on a "(Test)" integration.** Monitor it; the app should show "state unknown" instead of a stale "Closed" when a door stops reporting.
- **Open and close are untested.** `doorControl` open/close has not been tried; do it with someone at the door before trusting it.
- **Tokens appeared in chat during phase 1.** Revoke the SmartThings and LG tokens used for testing.
- **LG ThinQ needs the LG service terms accepted** in the ThinQ app before a personal access token works.
- **iOS web push only works for a Home Screen install**, and only on iOS 16.4 or later. The Alerts screen should say so when the app runs in plain Safari.
