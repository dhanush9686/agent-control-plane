"""Manage dashboard accounts (stored in Redis; the website only reads them).

  make dashboard-user ARGS="add you@example.com --name 'Your Name' --role admin"
  make dashboard-user ARGS="list"
  make dashboard-user ARGS="passwd you@example.com"
  make dashboard-user ARGS="disable you@example.com"      (or: enable)

Passwords are typed at a hidden prompt (or read from ACP_USER_PASSWORD for scripting). They are never printed or put
on the command line. Needs the Redis WRITE token in your environment or .env, like `make live-sync`.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from acp.assure.publish import UpstashError, client_from_env  # noqa: E402
from acp.dashboard_users import ROLES, UserError, add_user, list_users, set_disabled, set_password  # noqa: E402


def ask_password() -> str:
    given = os.environ.get("ACP_USER_PASSWORD")
    if given:
        return given
    a = getpass.getpass("Password: ")
    if a != getpass.getpass("Repeat password: "):
        raise UserError("The two passwords differ.")
    return a


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dashboard_user")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("email")
    a.add_argument("--name", default="")
    a.add_argument("--role", choices=ROLES, default="viewer")
    for c in ("passwd", "disable", "enable"):
        sub.add_parser(c).add_argument("email")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    client = client_from_env()
    if client is None:
        print("Redis is not configured: set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN (or the KV_ names).")
        return 1
    try:
        if args.cmd == "add":
            rec = add_user(client, args.email, ask_password(), name=args.name, role=args.role)
            print(f"Added {rec['email']} ({rec['role']}).")
        elif args.cmd == "passwd":
            set_password(client, args.email, ask_password())
            print("Password changed. Existing sessions stay valid until they expire (8 hours).")
        elif args.cmd in ("disable", "enable"):
            set_disabled(client, args.email, args.cmd == "disable")
            print(f"{args.email}: {args.cmd}d.")
        else:
            for u in list_users(client):
                print(
                    f"{u['email']:<36} {u['role']:<7} {'DISABLED' if u['disabled'] else 'active':<9} {u['created_at']}"
                )
    except (UserError, UpstashError) as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
