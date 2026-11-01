"""Load one stock's data from FinMind and compute fundamental metrics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .finmind import FinMindError, fetch
from .statements import col, decumulate_ytd, first_col, pivot_statement, roc_fiscal_year


@dataclass
class StockData:
    stock_id: str
    name: str
    industry: str
    market: str            # "twse" (上市) or "tpex" (上櫃)
    income: pd.DataFrame   # quarterly, single-quarter values
    balance: pd.DataFrame  # quarterly, point-in-time
    cashflow: pd.DataFrame # quarterly, de-cumulated to single quarters
    revenue: pd.DataFrame  # monthly revenue (月營收)
    daily: pd.DataFrame    # close, PER, PBR, dividend_yield by trading day
    dividends: pd.DataFrame  # raw dividend announcements

    @property
    def is_financial(self) -> bool:
        """Banks/insurers/financial holdings: different statement layout, leverage is normal."""
        return "金融" in self.industry


def load(stock_id: str, years: int = 5, refresh: bool = False) -> StockData:
    """Fetch everything needed for a report. One extra year is pulled for YoY/TTM math."""
    start = date(date.today().year - years - 1, 1, 1).isoformat()

    def get(dataset: str, start_date: str | None = start) -> pd.DataFrame:
        return fetch(dataset, stock_id, start_date, refresh=refresh)

    info = get("TaiwanStockInfo", None)
    if info.empty:
        raise FinMindError(f"Unknown stock id: {stock_id}")

    price = get("TaiwanStockPrice")
    per = get("TaiwanStockPER")
    daily = pd.DataFrame()
    if not per.empty:
        daily = per.set_index(pd.to_datetime(per["date"]))[["PER", "PBR", "dividend_yield"]]
    if not price.empty:
        close = price.set_index(pd.to_datetime(price["date"]))["close"]
        daily = daily.join(close, how="outer") if not daily.empty else close.to_frame()

    rev = get("TaiwanStockMonthRevenue")
    revenue = pd.DataFrame(columns=["revenue"])
    if not rev.empty:
        idx = pd.PeriodIndex([pd.Period(year=y, month=m, freq="M")
                              for y, m in zip(rev["revenue_year"], rev["revenue_month"])])
        revenue = pd.DataFrame({"revenue": rev["revenue"].astype(float).values}, index=idx)
        revenue = revenue.groupby(level=0).last()
        revenue = revenue.reindex(pd.period_range(revenue.index.min(), revenue.index.max(), freq="M"))

    return StockData(
        stock_id=stock_id,
        name=str(info["stock_name"].iloc[0]),
        industry=" / ".join(dict.fromkeys(info["industry_category"].astype(str))),
        market=str(info["type"].iloc[0]),
        income=pivot_statement(get("TaiwanStockFinancialStatements")),
        balance=pivot_statement(get("TaiwanStockBalanceSheet")),
        cashflow=decumulate_ytd(pivot_statement(get("TaiwanStockCashFlowsStatement"))),
        revenue=revenue,
        daily=daily.sort_index(),
        dividends=get("TaiwanStockDividend"),
    )


def _ratio(a: pd.Series, b: pd.Series) -> pd.Series:
    return a / b.where(b != 0)


def quarterly_metrics(d: StockData) -> pd.DataFrame:
    """One row per quarter: growth, margins, returns, balance-sheet health, cash flow."""
    idx = d.income.index.union(d.balance.index).union(d.cashflow.index)
    inc, bal, cf = (x.reindex(idx) for x in (d.income, d.balance, d.cashflow))

    revenue = col(inc, "Revenue")
    net_income = first_col(inc, "IncomeAfterTaxes", "IncomeAfterTax")
    ni_parent = col(inc, "EquityAttributableToOwnersOfParent")
    if ni_parent.isna().all():  # standalone filers have no parent/NCI split
        ni_parent = net_income
    eps = col(inc, "EPS")
    equity_parent = col(bal, "EquityAttributableToOwnersOfParent")
    if equity_parent.isna().all():  # financial companies only report total and NCI
        equity_parent = col(bal, "Equity") - col(bal, "NoncontrollingInterests").fillna(0)
    assets = col(bal, "TotalAssets")
    ocf = col(cf, "CashFlowsFromOperatingActivities")
    capex = col(cf, "PropertyAndPlantAndEquipment")  # reported as a negative outflow

    def ttm(s: pd.Series) -> pd.Series:
        return s.rolling(4, min_periods=4).sum()

    def avg_yoy(s: pd.Series) -> pd.Series:
        return (s + s.shift(4)) / 2

    m = pd.DataFrame(index=idx)
    m["revenue"] = revenue
    m["revenue_yoy"] = _ratio(revenue, revenue.shift(4)) - 1
    m["gross_margin"] = _ratio(col(inc, "GrossProfit"), revenue)
    m["operating_margin"] = _ratio(col(inc, "OperatingIncome"), revenue)
    m["net_margin"] = _ratio(net_income, revenue)
    m["eps"] = eps
    m["eps_yoy"] = _ratio(eps - eps.shift(4), eps.shift(4).abs())
    m["eps_ttm"] = ttm(eps)
    m["roe_ttm"] = _ratio(ttm(ni_parent), avg_yoy(equity_parent))
    m["roa_ttm"] = _ratio(ttm(net_income), avg_yoy(assets))
    m["debt_ratio"] = _ratio(col(bal, "Liabilities"), assets)
    m["current_ratio"] = _ratio(col(bal, "CurrentAssets"), col(bal, "CurrentLiabilities"))
    m["ocf"] = ocf
    m["capex"] = capex
    m["fcf"] = ocf + capex
    m["ocf_to_net_income_ttm"] = _ratio(ttm(ocf), ttm(net_income))
    return m


def monthly_revenue(d: StockData) -> pd.DataFrame:
    r = d.revenue.copy()
    if r.empty:
        return r
    r["yoy"] = _ratio(r["revenue"], r["revenue"].shift(12)) - 1
    r["mom"] = _ratio(r["revenue"], r["revenue"].shift(1)) - 1
    ytd = r["revenue"].groupby(r.index.year).cumsum()
    r["ytd_yoy"] = _ratio(ytd, ytd.shift(12)) - 1
    return r


def annual_dividends(d: StockData, q: pd.DataFrame) -> pd.DataFrame:
    """Dividends attributed to the fiscal year they were paid out of, with payout ratio."""
    dv = d.dividends
    out = pd.DataFrame(columns=["cash", "stock", "eps", "payout"])
    if dv.empty:
        return out
    fy = dv["year"].map(roc_fiscal_year)
    cash = col(dv, "CashEarningsDistribution").fillna(0) + col(dv, "CashStatutorySurplus").fillna(0)
    stock = col(dv, "StockEarningsDistribution").fillna(0) + col(dv, "StockStatutorySurplus").fillna(0)
    out = pd.DataFrame({"fy": fy, "cash": cash, "stock": stock}).dropna(subset=["fy"])
    out = out.groupby(out["fy"].astype(int))[["cash", "stock"]].sum()
    # Years before the fetch window may have only some of their payouts captured.
    if len(q.index):
        out = out[out.index >= q.index.min().year]

    eps_q = q["eps"].dropna()
    counts = eps_q.groupby(eps_q.index.year).count()
    annual_eps = eps_q.groupby(eps_q.index.year).sum()[counts == 4]
    out["eps"] = annual_eps.reindex(out.index)
    out["payout"] = _ratio(out["cash"], out["eps"])
    return out


def valuation_summary(d: StockData, years: int) -> dict:
    """Latest P/E, P/B, yield and where they sit within the last `years` of history."""
    s = {"years": years, "date": None, "close": np.nan}
    daily = d.daily
    if daily.empty:
        return s
    window = daily[daily.index >= daily.index.max() - pd.DateOffset(years=years)]
    last = daily.ffill().iloc[-1]
    s["date"] = daily.index.max().date()
    s["close"] = last.get("close", np.nan)
    for key in ("PER", "PBR", "dividend_yield"):
        if key not in window:
            continue
        hist = window[key].replace(0, np.nan).dropna()  # FinMind reports PER=0 when EPS<=0
        val = last.get(key, np.nan)
        s[key] = val if val else np.nan
        if not hist.empty and pd.notna(s[key]):
            s[f"{key}_pct"] = (hist < s[key]).mean()
            s[f"{key}_quantiles"] = hist.quantile([0.1, 0.5, 0.9]).tolist()
    return s


def checks(q: pd.DataFrame, rev: pd.DataFrame, divs: pd.DataFrame,
           financial: bool = False, lang: str = "en") -> list[tuple[bool, str]]:
    """Simple pass/fail heuristics. These are prompts for further reading, not verdicts.

    For financial companies the leverage, liquidity and cash-flow checks are skipped:
    high liabilities are their business model, and their operating cash flow mostly
    reflects deposit and loan movements rather than earnings quality.
    """
    from .i18n import translator
    tr = translator(lang)
    out: list[tuple[bool, str]] = []
    if financial:
        q = q.assign(**{c: np.nan for c in ["debt_ratio", "current_ratio", "ocf_to_net_income_ttm",
                                             "gross_margin", "fcf"]})
    last = q.dropna(subset=["revenue"]).iloc[-1] if q["revenue"].notna().any() else None
    if last is not None:
        if pd.notna(last["roe_ttm"]):
            out.append((last["roe_ttm"] >= 0.15, tr("ROE (TTM) {v:.1%} — threshold 15%", v=last["roe_ttm"])))
        if pd.notna(last["revenue_yoy"]):
            out.append((last["revenue_yoy"] > 0, tr("Latest quarter revenue YoY {v:+.1%}", v=last["revenue_yoy"])))
        if pd.notna(last["ocf_to_net_income_ttm"]):
            out.append((last["ocf_to_net_income_ttm"] >= 1,
                        tr("Operating cash flow / net income (TTM) {v:.2f}× — earnings backed by cash ≥ 1.0",
                           v=last["ocf_to_net_income_ttm"])))
        if pd.notna(last["debt_ratio"]):
            out.append((last["debt_ratio"] < 0.6, tr("Debt ratio {v:.1%} — threshold 60%", v=last["debt_ratio"])))
        if pd.notna(last["current_ratio"]):
            out.append((last["current_ratio"] >= 1.5, tr("Current ratio {v:.2f} — threshold 1.5", v=last["current_ratio"])))
    gm = q["gross_margin"].dropna()
    if len(gm) >= 5:
        out.append((gm.iloc[-1] >= gm.iloc[-5],
                    tr("Gross margin {v:.1%} vs {prev:.1%} a year earlier", v=gm.iloc[-1], prev=gm.iloc[-5])))
    fcf = q["fcf"].dropna().tail(4)
    if len(fcf) == 4:
        out.append((fcf.sum() > 0, tr("Free cash flow (TTM) NT$ {v:,.0f} 億", v=fcf.sum() / 1e8)))
    r = rev["ytd_yoy"].dropna() if "ytd_yoy" in rev else pd.Series(dtype=float)
    if not r.empty:
        out.append((r.iloc[-1] > 0, tr("Year-to-date monthly revenue YoY {v:+.1%} (through {month})", v=r.iloc[-1], month=r.index[-1])))
    complete = divs[divs["eps"].notna()] if not divs.empty else divs
    if len(complete):
        paid = int((complete["cash"] > 0).sum())
        out.append((paid == len(complete),
                    tr("Paid cash dividends in {paid} of {n} completed fiscal years", paid=paid, n=len(complete))))
    return out
