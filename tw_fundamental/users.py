"""User accounts for the web UI: registration, sign-in, roles, and admin tools.

Accounts live in an SQLite database under DATA_DIR (default ./data, or $TWF_DATA_DIR), kept
apart from .cache so clearing the cache does not delete them. Passwords are stored as salted
scrypt hashes (werkzeug). The first account ever created becomes an admin; admins can run
Claude research and manage other users, and privileged users have the same rights.

    python -m tw_fundamental.users list
    python -m tw_fundamental.users create alice [--admin]
    python -m tw_fundamental.users reset-password alice
    python -m tw_fundamental.users set-admin alice on|off
    python -m tw_fundamental.users set-privileged alice on|off
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from .i18n import N_

DATA_DIR = Path(os.environ.get("TWF_DATA_DIR", "data"))
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD = 8
MAX_PASSWORD = 200
# Compared against when the username does not exist, so a miss costs the same time as a hit.
_DUMMY_HASH = generate_password_hash("not-a-real-password")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  is_privileged INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  session_version INTEGER NOT NULL DEFAULT 1,
  created TEXT NOT NULL,
  last_login TEXT
)"""


class UserError(ValueError):
    """A user-facing error. `message` is an English catalog string; `kw` fills its placeholders."""

    def __init__(self, message: str, **kw):
        super().__init__(message.format(**kw) if kw else message)
        self.message, self.kw = message, kw


@dataclass
class User:
    id: int
    username: str
    is_admin: bool
    is_active: bool
    session_version: int
    created: str
    last_login: str | None
    is_guest: bool = False
    is_privileged: bool = False

    @property
    def has_admin_rights(self) -> bool:
        return self.is_admin or self.is_privileged


# Signed-in visitors without an account. Parentheses cannot appear in a real username, so a guest
# never matches an account or the owner of a saved analysis.
GUEST = User(0, "(guest)", False, True, 0, "", None, is_guest=True)


