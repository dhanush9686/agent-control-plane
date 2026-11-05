"""Print how much work is waiting for the hosted worker, so a scheduled run can stop early when there is none.

Uses only the standard library (no pip install needed) and the Redis REST URL and token from the environment.
Writes `pending=<n>` to $GITHUB_OUTPUT when set, and prints the number either way. Never prints the token.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

QUEUES = ("acp:live:queue", "acp:live:sim:queue")


def depth(url: str, token: str) -> int:
    body = json.dumps([["LLEN", q] for q in QUEUES]).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/pipeline", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )  # fmt: skip
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - https URL from the repository's own secret
        rows = json.loads(r.read().decode())
    return sum(int(r.get("result") or 0) for r in rows)


def main() -> int:
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url.startswith("https://") or not token:
        print("Redis secrets are not set on this repository.", file=sys.stderr)
        return 1
    try:
        n = depth(url, token)
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"Could not read the queue: {type(e).__name__}", file=sys.stderr)
        return 1
    print(f"pending={n}")
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write(f"pending={n}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
