"""Glue shared by the CLI and the web UI: fetch -> metrics -> HTML."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from . import analysis, forecast, report, theme, theme_store
from .finmind import FinMindError, fetch

STOCK_ID_RE = re.compile(r"^[0-9A-Za-z]{4,6}$")

# Industry categories that are funds/derivatives rather than operating companies.
_NON_COMPANY = re.compile(r"ETF|ETN|Index|受益證券|存託憑證|大盤|所有證券")


@dataclass
class Report:
    data: analysis.StockData
    valuation: dict
    checks: list[tuple[bool, str]]
    html: str
    price: dict = field(default_factory=dict)  # forecast.price_forecast() output
    outlook: dict | None = None                # theme_store.stock_outlook(): the theme run it borrows from


def price_forecast(d: analysis.StockData, refresh: bool = False) -> tuple[dict, dict | None]:
    """Implied prices for one stock, as in a theme analysis.

    Analysts' estimates and outlook growth come from the newest saved theme run that included the
    stock; without one, EPS grows at the long-run rate after year 1. The P/E band needs 5 years of
    history, so a shorter report loads it separately.
    """
    if len(d.daily) and d.daily.index.min() > d.daily.index.max() - pd.DateOffset(years=5):
        d = analysis.load(d.stock_id, 5, refresh=refresh)
    p = theme.profile_from_data(d)
    outlook = theme_store.stock_outlook(d.stock_id)
    est = (outlook or {}).get("eps_estimate")
    p.forward = forecast.forward_pe(p, {d.stock_id: est} if est else None)
    growth = outlook["end_growth"] if outlook else forecast.LONG_RUN_GROWTH
    return forecast.price_forecast(p, (outlook or {}).get("eps_by_year"), growth), outlook


def build_report(stock_id: str, years: int = 5, refresh: bool = False, nav: str = "",
                 lang: str = "en", saved_url: Callable[[str], str] | None = None) -> Report:
    """`saved_url` turns a saved theme run's id into a link (web UI only)."""
    if not STOCK_ID_RE.match(stock_id):
        raise FinMindError(f"Invalid stock id: {stock_id!r}")
    d = analysis.load(stock_id, years, refresh=refresh)
    q_all = analysis.quarterly_metrics(d)
    q = q_all.tail(years * 4)
    rev = analysis.monthly_revenue(d)
    divs = analysis.annual_dividends(d, q_all)
    val = analysis.valuation_summary(d, years)
    checks = analysis.checks(q, rev, divs, financial=d.is_financial, lang=lang)
    price, outlook = price_forecast(d, refresh)
    html = report.render(d, q, rev, divs, val, checks, nav=nav, lang=lang, price=price, outlook=outlook,
                         saved_url=saved_url)
    return Report(d, val, checks, html, price, outlook)


def list_stocks(refresh: bool = False) -> list[dict]:
    """Listed (TWSE) and OTC (TPEx) operating companies, sorted by id."""
    info = fetch("TaiwanStockInfo", max_age_hours=24, refresh=refresh)
    if info.empty:
        return []
    info = info[info["type"].isin(["twse", "tpex"])
                & info["stock_id"].str.fullmatch(r"[1-9]\d{3}")
                & ~info["industry_category"].str.contains(_NON_COMPANY)]
    grouped = info.groupby("stock_id", sort=True).agg(
        name=("stock_name", "last"),
        market=("type", "last"),
        industry=("industry_category", lambda s: " / ".join(dict.fromkeys(s))),
    )
    return [{"id": i, **row} for i, row in grouped.to_dict("index").items()]


__all__ = ["Report", "build_report", "list_stocks", "STOCK_ID_RE"]
