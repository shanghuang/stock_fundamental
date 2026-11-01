"""Reshape FinMind's long-format financial statements into quarterly tables.

FinMind conventions (verified against TSMC filings):
- Income statement rows are single-quarter figures (Q4 is already standalone).
- Cash-flow rows are year-to-date cumulative (Q1, H1, 9M, FY) and must be
  de-cumulated to get single quarters.
- Balance-sheet rows are point-in-time; rows ending in "_per" are % of total
  assets and are dropped here.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd


def pivot_statement(df: pd.DataFrame) -> pd.DataFrame:
    """Long rows (date, type, value) -> one row per quarter, one column per line item.

    The index is a contiguous quarterly PeriodIndex, so `shift(4)` is always
    "same quarter last year" even when a filing is missing.
    """
    if df.empty:
        return pd.DataFrame(index=pd.PeriodIndex([], freq="Q"))
    df = df[~df["type"].str.endswith("_per")]
    wide = df.pivot_table(index="date", columns="type", values="value", aggfunc="last")
    wide.index = pd.PeriodIndex(pd.to_datetime(wide.index), freq="Q")
    wide = wide.groupby(level=0).last()
    full = pd.period_range(wide.index.min(), wide.index.max(), freq="Q")
    wide = wide.reindex(full)
    wide.columns.name = None
    return wide


def decumulate_ytd(wide: pd.DataFrame) -> pd.DataFrame:
    """Turn year-to-date cumulative values into single-quarter values.

    Q1 is kept as-is; Qn = YTD(n) - YTD(n-1). A quarter whose previous quarter
    is missing becomes NaN rather than silently reporting a multi-quarter sum.
    """
    if wide.empty:
        return wide
    prev = wide.shift(1)
    is_q1 = (wide.index.quarter == 1)[:, None]
    return pd.DataFrame(np.where(is_q1, wide, wide - prev), index=wide.index, columns=wide.columns)


def col(df: pd.DataFrame, name: str) -> pd.Series:
    """Column if present, else an all-NaN series (banks/insurers lack many items)."""
    if name in df.columns:
        return df[name].astype(float)
    return pd.Series(np.nan, index=df.index, dtype=float)


def first_col(df: pd.DataFrame, *names: str) -> pd.Series:
    """First of several alternative line-item names that exists (naming differs by industry)."""
    for name in names:
        if name in df.columns:
            return col(df, name)
    return col(df, names[0])


def roc_fiscal_year(label: str) -> int | None:
    """'113年第3季' or '113年' -> 2024 (ROC year + 1911)."""
    m = re.search(r"(\d+)\s*年", str(label))
    return int(m.group(1)) + 1911 if m else None
