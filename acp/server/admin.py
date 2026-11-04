"""Admin commands (run on the server, there is no admin web surface).

python -m acp.server.admin create-user --name alice     # prints the API key ONCE
python -m acp.server.admin list-users
python -m acp.server.admin disable-user <user_id>
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from acp import store
from acp.config import ROOT, get_settings
from acp.server import auth


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="acp.server.admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("--name", required=True)
    sub.add_parser("list-users")
    d = sub.add_parser("disable-user")
    d.add_argument("user_id")
    args = p.parse_args(argv)
    load_dotenv(ROOT / ".env")
    conn = store.connect(get_settings().db_path)
    try:
        if args.cmd == "create-user":
            user_id, key = auth.create_user(conn, args.name)
            print(f"user_id: {user_id}\napi_key: {key}\n(Store this key now; it cannot be shown again.)")
        elif args.cmd == "list-users":
            for r in conn.execute("SELECT id, name, key_prefix, created_at, disabled FROM users ORDER BY created_at"):
                state = "DISABLED" if r["disabled"] else "active"
                print(f"{r['id']}  {r['name']:<20} {r['key_prefix']}...  {r['created_at']}  {state}")
        else:
            print("disabled" if auth.set_disabled(conn, args.user_id, True) else "no such user")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
