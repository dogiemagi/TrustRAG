"""SQLite persistence: users, login sessions, conversations, messages.

Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes; session tokens are stored only as
SHA-256 hashes, so a leaked database does not leak usable credentials.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

PBKDF2_ROUNDS = 200_000
SESSION_TTL = 7 * 24 * 3600
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  pw_hash TEXT NOT NULL, salt TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated DESC);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, created);
"""


class AuthError(Exception):
    pass


def _hash_pw(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS).hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
        self._failed: dict[str, list[float]] = {}

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ------------------------------------------------------------------ users
    def register(self, email: str, password: str, name: str = "") -> dict[str, Any]:
        email = email.strip().lower()
        if not _EMAIL.match(email) or len(email) > 200:
            raise AuthError("Please enter a valid email address.")
        if len(password) < 8:
            raise AuthError("Password must be at least 8 characters.")
        if len(password) > 200:
            raise AuthError("Password is too long.")
        name = (name.strip() or email.split("@")[0])[:60]
        salt = secrets.token_bytes(16)
        uid = uuid.uuid4().hex
        try:
            with self.conn() as c:
                c.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
                          (uid, email, name, _hash_pw(password, salt), salt.hex(), time.time()))
        except sqlite3.IntegrityError:
            raise AuthError("An account with this email already exists.") from None
        return {"id": uid, "email": email, "name": name}

    def login(self, email: str, password: str) -> dict[str, Any]:
        email = email.strip().lower()
        now = time.time()
        recent = [t for t in self._failed.get(email, []) if now - t < 300]
        if len(recent) >= 5:  # brute-force protection: 5 failures per 5 minutes per email
            raise AuthError("Too many failed attempts. Try again in a few minutes.")
        with self.conn() as c:
            row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        ok = row is not None and hmac.compare_digest(_hash_pw(password, bytes.fromhex(row["salt"])), row["pw_hash"])
        if not ok:
            self._failed[email] = recent + [now]
            raise AuthError("Incorrect email or password.")
        self._failed.pop(email, None)
        return {"id": row["id"], "email": row["email"], "name": row["name"]}

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.conn() as c:
            c.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            c.execute("INSERT INTO sessions VALUES (?,?,?)", (_hash_token(token), user_id, time.time() + SESSION_TTL))
        return token

    def user_for_token(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self.conn() as c:
            row = c.execute(
                "SELECT u.id, u.email, u.name FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash=? AND s.expires > ?", (_hash_token(token), time.time())).fetchone()
        return dict(row) if row else None

    def end_session(self, token: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))

    # ------------------------------------------------------------------ conversations
    def new_conversation(self, user_id: str, title: str = "New chat") -> dict[str, Any]:
        cid, now = uuid.uuid4().hex, time.time()
        with self.conn() as c:
            c.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", (cid, user_id, title, now, now))
        return {"id": cid, "title": title, "updated": now}

    def conversations(self, user_id: str) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT id, title, updated FROM conversations WHERE user_id=? ORDER BY updated DESC LIMIT 100",
                             (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def owns(self, user_id: str, conv_id: str) -> bool:
        with self.conn() as c:
            return c.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id)).fetchone() is not None

    def messages(self, conv_id: str) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT id, role, content, meta, created FROM messages WHERE conv_id=? ORDER BY created, rowid",
                             (conv_id,)).fetchall()
        return [{**dict(r), "meta": json.loads(r["meta"])} for r in rows]

    def add_message(self, conv_id: str, role: str, content: str, meta: dict | None = None) -> str:
        mid, now = uuid.uuid4().hex, time.time()
        with self.conn() as c:
            c.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", (mid, conv_id, role, content, json.dumps(meta or {}), now))
            c.execute("UPDATE conversations SET updated=? WHERE id=?", (now, conv_id))
        return mid

    def rate_message(self, user_id: str, msg_id: str, rating: int) -> bool:
        with self.conn() as c:
            row = c.execute("SELECT m.meta FROM messages m JOIN conversations v ON v.id = m.conv_id "
                            "WHERE m.id=? AND v.user_id=?", (msg_id, user_id)).fetchone()
            if not row:
                return False
            meta = json.loads(row["meta"])
            meta["rating"] = rating
            c.execute("UPDATE messages SET meta=? WHERE id=?", (json.dumps(meta), msg_id))
        return True

    def stats(self, user_id: str) -> dict[str, int]:
        with self.conn() as c:
            convs = c.execute("SELECT COUNT(*) n FROM conversations WHERE user_id=?", (user_id,)).fetchone()["n"]
            msgs = c.execute("SELECT COUNT(*) n FROM messages m JOIN conversations v ON v.id=m.conv_id "
                             "WHERE v.user_id=? AND m.role='user'", (user_id,)).fetchone()["n"]
        return {"conversations": convs, "questions": msgs}

    def rename_conversation(self, conv_id: str, title: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE conversations SET title=? WHERE id=?", (title[:80], conv_id))

    def delete_conversation(self, conv_id: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM conversations WHERE id=?", (conv_id,))

    def delete_user(self, user_id: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM users WHERE id=?", (user_id,))
