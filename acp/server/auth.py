"""API keys: how the service knows who is calling.

Analogy: a hotel key card. We hand the card over once and keep only a fingerprint (sha256 hash) of it; if our
database leaked, the fingerprints would not open any door. Each key belongs to one user, and every document and every
question is tied to that user.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import UTC, datetime
from typing import Any

KEY_PREFIX = "acp_"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_user(conn: sqlite3.Connection, name: str) -> tuple[str, str]:
    """Create a user and return (user_id, api_key). The key is shown ONCE and never stored."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    user_id = secrets.token_hex(8)  # 16 hex chars: also the Pinecone namespace and a directory name
    with conn:
        conn.execute(
            "INSERT INTO users (id, name, key_hash, key_prefix, created_at) VALUES (?,?,?,?,?)",
            (
                user_id,
                name.strip()[:80] or "user",
                hash_key(key),
                key[:8],
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
    return user_id, key


def authenticate(conn: sqlite3.Connection, key: str | None) -> dict[str, Any] | None:
    """Return the user row for a valid, enabled key, else None."""
    if not key or not key.startswith(KEY_PREFIX) or len(key) > 200:
        return None
    digest = hash_key(key)
    row = conn.execute("SELECT * FROM users WHERE key_hash = ?", (digest,)).fetchone()
    if row is None or row["disabled"] or not hmac.compare_digest(row["key_hash"], digest):
        return None
    return dict(row)


def set_disabled(conn: sqlite3.Connection, user_id: str, disabled: bool) -> bool:
    with conn:
        return conn.execute("UPDATE users SET disabled=? WHERE id=?", (int(disabled), user_id)).rowcount == 1
