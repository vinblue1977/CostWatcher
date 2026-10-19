# Working in this repo

## Before writing UI code

- Show mockups and get approval before committing any UI code. The approved set (dark mode only) is linked from the README.
- The UI is dark mode only: one dark palette, no light theme, no theme toggle.

## Safety

- Ask before the first `cdk bootstrap` or `cdk deploy`, and before any command that moves a garage door or lock for real.
- Never log or commit vendor tokens (SmartThings, LG). They live in SSM Parameter Store or the environment, never in the repo.
- Add ideas and bugs you find to `ROADMAP.md`.

## Pull requests

- Before starting new work, run `gh pr list`. If a PR is open, get it merged first (fix conflicts or failing checks, `gh pr update-branch <n>`, wait for the merge), then `git pull` main.
- Don't open a second PR while one is open, unless it's docs-only. If two are open, run `gh pr update-branch` on the other as soon as one merges, without waiting to be told.
- `git pull` main before branching. Use a conventional title (`feat:`, `fix:`, `ci:`, `docs:`).
- Run `make check` before pushing.
- After `gh pr create`, run `gh pr merge --auto --squash` yourself. There is no bot auto-merge: merges made with
  `GITHUB_TOKEN` don't start the push run, so the deploy would be skipped.
- When an earlier PR merges, run `gh pr update-branch <n>` on the PRs still open (branch protection needs them up to date).
- After a PR merges, delete its branches: `git fetch --prune`, then `git branch -D` every local branch marked `gone` (and any remote branch of a merged PR that GitHub left behind). Never delete a branch with an open PR.
- If a PR ever merges without a push run on main, deploy with `gh workflow run ci.yml --ref main`.
