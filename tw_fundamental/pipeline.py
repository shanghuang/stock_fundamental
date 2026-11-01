"""Glue shared by the CLI and the web UI: fetch -> metrics -> HTML."""
from __future__ import annotations

import re
from dataclasses import dataclass


from . import analysis, report
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


def build_report(stock_id: str, years: int = 5, refresh: bool = False, nav: str = "",
                 lang: str = "en") -> Report:
    if not STOCK_ID_RE.match(stock_id):
        raise FinMindError(f"Invalid stock id: {stock_id!r}")
    d = analysis.load(stock_id, years, refresh=refresh)
    q_all = analysis.quarterly_metrics(d)
    q = q_all.tail(years * 4)
    rev = analysis.monthly_revenue(d)
    divs = analysis.annual_dividends(d, q_all)
    val = analysis.valuation_summary(d, years)
    checks = analysis.checks(q, rev, divs, financial=d.is_financial, lang=lang)
    return Report(d, val, checks, report.render(d, q, rev, divs, val, checks, nav=nav, lang=lang))


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
