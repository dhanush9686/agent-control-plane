"""Dashboard accounts: password hashing and the user records the sign-in function reads from Redis.

Analogy: the staff register at the front desk. Each person has their own entry with a salted, deliberately slow hash
of their password (scrypt), never the password. The website only ever reads this register; adding or disabling people
is done here, from a trusted machine, with the write token.

Keys: `acp:user:<email>` (JSON record) and `acp:users` (the set of emails). The record format is read by
`dashboard/api/_users.js`, which verifies the same scrypt string, so the two must stay in step (a test checks it).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from datetime import UTC, datetime
from typing import Any

from acp.assure.publish import UpstashClient

SCRYPT_N, SCRYPT_R, SCRYPT_P, DKLEN = 16384, 8, 1, 64
MIN_PASSWORD = 12
ROLES = ("admin", "viewer")
KEY_USERS = "acp:users"
_EMAIL = re.compile(r"^[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}$")


class UserError(Exception):
    pass


def normalize_email(email: str) -> str:
    e = email.strip().lower()
    if not _EMAIL.match(e):
        raise UserError("That does not look like an email address.")
    return e


def user_key(email: str) -> str:
    return f"acp:user:{normalize_email(email)}"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """`scrypt$N$r$p$<salt b64>$<hash b64>`. Refuses short passwords."""
    if len(password) < MIN_PASSWORD:
        raise UserError(f"Use at least {MIN_PASSWORD} characters.")
    salt = salt or os.urandom(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN, maxmem=64 * 1024 * 1024
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(stored: str, password: str) -> bool:
    try:
        scheme, n, r, p, salt, want = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
            dklen=len(base64.b64decode(want)), maxmem=64 * 1024 * 1024,
        )  # fmt: skip
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(want))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def get_user(client: UpstashClient, email: str) -> dict[str, Any] | None:
    return client.get_json(user_key(email))


def save_user(client: UpstashClient, rec: dict[str, Any]) -> None:
    client.pipeline([["SET", user_key(rec["email"]), json.dumps(rec)], ["SADD", KEY_USERS, rec["email"]]])


def add_user(
    client: UpstashClient, email: str, password: str, *, name: str = "", role: str = "viewer"
) -> dict[str, Any]:
    email = normalize_email(email)
    if role not in ROLES:
        raise UserError(f"Role must be one of {', '.join(ROLES)}.")
    if get_user(client, email):
        raise UserError("That user already exists; use `passwd` to change the password.")
    rec = {"email": email, "name": name.strip()[:80] or email.split("@")[0], "role": role,
           "hash": hash_password(password), "created_at": _now(), "disabled": False}  # fmt: skip
    save_user(client, rec)
    return rec


def set_password(client: UpstashClient, email: str, password: str) -> None:
    rec = get_user(client, email)
    if not rec:
        raise UserError("No such user.")
    rec["hash"] = hash_password(password)
    save_user(client, rec)


def set_disabled(client: UpstashClient, email: str, disabled: bool) -> None:
    rec = get_user(client, email)
    if not rec:
        raise UserError("No such user.")
    rec["disabled"] = disabled
    save_user(client, rec)


def list_users(client: UpstashClient) -> list[dict[str, Any]]:
    emails = sorted(client.pipeline([["SMEMBERS", KEY_USERS]])[0] or [])
    out = []
    for e in emails:
        rec = get_user(client, e)
        if rec:
            out.append({k: rec.get(k) for k in ("email", "name", "role", "created_at", "disabled")})
    return out
