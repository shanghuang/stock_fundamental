"""Each user's portfolio: the stocks and themes they chose to follow.

Lives in the accounts database (users.db). It is a list of what to follow, not holdings, so it has
no share counts or costs. Guests have no account and so no portfolio.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass

from . import users
from .i18n import N_

MAX_STOCKS = 100
MAX_THEMES = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_stock (
  user_id INTEGER NOT NULL,
  stock_id TEXT NOT NULL,
  added TEXT NOT NULL,
  PRIMARY KEY (user_id, stock_id)
);
CREATE TABLE IF NOT EXISTS portfolio_theme (
  user_id INTEGER NOT NULL,
  key TEXT NOT NULL COLLATE NOCASE,
  title TEXT NOT NULL,
  keywords TEXT NOT NULL DEFAULT '',
  stocks TEXT NOT NULL DEFAULT '',
  added TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);"""


@dataclass
class StockItem:
    stock_id: str
    added: str


@dataclass
class ThemeItem:
    key: str
    title: str
    keywords: str  # a custom theme's own keywords and stock codes, to run it again
    stocks: str
    added: str


def _connect() -> sqlite3.Connection:
    conn = users._connect()
    conn.executescript(SCHEMA)
    return conn


def stocks(user_id: int) -> list[StockItem]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT stock_id, added FROM portfolio_stock WHERE user_id = ? ORDER BY stock_id",
                            (user_id,)).fetchall()
    return [StockItem(r["stock_id"], r["added"]) for r in rows]


def themes(user_id: int) -> list[ThemeItem]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT key, title, keywords, stocks, added FROM portfolio_theme "
                            "WHERE user_id = ? ORDER BY added, key", (user_id,)).fetchall()
    return [ThemeItem(r["key"], r["title"], r["keywords"], r["stocks"], r["added"]) for r in rows]


def has_stock(user_id: int, stock_id: str) -> bool:
    with closing(_connect()) as conn:
        return conn.execute("SELECT 1 FROM portfolio_stock WHERE user_id = ? AND stock_id = ?",
                            (user_id, stock_id)).fetchone() is not None


def has_theme(user_id: int, key: str) -> bool:
    with closing(_connect()) as conn:
        return conn.execute("SELECT 1 FROM portfolio_theme WHERE user_id = ? AND key = ?",
                            (user_id, key)).fetchone() is not None


def add_stock(user_id: int, stock_id: str) -> bool:
    """True if the stock was added, False if it was already there."""
    if has_stock(user_id, stock_id):
        return False
    with closing(_connect()) as conn:
        n = conn.execute("SELECT COUNT(*) FROM portfolio_stock WHERE user_id = ?", (user_id,)).fetchone()[0]
        if n >= MAX_STOCKS:
            raise users.UserError(N_("A portfolio can hold up to {n} stocks."), n=MAX_STOCKS)
        conn.execute("INSERT OR IGNORE INTO portfolio_stock (user_id, stock_id, added) VALUES (?, ?, ?)",
                     (user_id, stock_id, users._now()))
    return True


def add_theme(user_id: int, key: str, title: str, keywords: str = "", stocks: str = "") -> bool:
    """True if the theme was added, False if it was there already (its details are updated)."""
    new = not has_theme(user_id, key)
    with closing(_connect()) as conn:
        if new:
            n = conn.execute("SELECT COUNT(*) FROM portfolio_theme WHERE user_id = ?", (user_id,)).fetchone()[0]
            if n >= MAX_THEMES:
                raise users.UserError(N_("A portfolio can hold up to {n} themes."), n=MAX_THEMES)
        conn.execute("INSERT INTO portfolio_theme (user_id, key, title, keywords, stocks, added) "
                     "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (user_id, key) DO UPDATE SET "
                     "title = excluded.title, keywords = excluded.keywords, stocks = excluded.stocks",
                     (user_id, key, title, keywords, stocks, users._now()))
    return new


def remove_stock(user_id: int, stock_id: str) -> bool:
    with closing(_connect()) as conn:
        return conn.execute("DELETE FROM portfolio_stock WHERE user_id = ? AND stock_id = ?",
                            (user_id, stock_id)).rowcount > 0


def remove_theme(user_id: int, key: str) -> bool:
    with closing(_connect()) as conn:
        return conn.execute("DELETE FROM portfolio_theme WHERE user_id = ? AND key = ?",
                            (user_id, key)).rowcount > 0


def remove_user(user_id: int) -> None:
    """Called when an account is deleted. SQLite can reuse the id, so nothing may be left behind."""
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM portfolio_stock WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM portfolio_theme WHERE user_id = ?", (user_id,))
