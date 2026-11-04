"""Topic requests: users ask for a theme to be analyzed, and an admin runs it or declines.

Requests live in the accounts database (users.db). When an admin decides, the requester gets a
notification that stays until they open the analysis or dismiss it. Any admin run of a requested
topic fulfills it, along with everyone else's pending request for the same topic.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass

from . import users
from .i18n import N_
from .users import UserError

MAX_TOPIC = 40  # the theme page's name limit
MAX_NOTE = 300
MAX_PENDING = 5  # per user
STATUSES = ("pending", "done", "declined")

SCHEMA = """
CREATE TABLE IF NOT EXISTS topic_requests (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  username TEXT NOT NULL,
  topic TEXT NOT NULL COLLATE NOCASE,
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created TEXT NOT NULL,
  decided TEXT,
  decided_by TEXT,
  reply TEXT NOT NULL DEFAULT '',
  saved_id TEXT,
  seen INTEGER NOT NULL DEFAULT 0
)"""


@dataclass
class TopicRequest:
    id: int
    user_id: int
    username: str
    topic: str
    note: str
    status: str
    created: str
    decided: str | None
    decided_by: str | None
    reply: str
    saved_id: str | None
    seen: bool


def _connect() -> sqlite3.Connection:
    conn = users._connect()
    conn.execute(SCHEMA)
    return conn


def _req(row) -> TopicRequest | None:
    if row is None:
        return None
    return TopicRequest(**{**dict(row), "seen": bool(row["seen"])})


def submit(user: users.User, topic: str, note: str = "") -> TopicRequest:
    topic, note = " ".join(topic.split()), note.strip()
    if not topic:
        raise UserError(N_("Enter a topic."))
    if len(topic) > MAX_TOPIC:
        raise UserError(N_("Topics can be at most {n} characters."), n=MAX_TOPIC)
    if len(note) > MAX_NOTE:
        raise UserError(N_("Notes can be at most {n} characters."), n=MAX_NOTE)
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            pending = conn.execute("SELECT topic FROM topic_requests WHERE user_id = ? AND status = 'pending'",
                                   (user.id,)).fetchall()
            if any(r["topic"].lower() == topic.lower() for r in pending):
                raise UserError(N_("You have already requested this topic."))
            if len(pending) >= MAX_PENDING:
                raise UserError(N_("You can have at most {n} open requests."), n=MAX_PENDING)
            cur = conn.execute("INSERT INTO topic_requests (user_id, username, topic, note, created) "
                               "VALUES (?, ?, ?, ?, ?)", (user.id, user.username, topic, note, users._now()))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return get(cur.lastrowid)


def get(req_id: int) -> TopicRequest | None:
    with closing(_connect()) as conn:
        return _req(conn.execute("SELECT * FROM topic_requests WHERE id = ?", (req_id,)).fetchone())


def list_requests(user_id: int | None = None, status: str | None = None) -> list[TopicRequest]:
    """Newest first, optionally only one user's or one status."""
    sql, params = "SELECT * FROM topic_requests WHERE 1 = 1", []
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    with closing(_connect()) as conn:
        return [_req(r) for r in conn.execute(sql + " ORDER BY id DESC", params)]


def pending_count() -> int:
    with closing(_connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM topic_requests WHERE status = 'pending'").fetchone()[0]


def _set_decided(conn, ids: list[int], values: tuple) -> None:
    """values = (status, reply, saved_id, admin); the requesters see it as a new notification."""
    if ids:
        conn.execute("UPDATE topic_requests SET status = ?, reply = ?, saved_id = ?, decided_by = ?, "
                     f"decided = ?, seen = 0 WHERE id IN ({','.join('?' * len(ids))})",
                     (*values, users._now(), *ids))


def _decide(pick: Callable[[sqlite3.Connection], list[int]], values: tuple) -> list[int]:
    """Decide the pending requests that `pick` selects, in one transaction; returns their ids."""
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ids = pick(conn)
            _set_decided(conn, ids, values)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return ids


def _same_topic(req_id: int) -> Callable[[sqlite3.Connection], list[int]]:
    """The request (if still pending) and every other pending request for its topic."""
    def pick(conn):
        row = conn.execute("SELECT topic FROM topic_requests WHERE id = ? AND status = 'pending'",
                           (req_id,)).fetchone()
        if row is None:
            return []
        return [req_id] + [r["id"] for r in conn.execute(
            "SELECT id FROM topic_requests WHERE status = 'pending' AND topic = ? AND id != ?",
            (row["topic"], req_id))]
    return pick


def fulfill(req_id: int, saved_id: str, admin: str) -> list[int]:
    """Mark the request, and every other pending request for the same topic, as analyzed."""
    return _decide(_same_topic(req_id), ("done", "", saved_id, admin))


def fulfill_matching(matches: Callable[[str], bool], saved_id: str, admin: str) -> list[int]:
    """Mark as analyzed every pending request whose topic `matches` (an admin ran that theme)."""
    def pick(conn):
        return [r["id"] for r in conn.execute("SELECT id, topic FROM topic_requests WHERE status = 'pending'")
                if matches(r["topic"])]
    return _decide(pick, ("done", "", saved_id, admin))


def decline(req_id: int, admin: str, reply: str = "") -> list[int]:
    """Decline the request and every other pending request for the same topic."""
    return _decide(_same_topic(req_id), ("declined", reply.strip()[:MAX_NOTE], None, admin))


def notifications(user_id: int) -> list[TopicRequest]:
    """Decided requests the user has not opened or dismissed yet."""
    with closing(_connect()) as conn:
        return [_req(r) for r in conn.execute(
            "SELECT * FROM topic_requests WHERE user_id = ? AND status != 'pending' AND seen = 0 ORDER BY decided",
            (user_id,))]


def mark_seen(user_id: int, req_id: int | None = None) -> None:
    """Clear one notification, or all of the user's."""
    sql, params = "UPDATE topic_requests SET seen = 1 WHERE user_id = ? AND status != 'pending'", [user_id]
    if req_id is not None:
        sql += " AND id = ?"
        params.append(req_id)
    with closing(_connect()) as conn:
        conn.execute(sql, params)


def withdraw(user_id: int, req_id: int) -> bool:
    """A user takes back their own pending request."""
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM topic_requests WHERE id = ? AND user_id = ? AND status = 'pending'",
                           (req_id, user_id))
        return cur.rowcount > 0
