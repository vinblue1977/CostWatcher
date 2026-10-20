# CostWatcher

One dark-mode app for the house: SmartThings devices (lights, plugs, the Schlage lock and both garage doors) and LG ThinQ appliances, on serverless AWS. It installs on an iPhone as a PWA (Add to Home Screen), so no Apple Developer account is needed. Xfinity Home stays in its own app (no public API).

Status: planning and scaffolding. Nothing is deployed yet.

- [docs/PLAN.md](docs/PLAN.md): stack, phases, risks
- [ROADMAP.md](ROADMAP.md): ideas and known bugs
- Approved mockups (dark mode only, private link, ask the owner): https://claude.ai/artifact/Sqs8cPTkKaEtfVAPdhjAGY

## Stack

Python 3.14 Lambdas behind an API Gateway HTTP API with a Cognito JWT authorizer, DynamoDB, AWS CDK (Python) with cdk-nag, and a React + Vite PWA on S3 + CloudFront. CI, deploys (GitHub OIDC, no stored AWS keys) and dependency updates follow the sibling repos `Stocks` and `TennisTracker`.

## License

MIT
