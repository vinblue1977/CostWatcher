# CostWatcher

A dark-mode dashboard that breaks down your AWS spend by project, service and resource, in more depth than Cost Explorer. It reads the Cost and Usage Report (CUR 2.0) from S3 with Athena, so it makes no Cost Explorer API calls, and it runs serverless on AWS for well under $1 a month.

Status: planning and scaffolding. Nothing is deployed yet.

- [docs/PLAN.md](docs/PLAN.md): data flow, analysis, phases, risks
- [ROADMAP.md](ROADMAP.md): ideas and known bugs
- Mockups (dark mode only): to be linked here once approved

## Stack

Python 3.14 Lambdas, DynamoDB, Athena over a CUR 2.0 Parquet export in S3, an API Gateway HTTP API with a Cognito JWT authorizer, AWS CDK (Python) with cdk-nag, and a React + Vite site on S3 + CloudFront. CI, deploys (GitHub OIDC, no stored AWS keys) and dependency updates follow the sibling repos `Stocks` and `TennisTracker`.

## License

MIT
