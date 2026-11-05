"""The Python side of dashboard accounts: hashing, the register in Redis, and the command-line tool."""

import json

import pytest

from acp import dashboard_users as du
from tests.test_assure_ingest import Upstash
from tests.test_assure_publish import up


class Redis(Upstash):
    """The fake Redis plus SADD and SMEMBERS."""

    def __call__(self, url, headers, body):
        cmds = json.loads(body)
        if not any(c[0] in ("SADD", "SMEMBERS") for c in cmds):
            return super().__call__(url, headers, body)
        out = []
        for c in cmds:
            if c[0] == "SADD":
                self.lists.setdefault(c[1], [])
                if c[2] not in self.lists[c[1]]:
                    self.lists[c[1]].append(c[2])
                out.append({"result": 1})
            elif c[0] == "SMEMBERS":
                out.append({"result": list(self.lists.get(c[1], []))})
            else:
                out.append(json.loads(super().__call__(url, headers, json.dumps([c]).encode())[1])[0])
        return 200, json.dumps(out).encode()


def test_hashes_are_salted_slow_and_verify_only_the_right_password():
    a, b = du.hash_password("a long enough password"), du.hash_password("a long enough password")
    assert a != b and a.startswith("scrypt$16384$8$1$")  # a fresh salt each time
    assert du.verify_password(a, "a long enough password") and not du.verify_password(a, "another long password")
    assert not du.verify_password("garbage", "x") and not du.verify_password("md5$1$2$3$a$b", "x")
    with pytest.raises(du.UserError, match="at least"):
        du.hash_password("short")


def test_adding_listing_disabling_and_changing_a_password():
    c, fake = up(Redis())
    rec = du.add_user(c, " Ada@Example.com ", "ada-long-password-1", name="Ada", role="admin")
    assert rec["email"] == "ada@example.com" and "ada-long-password-1" not in json.dumps(fake.kv)
    with pytest.raises(du.UserError, match="already exists"):
        du.add_user(c, "ada@example.com", "ada-long-password-1")
    with pytest.raises(du.UserError, match="Role"):
        du.add_user(c, "x@example.com", "x-long-password-12", role="root")
    with pytest.raises(du.UserError, match="email"):
        du.add_user(c, "not-an-email", "x-long-password-12")
    du.set_disabled(c, "ada@example.com", True)
    assert du.list_users(c) == [
        {"email": "ada@example.com", "name": "Ada", "role": "admin", "created_at": rec["created_at"], "disabled": True}
    ]
    du.set_password(c, "ada@example.com", "a-brand-new-password")
    assert du.verify_password(du.get_user(c, "ada@example.com")["hash"], "a-brand-new-password")
    with pytest.raises(du.UserError, match="No such"):
        du.set_password(c, "ghost@example.com", "a-brand-new-password")


def test_the_command_line_never_takes_a_password_as_an_argument(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "dashboard_user", Path(__file__).resolve().parents[1] / "scripts" / "dashboard_user.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    fake = Redis()
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(cli, "client_from_env", lambda: up(fake)[0])
    monkeypatch.setenv("ACP_USER_PASSWORD", "cli-long-password-1")
    assert cli.main(["add", "cli@example.com", "--role", "viewer"]) == 0 and cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "cli@example.com" in out and "cli-long-password-1" not in out
    with pytest.raises(SystemExit):  # there is no --password flag
        cli.main(["add", "x@example.com", "--password", "oops-long-password"])
    assert cli.main(["add", "cli@example.com"]) == 1  # duplicates are refused, with a message not a traceback
