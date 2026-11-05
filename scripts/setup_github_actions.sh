#!/usr/bin/env bash
# One-time setup of the GitHub repository for the hosted worker and the weekly sync.
# Run it YOURSELF on your machine: it reads your local .env and sends each value straight to GitHub over `gh`, so no secret
# passes through a chat, a command line or a log. It never prints a value.
#
#   scripts/setup_github_actions.sh <AWS_ROLE_ARN> <AWS_REGION>
#   e.g. scripts/setup_github_actions.sh arn:aws:iam::123456789012:role/acp-github-bedrock-invoke ap-south-1
#
# Needs: the GitHub CLI logged in (`gh auth login`) with admin access to the repository, and the CloudFormation stack from
# infra/github-oidc-bedrock.yaml already deployed (that is where the role ARN comes from).
set -euo pipefail
cd "$(dirname "$0")/.."
ROLE_ARN="${1:?usage: $0 <AWS_ROLE_ARN> <AWS_REGION>}"
REGION="${2:?usage: $0 <AWS_ROLE_ARN> <AWS_REGION>}"
[ -f .env ] || { echo "No .env here; create it first."; exit 1; }
set -a; . ./.env; set +a

put_secret() {  # name, value
  if [ -z "${2:-}" ]; then echo "  skipped $1 (not set in .env)"; return; fi
  printf '%s' "$2" | gh secret set "$1" >/dev/null && echo "  secret   $1"  # no --body: gh then reads the value from stdin (--body - would store a literal dash)
}
echo "Secrets (values are sent directly to GitHub):"
put_secret UPSTASH_REDIS_REST_URL   "${UPSTASH_REDIS_REST_URL:-${KV_REST_API_URL:-}}"
put_secret UPSTASH_REDIS_REST_TOKEN "${UPSTASH_REDIS_REST_TOKEN:-${KV_REST_API_TOKEN:-}}"
put_secret RAZORPAY_KEY_ID          "${RAZORPAY_KEY_ID:-}"
put_secret RAZORPAY_KEY_SECRET      "${RAZORPAY_KEY_SECRET:-}"
put_secret ALERT_WEBHOOK_URL        "${ALERT_WEBHOOK_URL:-}"   # optional: Slack-style alerts
echo "Variables (not secret):"
gh variable set AWS_ROLE_ARN --body "$ROLE_ARN" >/dev/null && echo "  variable AWS_ROLE_ARN"
gh variable set AWS_REGION   --body "$REGION"   >/dev/null && echo "  variable AWS_REGION"
[ -z "${DASHBOARD_URL:-}" ] || { gh variable set DASHBOARD_URL --body "$DASHBOARD_URL" >/dev/null && echo "  variable DASHBOARD_URL"; }
echo
echo "Done. Next: deploy infra/github-oidc-bedrock.yaml if you have not, then run the 'live-worker' workflow once by hand."
echo "Optional (instant worker start): add GITHUB_DISPATCH_TOKEN and GITHUB_REPO to Vercel; see docs/HOSTED_WORKER.md."