def db_path() -> Path:
    return DATA_DIR / "users.db"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # explicit transactions below
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    if "is_privileged" not in {r["name"] for r in conn.execute("PRAGMA table_info(users)")}:
        try:  # databases from before privileged users
            conn.execute("ALTER TABLE users ADD COLUMN is_privileged INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # another request added it first
    return conn


def _user(row) -> User | None:
    if row is None:
        return None
    return User(row["id"], row["username"], bool(row["is_admin"]), bool(row["is_active"]),
                row["session_version"], row["created"], row["last_login"],
                is_privileged=bool(row["is_privileged"]))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def secret_key() -> str:
    """Key that signs session cookies: $TWF_SECRET_KEY, else a random key saved in DATA_DIR."""
    if os.environ.get("TWF_SECRET_KEY"):
        return os.environ["TWF_SECRET_KEY"]
    path = DATA_DIR / "secret_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return path.read_text().strip()


def validate_username(username: str) -> str:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise UserError(N_("Username must be 3–32 characters: letters, digits, “.”, “_” or “-”."))
    return username


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise UserError(N_("Password must be at least {n} characters."), n=MIN_PASSWORD)
    if len(password) > MAX_PASSWORD:
        raise UserError(N_("Password must be at most {n} characters."), n=MAX_PASSWORD)


def create(username: str, password: str, is_admin: bool | None = None) -> User:
    """Create an account. With is_admin=None the very first account becomes an admin."""
    username = validate_username(username)
    validate_password(password)
    pw_hash = generate_password_hash(password)
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")  # so two first registrations cannot both become admin
        try:
            if is_admin is None:
                is_admin = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            cur = conn.execute("INSERT INTO users (username, password_hash, is_admin, created) VALUES (?, ?, ?, ?)",
                               (username, pw_hash, int(is_admin), _now()))
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise UserError(N_("That username is already taken.")) from None
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return get(cur.lastrowid)


def get(user_id: int) -> User | None:
    with closing(_connect()) as conn:
        return _user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_by_name(username: str) -> User | None:
    with closing(_connect()) as conn:
        return _user(conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone())


def list_users() -> list[User]:
    with closing(_connect()) as conn:
        return [_user(r) for r in conn.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE")]


def count() -> int:
    with closing(_connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def authenticate(username: str, password: str) -> User | None:
    """The user if the password matches and the account is active; records the sign-in time."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password[:MAX_PASSWORD])
        if not (row and ok and row["is_active"]):
            return None
    record_login(row["id"])
    return get(row["id"])


def record_login(user_id: int) -> None:
    with closing(_connect()) as conn:
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (_now(), user_id))


def change_password(user_id: int, current: str, new: str) -> None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or not check_password_hash(row["password_hash"], current[:MAX_PASSWORD]):
        raise UserError(N_("The current password is not correct."))
    set_password(user_id, new)


def set_password(user_id: int, password: str) -> None:
    """Set a password and sign the user out everywhere (sessions carry session_version)."""
    validate_password(password)
    with closing(_connect()) as conn:
        conn.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                     (generate_password_hash(password), user_id))


def reset_password(user_id: int) -> str:
    """Give the user a random temporary password and return it."""
    temp = secrets.token_urlsafe(9)
    set_password(user_id, temp)
    return temp


def _guard_last_admin(conn, user_id: int) -> None:
    others = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1 AND is_active = 1 AND id != ?",
                          (user_id,)).fetchone()[0]
    target = conn.execute("SELECT is_admin, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    if target and target["is_admin"] and target["is_active"] and others == 0:
        raise UserError(N_("There must be at least one active admin."))


def _update(user_id: int, sql: str, params: tuple, guard: bool) -> None:
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if guard:
                _guard_last_admin(conn, user_id)
            conn.execute(sql, params)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def set_admin(user_id: int, is_admin: bool) -> None:
    _update(user_id, "UPDATE users SET is_admin = ?, is_privileged = 0 WHERE id = ?", (int(is_admin), user_id),
            guard=not is_admin)


def set_privileged(user_id: int, privileged: bool) -> None:
    """Admin rights without being an admin. A user holds one role, so granting it ends any admin role."""
    if privileged:
        _update(user_id, "UPDATE users SET is_privileged = 1, is_admin = 0 WHERE id = ?", (user_id,), guard=True)
    else:
        _update(user_id, "UPDATE users SET is_privileged = 0 WHERE id = ?", (user_id,), guard=False)


def set_active(user_id: int, active: bool) -> None:
    # Disabling also bumps session_version, which signs the user out at once.
    _update(user_id, "UPDATE users SET is_active = ?, session_version = session_version + 1 WHERE id = ?",
            (int(active), user_id), guard=not active)


def delete(user_id: int) -> None:
    from . import activity, portfolio
    _update(user_id, "DELETE FROM users WHERE id = ?", (user_id,), guard=True)
    activity.mark_deleted(user_id)
    portfolio.remove_user(user_id)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass
    import sys

    ap = argparse.ArgumentParser(description="Manage web UI user accounts.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list accounts")
    c = sub.add_parser("create", help="create an account (asks for the password)")
    c.add_argument("username")
    c.add_argument("--admin", action="store_true")
    r = sub.add_parser("reset-password", help="set a new password (asks for it)")
    r.add_argument("username")
    a = sub.add_parser("set-admin", help="grant or remove admin rights")
    a.add_argument("username")
    a.add_argument("state", choices=["on", "off"])
    v = sub.add_parser("set-privileged", help="grant or remove privileged rights (same as an admin's)")
    v.add_argument("username")
    v.add_argument("state", choices=["on", "off"])
    args = ap.parse_args(argv)

    try:
        if args.cmd == "list":
            for u in list_users():
                flags = ", ".join(f for f, on in (("admin", u.is_admin), ("privileged", u.is_privileged),
                                                  ("disabled", not u.is_active)) if on)
                print(f"{u.username:<24} {flags or 'user':<16} created {u.created}  last sign-in {u.last_login or '–'}")
            return 0
        if args.cmd == "create":
            pw = getpass.getpass("Password: ")
            if pw != getpass.getpass("Repeat password: "):
                print("error: passwords do not match", file=sys.stderr)
                return 1
            u = create(args.username, pw, is_admin=True if args.admin else None)
            print(f"created {u.username}{' (admin)' if u.is_admin else ''}")
            return 0
        u = get_by_name(args.username)
        if u is None:
            print(f"error: no such user: {args.username}", file=sys.stderr)
            return 1
        if args.cmd == "reset-password":
            pw = getpass.getpass("New password: ")
            if pw != getpass.getpass("Repeat password: "):
                print("error: passwords do not match", file=sys.stderr)
                return 1
            set_password(u.id, pw)
            print(f"password changed for {u.username}; their sessions are signed out")
        elif args.cmd == "set-admin":
            set_admin(u.id, args.state == "on")
            print(f"{u.username} is {'now' if args.state == 'on' else 'no longer'} an admin")
        elif args.cmd == "set-privileged":
            set_privileged(u.id, args.state == "on")
            print(f"{u.username} is {'now' if args.state == 'on' else 'no longer'} privileged")
        return 0
    except UserError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
