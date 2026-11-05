# Hosted worker, weekly sync and CI gate

What runs where, and the one-time setup. Nothing here needs your laptop to stay on.

| Workflow | Runs when | Does |
|---|---|---|
| `live-worker.yml` | a webhook event or simulator job is queued (a ping from the website), hourly as a backstop, or by hand | Handles whatever is queued with the guarded agent. Exits in seconds when the queue is empty. |
| `live-sync.yml` | Mondays 03:00 UTC, or by hand | Full live sync on the Razorpay **test** account plus the sample ledger, published to Redis, then **fails the run if the guardrails regressed**. |
| `ci.yml` | every push and pull request | Ruff, the offline test suite (which includes the Node auth, webhook, account and simulator checks), and the dashboard typecheck and build. No cloud access. |

## How AWS access works (no keys)

GitHub gives each run a short-lived OIDC token. AWS exchanges it for temporary credentials for one role,
`acp-github-bedrock-invoke`. That role can only call `bedrock:InvokeModel` on one model (Nova Lite and its APAC inference
profile), and only runs on this repository's `main` branch can assume it. There are no `AWS_ACCESS_KEY_ID` values anywhere;
a test fails if one appears in a workflow.

## One-time setup (you do these; they touch your AWS and GitHub accounts)

1. **Create the role.** Deploy [infra/github-oidc-bedrock.yaml](../infra/github-oidc-bedrock.yaml) in the AWS region you use
   for Bedrock. If your account already has the GitHub OIDC provider, pass `CreateOidcProvider=false` and its ARN.
   ```bash
   aws cloudformation deploy --stack-name acp-github-oidc --template-file infra/github-oidc-bedrock.yaml \
     --capabilities CAPABILITY_NAMED_IAM --region ap-south-1
   aws cloudformation describe-stacks --stack-name acp-github-oidc --region ap-south-1 \
     --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text
   ```
2. **Give the repository its settings.** This reads your local `.env` and sends values straight to GitHub (nothing is printed):
   ```bash
   scripts/setup_github_actions.sh arn:aws:iam::<account>:role/acp-github-bedrock-invoke ap-south-1
   ```
   It sets the secrets `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and the
   variables `AWS_ROLE_ARN`, `AWS_REGION`. The Razorpay keys must be **test** keys; the adapter refuses anything else.
3. **Try it.** In the Actions tab run `live-worker` by hand, then `live-sync`.
4. **Optional, for an instant start.** Without this the worker starts within the hour. To start it the moment an event or
   simulator job is queued, add two environment variables on Vercel: `GITHUB_REPO` (`owner/repo`) and `GITHUB_DISPATCH_TOKEN`
   (a fine-grained personal access token limited to this one repository with the **Contents: read and write** permission,
   which is what GitHub requires for `repository_dispatch`). That token can write to the repository's contents, so keep it
   to this repo, give it an expiry, and treat it as a secret. The website pings at most once a minute and ignores failures.

5. **Optional, for alerts.** Add a Slack incoming webhook (or any https endpoint that accepts `{"text": "..."}`) as the repository secret
   `ALERT_WEBHOOK_URL`, and the dashboard address as the repository variable `DASHBOARD_URL` so alerts link to the Approvals page.
   If your `.env` has either value, `scripts/setup_github_actions.sh` sends it for you. The worker alerts when a drafted refund is waiting
   for approval and when the guardrails block an attempt. Limits: 20 alerts an hour, https only, masked, ids and amounts only. Email is not built in.

## The CI gate

`scripts/check_live_gate.py` applies `evals/assure/thresholds.json` to every source in a live sync: **no guarded critical
outcome, a guarded pass rate of at least 70%, no errored runs**, and it also fails a sync that stopped early on its spend
cap. A failing weekly run shows red in Actions and GitHub emails the repository's watchers. A cheap offline check also runs
in CI on every push: the committed benchmark must still satisfy the same thresholds
(`tests/test_workflows.py`).

## Costs and limits, stated plainly

- **Bedrock:** the worker and sync cost fractions of a cent per item; both stop at a daily or per-run spend cap ($0.25).
- **GitHub Actions minutes:** the hourly backstop reads the queue with the standard library and stops (about 10 seconds a
  run when idle). A private repository gets a monthly allowance; check yours.
- **Latency:** a pinged run starts in roughly 30 to 90 seconds; without the ping, up to an hour. The pages say "waiting for
  the worker" in the meantime.
- **The audit chain** is a SQLite file. Runners start empty, so it is restored from and saved to the Actions cache; if the
  cache is evicted (after 7 idle days) the chain starts again. A production system would keep the head outside the runner.
- **Branch protection is not configured by this repository.** The OIDC role trusts only `main`, but who can push to `main`
  is a GitHub setting you own.
