"""What users do in the web UI, for admins: sign-ins, reports read, analyses run, admin actions.

Entries live in the accounts database (users.db) and are kept for KEEP_DAYS. No IP addresses or
passwords are stored. Guests share one identity, users.GUEST.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import users
from .i18n import N_

KEEP_DAYS = 180
PAGE_SIZE = 100
PRUNE_EVERY = 500  # entries

# Action -> label, in the order the filter lists them.
ACTIONS = {
    "sign-in": N_("Signed in"),
    "sign-in-failed": N_("Failed sign-in"),
    "sign-out": N_("Signed out"),
    "register": N_("Created an account"),
    "guest": N_("Continued as guest"),
    "view-report": N_("Opened a stock report"),
    "view-saved": N_("Opened a saved analysis"),
    "run-theme": N_("Ran a theme analysis"),
    "delete-saved": N_("Deleted a saved analysis"),
    "portfolio-add": N_("Added to their portfolio"),
    "portfolio-remove": N_("Removed from their portfolio"),
    "request-topic": N_("Requested a topic"),
    "withdraw-request": N_("Withdrew a topic request"),
    "decline-request": N_("Declined a topic request"),
    "change-password": N_("Changed their password"),
    "grant-admin": N_("Made a user an admin"),
    "revoke-admin": N_("Removed an admin"),
    "grant-privileged": N_("Made a user privileged"),
    "revoke-privileged": N_("Removed a privileged user"),
    "disable": N_("Disabled a user"),
    "enable": N_("Enabled a user"),
    "reset-password": N_("Reset a user’s password"),
    "delete": N_("Deleted a user"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  username TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL
)"""
INDEX = "CREATE INDEX IF NOT EXISTS activity_user ON activity (user_id, id)"


@dataclass
class Entry:
    id: int
    user_id: int
    username: str
    action: str
    detail: str
    created: str


def _connect() -> sqlite3.Connection:
    conn = users._connect()
    conn.execute(SCHEMA)
    conn.execute(INDEX)
    return conn


def log(user: users.User, action: str, detail: str = "") -> None:
    """Record an action. A logging failure must never break the page the user asked for."""
    try:
        with closing(_connect()) as conn:
            cur = conn.execute("INSERT INTO activity (user_id, username, action, detail, created) "
                               "VALUES (?, ?, ?, ?, ?)", (user.id, user.username, action, detail[:200], users._now()))
            if cur.lastrowid % PRUNE_EVERY == 0:
                cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
                conn.execute("DELETE FROM activity WHERE created < ?", (cutoff,))
    except sqlite3.Error:
        pass


def recent(user_id: int | None = None, action: str | None = None, before: int | None = None,
           limit: int = PAGE_SIZE) -> list[Entry]:
    """Newest first, optionally one user's or one action's; `before` is an entry id, for paging."""
    sql, params = "SELECT * FROM activity WHERE 1 = 1", []
    for clause, value in (("user_id = ?", user_id), ("action = ?", action), ("id < ?", before)):
        if value is not None:
            sql += f" AND {clause}"
            params.append(value)
    with closing(_connect()) as conn:
        return [Entry(**dict(r)) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))]


def active_users(days: int = 7) -> int:
    """Accounts that did anything in the last `days` days (guests are not accounts)."""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(_connect()) as conn:
        return conn.execute("SELECT COUNT(DISTINCT user_id) FROM activity WHERE user_id > 0 AND created >= ?",
                            (since,)).fetchone()[0]


def mark_deleted(user_id: int) -> None:
    """Keep a deleted account's history, detached so a later account cannot inherit it (ids and names are reused)."""
    with closing(_connect()) as conn:
        conn.execute("UPDATE activity SET user_id = -1, username = username || ' (deleted)' WHERE user_id = ?",
                     (user_id,))
