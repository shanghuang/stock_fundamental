"""Market-theme analysis (e.g. CPO): related stocks, theme news, and SWOT.

Without an API key everything here is data-driven: fundamentals from FinMind,
theme headlines filtered by keyword, and SWOT signals derived from the numbers.
With Claude enabled (see llm.py) the theme is researched on the web and each
stock gets a qualitative SWOT grounded in the same data. forecast.py adds a six-year
market outlook and forward P/E (sourced from Claude when enabled, extrapolated otherwise).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import analysis, forecast
from .finmind import FinMindError, fetch
from .i18n import pick, translator

PRESETS_FILE = Path(__file__).with_name("themes.json")
MAX_STOCKS = 20
# Supply-chain stages in flow order; "support" (test, equipment) sits beside the flow.
STAGES = ["upstream", "midstream", "downstream", "support"]


@dataclass
class Theme:
    key: str
    title: str
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    stock_ids: list[str] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)      # [{name, stage, description}] upstream first
    roles: dict[str, dict] = field(default_factory=dict)    # stock_id -> {segment, role}


@dataclass
class StockProfile:
    stock_id: str
    name: str = ""
    industry: str = ""
    is_financial: bool = False
    latest_quarter: str = ""
    metrics: dict = field(default_factory=dict)
    headlines: list[dict] = field(default_factory=list)
    signals: dict = field(default_factory=lambda: {"strengths": [], "weaknesses": [],
                                                   "opportunities": [], "threats": []})
    error: str | None = None
    segment: str = ""
    role: str = ""
    role_source: str = ""   # "preset", "Claude" or "industry" (fallback grouping)
    exposure: str = ""      # from Claude: direct / indirect / peripheral / unclear
    revenue_through: str = ""  # latest month of reported revenue, e.g. "2026-10"
    forward: dict = field(default_factory=dict)  # forecast.forward_pe() output
    price: dict = field(default_factory=dict)    # forecast.price_forecast() output


@dataclass
class ThemeResult:
    theme: Theme
    profiles: list[StockProfile]
    medians: dict
    news_days: int
    research: dict | None = None      # llm.ThemeResearch as dict
    swot: dict[str, dict] | None = None  # stock_id -> llm.StockSWOT as dict
    llm_error: str | None = None
    added_by_research: list[str] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)  # segment_summary() output, flow order
    lang: str = "en"
    market_forecast: dict | None = None  # forecast.from_claude() output (Claude research only)
    projection: dict | None = None       # forecast.extrapolate() output (tracked companies)
    forecast_error: str | None = None
    generated: str = ""  # ISO time the analysis ran


def load_presets(lang: str = "en") -> dict[str, Theme]:
    """Presets list segments upstream first; each segment maps stock codes to their role.

    Text fields are plain strings or {"en": ..., "zh-TW": ...}; they are resolved to `lang` here.
    """
    raw = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
    out = {}
    for k, v in raw.items():
        segments, roles = [], {}
        for seg in v.get("segments", []):
            name = pick(seg["name"], lang)
            segments.append({"name": name, "stage": seg.get("stage", "midstream"),
                             "description": pick(seg.get("description", ""), lang)})
            for sid, role in seg.get("stocks", {}).items():
                roles.setdefault(sid, {"segment": name, "role": pick(role, lang)})
        stocks = v.get("stocks") or list(roles)
        out[k] = Theme(k, pick(v["title"], lang), pick(v.get("description", ""), lang), v.get("keywords", []),
                       stocks, segments, roles)
    return out


def find_preset(name: str, lang: str = "en") -> Theme | None:
    want = name.strip().lower()
    if not want:
        return None
    presets = load_presets(lang)
    titles = {lg: load_presets(lg) for lg in ("en", "zh-TW")}
    for k, t in presets.items():
        if want == k.lower() or any(want == titles[lg][k].title.lower() for lg in titles):
            return t
    by_keyword = [t for t in presets.values() if want in (kw.lower() for kw in t.keywords)]
    return by_keyword[0] if len(by_keyword) == 1 else None


def suggest_stocks(terms: list[str], stocks: list[dict], limit: int = MAX_STOCKS) -> list[dict]:
    """Listed companies whose name or industry contains one of the terms (for themes with no stocks)."""
    terms = [t.strip().lower() for t in terms if len(t.strip()) >= 2]
    hits = [s for s in stocks if any(t in s["name"].lower() or t in s.get("industry", "").lower() for t in terms)]
    return hits[:limit]


def make_theme(name: str, keywords: list[str] | None = None, stock_ids: list[str] | None = None,
               lang: str = "en") -> Theme:
    """A preset with optional overrides, or a new ad-hoc theme.

    The name matches a preset by key or title (either language, case-insensitive), or by one of its
    keywords when only one preset has that keyword, so "DRAM" or "記憶體" opens the memory preset.
    """
    base = find_preset(name, lang)
    if base is None:
        base = Theme(key=name.strip(), title=name.strip(), keywords=[name.strip()])
    return Theme(
        key=base.key,
        title=base.title,
        description=base.description,
        keywords=keywords or base.keywords,
        stock_ids=list(dict.fromkeys(stock_ids or base.stock_ids))[:MAX_STOCKS],
        segments=[dict(s) for s in base.segments],
        roles=dict(base.roles),
    )


# --------------------------------------------------------------------------- data

def theme_headlines(stock_id: str, keywords: list[str], days: int) -> list[dict]:
    """Headlines from the last `days` days that mention any keyword (one request per day)."""
    if not keywords or days <= 0:
        return []
    pattern = re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE)
    out, seen = [], set()
    today = date.today()
    for i in range(days):
        day = today - timedelta(days=i)
        # A past day's news is final, so cache it for good; today's refreshes hourly.
        df = fetch("TaiwanStockNews", stock_id, day.isoformat(), max_age_hours=1 if i == 0 else 24 * 365)
        if df.empty:
            continue
        for row in df.itertuples():
            title = str(row.title)
            key = re.sub(r"\s*[-|].*$", "", title)  # same story syndicated by several sites
            if not pattern.search(title) or key in seen:
                continue
            seen.add(key)
            link = str(row.link) if str(row.link).startswith(("http://", "https://")) else ""
            out.append({"date": str(row.date)[:10], "title": title, "source": str(row.source), "link": link})
    return sorted(out, key=lambda h: h["date"], reverse=True)


def _window_sum(rev: pd.DataFrame, months: int) -> tuple[float, str]:
    """Sum of the latest `months` reported months and the latest month, e.g. (1.2e11, "2026-10")."""
    s = rev["revenue"] if "revenue" in rev else pd.Series(dtype=float)
    if not s.notna().any():
        return np.nan, ""
    s = s.loc[: s.last_valid_index()]
    recent = s.iloc[-months:]
    total = float(recent.sum()) if len(recent) == months and recent.notna().all() else np.nan
    return total, str(s.index[-1])


def _window_yoy(rev: pd.DataFrame, months: int) -> float:
    s = rev["revenue"] if "revenue" in rev else pd.Series(dtype=float)
    s = s.loc[: s.last_valid_index()] if s.notna().any() else s
    if len(s) < months + 12:
        return np.nan
    recent, prior = s.iloc[-months:], s.iloc[-months - 12:-12]
    if recent.isna().any() or prior.isna().any() or prior.sum() <= 0:
        return np.nan
    return recent.sum() / prior.sum() - 1


def profile_stock(stock_id: str, theme: Theme, news_days: int, refresh: bool = False) -> StockProfile:
    try:
        d = analysis.load(stock_id, years=5, refresh=refresh)
    except (FinMindError, OSError, ValueError) as e:
        return StockProfile(stock_id, error=str(e))
    p = profile_from_data(d)
    try:
        p.headlines = theme_headlines(stock_id, theme.keywords, news_days)
    except FinMindError as e:
        p.error = f"news unavailable: {e}"
    return p


def profile_from_data(d: analysis.StockData) -> StockProfile:
    """A stock's comparison metrics (no headlines), from 5 years of loaded data."""
    p = StockProfile(d.stock_id, d.name, d.industry, d.is_financial)

    q = analysis.quarterly_metrics(d)
    rev = analysis.monthly_revenue(d)
    val = analysis.valuation_summary(d, years=5)
    q_valid = q.dropna(subset=["revenue"])
    last = q_valid.iloc[-1] if len(q_valid) else pd.Series(dtype=float)
    year_ago = q_valid.iloc[-5] if len(q_valid) >= 5 else pd.Series(dtype=float)
    p.latest_quarter = str(q_valid.index[-1]) if len(q_valid) else ""

    def get(s: pd.Series, k: str) -> float:
        v = s.get(k, np.nan)
        return float(v) if pd.notna(v) else np.nan

    fcf = q["fcf"].dropna().tail(4)
    rev4 = q.loc[: q_valid.index[-1], "revenue"].tail(4) if len(q_valid) else pd.Series(dtype=float)
    revenue_12m, p.revenue_through = _window_sum(rev, 12)
    p.metrics = {
        "rev_3m_yoy": _window_yoy(rev, 3),
        "rev_12m_yoy": _window_yoy(rev, 12),
        "revenue_ttm": float(rev4.sum()) if len(rev4) == 4 and rev4.notna().all() else np.nan,
        "revenue_12m": revenue_12m,
        "revenue_yoy_q": get(last, "revenue_yoy"),
        "gross_margin": get(last, "gross_margin"),
        "gross_margin_chg": get(last, "gross_margin") - get(year_ago, "gross_margin"),
        "operating_margin": get(last, "operating_margin"),
        "net_margin": get(last, "net_margin"),
        "eps_ttm": get(last, "eps_ttm"),
        "roe_ttm": get(last, "roe_ttm"),
        "debt_ratio": get(last, "debt_ratio"),
        "current_ratio": get(last, "current_ratio"),
        "ocf_to_ni": get(last, "ocf_to_net_income_ttm"),
        "fcf_ttm": float(fcf.sum()) if len(fcf) == 4 else np.nan,
        "per": float(val.get("PER", np.nan)),
        "per_pct": float(val.get("PER_pct", np.nan)),
        # 25th, 50th and 75th percentile of the last 5 years' P/E: the band for implied prices.
        **dict(zip(("per_q25", "per_q50", "per_q75"), val.get("PER_quartiles") or [np.nan] * 3)),
        "pbr": float(val.get("PBR", np.nan)),
        "dividend_yield": float(val.get("dividend_yield", np.nan)),
        "close": float(val.get("close", np.nan)),
    }
    return p


# --------------------------------------------------------------------------- SWOT signals

def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and np.isnan(v))


def group_medians(profiles: list[StockProfile]) -> dict:
    keys = ["gross_margin", "operating_margin", "roe_ttm", "rev_12m_yoy", "per", "per_fwd"]
    out = {}
    for k in keys:
        vals = [p.metrics.get(k) for p in profiles if p.metrics]
        vals = [v for v in vals if _ok(v) and not (k in ("per", "per_fwd") and v <= 0)]
        out[k] = float(np.median(vals)) if vals else np.nan
    return out


def data_signals(p: StockProfile, med: dict, news_days: int, lang: str = "en") -> dict[str, list[str]]:
    """Rule-based SWOT signals. Every item cites the number behind it."""
    tr = translator(lang)
    m = p.metrics
    s, w, o, t = [], [], [], []
    roe, gm, nm = m.get("roe_ttm"), m.get("gross_margin"), m.get("net_margin")

    if _ok(roe) and roe >= 0.15:
        s.append(tr("High ROE (TTM) {v:.1%}", v=roe))
    elif _ok(roe) and roe < 0.08:
        w.append(tr("Low ROE (TTM) {v:.1%}", v=roe))
    if not p.is_financial and _ok(gm) and _ok(med.get("gross_margin")):
        if gm >= med["gross_margin"]:
            s.append(tr("Gross margin {v:.1%} at or above the group median {med:.1%}", v=gm, med=med["gross_margin"]))
        else:
            w.append(tr("Gross margin {v:.1%} below the group median {med:.1%}", v=gm, med=med["gross_margin"]))
    if _ok(nm) and nm < 0:
        w.append(tr("Loss-making: net margin {v:.1%}", v=nm))
    r12, r3 = m.get("rev_12m_yoy"), m.get("rev_3m_yoy")
    if _ok(r12) and r12 >= 0.2:
        s.append(tr("Revenue up {v:+.1%} over the last 12 months", v=r12))
    elif _ok(r12) and r12 < 0:
        w.append(tr("Revenue down {v:+.1%} over the last 12 months", v=r12))
    if not p.is_financial:
        ocf = m.get("ocf_to_ni")
        if _ok(ocf) and ocf >= 1 and _ok(nm) and nm > 0:
            s.append(tr("Earnings backed by cash: operating cash flow {v:.2f}× net income", v=ocf))
        fcf = m.get("fcf_ttm")
        if _ok(fcf) and fcf < 0:
            w.append(tr("Negative free cash flow (TTM) NT$ {v:,.0f} 億", v=fcf / 1e8))
        debt, cr = m.get("debt_ratio"), m.get("current_ratio")
        if _ok(debt) and debt < 0.4:
            s.append(tr("Conservative balance sheet: debt ratio {v:.1%}", v=debt))
        elif _ok(debt) and debt > 0.6:
            w.append(tr("High leverage: debt ratio {v:.1%}", v=debt))
        if _ok(cr) and cr < 1:
            w.append(tr("Current ratio {v:.2f} below 1", v=cr))

    if _ok(r3) and _ok(r12):
        if r3 > r12 + 0.05:
            o.append(tr("Revenue accelerating: last 3 months {r3:+.1%} YoY vs {r12:+.1%} over 12 months", r3=r3, r12=r12))
        elif r3 < r12 - 0.05:
            t.append(tr("Revenue decelerating: last 3 months {r3:+.1%} YoY vs {r12:+.1%} over 12 months", r3=r3, r12=r12))
    gmc = m.get("gross_margin_chg")
    if not p.is_financial and _ok(gmc):
        if gmc > 0.01:
            o.append(tr("Gross margin expanding: {v:+.1f} pts year over year", v=gmc * 100))
        elif gmc < -0.01:
            t.append(tr("Gross margin compressing: {v:+.1f} pts year over year", v=gmc * 100))
    if p.headlines:
        o.append(tr("{n} theme-related headline(s) in the last {days} day(s)", n=len(p.headlines), days=news_days))
    pct, per = m.get("per_pct"), m.get("per")
    if _ok(pct) and pct >= 0.8:
        t.append(tr("P/E {per:.1f}× is above {pct:.0%} of its 5-year history; expectations are priced in", per=per, pct=pct))
    elif _ok(pct) and pct <= 0.3:
        o.append(tr("P/E {per:.1f}× is in the bottom {pct:.0%} of its 5-year history", per=per, pct=pct))
    if _ok(per) and per > 0 and _ok(med.get("per")) and per > 1.5 * med["per"]:
        t.append(tr("P/E {per:.1f}× is a premium to the group median {med:.1f}×", per=per, med=med["per"]))
    eps = m.get("eps_ttm")
    if _ok(eps) and eps <= 0:
        t.append(tr("No earnings support for valuation: EPS (TTM) NT$ {v:.2f}", v=eps))
    fwd = m.get("per_fwd")
    if _ok(fwd) and fwd > 0 and _ok(per) and per > 0:
        if fwd < 0.8 * per:
            o.append(tr("Forward P/E {fwd:.1f}× vs {per:.1f}× trailing: earnings expected to grow", fwd=fwd, per=per))
        elif fwd > 1.2 * per:
            t.append(tr("Forward P/E {fwd:.1f}× vs {per:.1f}× trailing: earnings expected to fall", fwd=fwd, per=per))
    return {"strengths": s, "weaknesses": w, "opportunities": o, "threats": t}


# --------------------------------------------------------------------------- supply chain

def _add_segment(segments: list[dict], seg: dict, stage: str | None = None) -> None:
    name = str(seg.get("name", "")).strip()
    if not name or any(s["name"] == name for s in segments):
        return
    stage = stage or seg.get("stage")
    segments.append({"name": name, "stage": stage if stage in STAGES + ["unmapped"] else "midstream",
                     "description": seg.get("description", "")})


def assign_roles(theme: Theme, profiles: list[StockProfile], research: dict | None = None,
                 lang: str = "en") -> list[dict]:
    """Give each profile a supply-chain segment and role; return the segments in flow order.

    Priority: Claude's research (when present), then the preset, then the stock's
    FinMind industry as a coarse fallback so unmapped stocks are still grouped.
    """
    segments: list[dict] = []
    claude = {}
    if research:
        for seg in research.get("segments", []):
            _add_segment(segments, seg)
        claude = {r["stock_id"]: r for r in research.get("roles", []) if r.get("stock_id") and r.get("segment")}
    for seg in theme.segments:
        _add_segment(segments, seg)

    for p in profiles:
        c, preset = claude.get(p.stock_id), theme.roles.get(p.stock_id)
        if c:
            p.segment, p.role, p.role_source = c["segment"].strip(), c.get("role", ""), "Claude"
            p.exposure = c.get("exposure", "")
            _add_segment(segments, {"name": p.segment})  # a segment Claude used but did not list
        elif preset:
            p.segment, p.role, p.role_source = preset["segment"], preset["role"], "preset"
        elif p.industry:
            tr = translator(lang)
            p.segment = tr("Industry: {name}", name=p.industry.split("/")[0].strip())
            p.role_source = "industry"
            _add_segment(segments, {"name": p.segment}, stage="unmapped")
    order = STAGES + ["unmapped"]
    return sorted(segments, key=lambda s: order.index(s["stage"]))


def segment_summary(segments: list[dict], profiles: list[StockProfile]) -> list[dict]:
    """Per-segment medians and totals. Segments without a tracked company are kept (coverage gaps)."""
    out = []
    for seg in segments:
        members = [p for p in profiles if p.metrics and p.segment == seg["name"]]

        def med(k: str) -> float:
            vals = [p.metrics.get(k) for p in members]
            vals = [v for v in vals if _ok(v) and not (k == "per" and v <= 0)]
            return float(np.median(vals)) if vals else np.nan

        revs = [p.metrics.get("revenue_ttm") for p in members if _ok(p.metrics.get("revenue_ttm"))]
        out.append({**seg, "stock_ids": [p.stock_id for p in members],
                    **{k: med(k) for k in ["rev_3m_yoy", "rev_12m_yoy", "gross_margin", "operating_margin",
                                           "roe_ttm", "per"]},
                    "revenue_ttm": float(sum(revs)) if revs else np.nan,
                    "headlines": sum(len(p.headlines) for p in members)})
    return out


def segment_signals(p: StockProfile, peers: list[StockProfile], lang: str = "en") -> dict[str, list[str]]:
    """Leader/laggard within its supply-chain segment (needs at least 3 comparable companies)."""
    tr = translator(lang)
    out = {"strengths": [], "weaknesses": [], "opportunities": [], "threats": []}
    if p.role_source == "industry":
        return out
    checks = [("rev_3m_yoy", "opportunities", "threats", True,
               tr("Highest revenue growth over the last 3 months in the “{segment}” segment: {v} ({n} companies)"),
               tr("Lowest revenue growth over the last 3 months in the “{segment}” segment: {v} ({n} companies)"))]
    if not p.is_financial:
        checks.insert(0, ("gross_margin", "strengths", "weaknesses", False,
                          tr("Highest gross margin in the “{segment}” segment: {v} ({n} companies)"),
                          tr("Lowest gross margin in the “{segment}” segment: {v} ({n} companies)")))
    for key, good, bad, signed, hi_text, lo_text in checks:
        vals = [(q.metrics[key], q.stock_id) for q in peers if _ok(q.metrics.get(key))]
        v = p.metrics.get(key)
        if len(vals) < 3 or not _ok(v):
            continue
        text = f"{v:+.1%}" if signed else f"{v:.1%}"
        if max(vals)[1] == p.stock_id:
            out[good].append(hi_text.format(segment=p.segment, v=text, n=len(vals)))
        elif min(vals)[1] == p.stock_id:
            out[bad].append(lo_text.format(segment=p.segment, v=text, n=len(vals)))
    return out


# --------------------------------------------------------------------------- orchestration

def analyze(theme: Theme, news_days: int = 3, use_llm: bool = False, refresh: bool = False,
            workers: int = 4, lang: str = "en") -> ThemeResult:
    research, research_error, added = None, None, []
    if use_llm:
        from . import llm
        try:
            research = llm.research_theme(theme, refresh=refresh, lang=lang)
            known = set(theme.stock_ids)
            valid = {s["id"] for s in _stock_list()}
            for c in research.get("candidates", []):
                sid = c.get("stock_id", "")
                if sid in valid and sid not in known and len(theme.stock_ids) < MAX_STOCKS:
                    theme.stock_ids.append(sid)
                    known.add(sid)
                    added.append(sid)
        except llm.LLMError as e:
            research_error = str(e)

    with ThreadPoolExecutor(max_workers=workers + 1) as pool:
        # The market forecast is researched while the stocks load; it only needs the segment names.
        forecast_job = None
        if research is not None:
            from . import llm
            seg_names = [s["name"] for s in research.get("segments", [])] or [s["name"] for s in theme.segments]
            forecast_job = pool.submit(llm.forecast_market, theme, seg_names, refresh=refresh, lang=lang)
        profiles = list(pool.map(lambda sid: profile_stock(sid, theme, news_days, refresh), theme.stock_ids))
        market_forecast, forecast_error = None, None
        if forecast_job is not None:
            try:
                market_forecast = forecast.from_claude(forecast_job.result(), theme.stock_ids)
                if market_forecast is None:
                    forecast_error = "Claude found no usable market-size figures"
            except llm.LLMError as e:
                forecast_error = str(e)
    segments = assign_roles(theme, profiles, research, lang)
    ok = [p for p in profiles if p.metrics]
    estimates = (market_forecast or {}).get("eps_estimates")
    for p in ok:
        p.forward = forecast.forward_pe(p, estimates)
        p.metrics["eps_fwd"], p.metrics["per_fwd"] = p.forward["eps"], p.forward["per"]
    med = group_medians(ok)
    for p in ok:
        p.signals = data_signals(p, med, news_days, lang)
        for k, items in segment_signals(p, [q for q in ok if q.segment == p.segment], lang).items():
            p.signals[k].extend(items)

    seg_summary = segment_summary(segments, profiles)
    projection = forecast.extrapolate(ok, seg_summary)
    by_year = (market_forecast or {}).get("eps_by_year", {})
    for p in ok:
        p.price = forecast.price_forecast(p, by_year.get(p.stock_id), end_growth(p, market_forecast, projection),
                                          med.get("per"))
    result = ThemeResult(theme, profiles, med, news_days, research=research,
                         llm_error=research_error, added_by_research=added,
                         segments=seg_summary, lang=lang, market_forecast=market_forecast,
                         projection=projection, forecast_error=forecast_error,
                         generated=datetime.now().isoformat(timespec="minutes"))
    if use_llm and research is not None and ok:
        from . import llm
        try:
            result.swot = llm.swot_analysis(theme, research, ok, med, refresh=refresh, lang=lang,
                                            market_forecast=market_forecast)
        except llm.LLMError as e:
            result.llm_error = str(e)
    return result


def _stock_list() -> list[dict]:
    from .pipeline import list_stocks
    return list_stocks()


def end_growth(p: StockProfile, market_forecast: dict | None, projection: dict | None) -> float:
    """Growth an EPS path fades to: the stock's segment outlook, else the theme's, else long-run growth."""
    for outlook in (market_forecast, projection):
        for seg in (outlook or {}).get("segments", []):
            if p.segment and seg["segment"] == p.segment and _ok(seg.get("cagr")):
                return seg["cagr"]
    for outlook in (market_forecast, projection):
        if outlook and _ok(outlook.get("cagr")):
            return outlook["cagr"]
    return forecast.LONG_RUN_GROWTH


def profile_for_prompt(p: StockProfile) -> dict:
    """Compact, JSON-safe view of a profile for the LLM prompt."""
    def clean(v):
        if isinstance(v, float):
            return None if np.isnan(v) else round(v, 4)
        return v
    d = asdict(p)
    d["metrics"] = {k: clean(v) for k, v in p.metrics.items()}
    d["forward"] = {k: clean(v) for k, v in p.forward.items()}
    d.pop("price", None)  # computed after the SWOT prompt's inputs; not needed there
    d["headlines"] = [{"date": h["date"], "title": h["title"]} for h in p.headlines[:10]]
    d.pop("error", None)
    return d


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m tw_fundamental.theme CPO [--stocks 3363,3081] [--keywords CPO,矽光子] [--llm]"""
    import argparse
    import sys
    import webbrowser

    from . import llm, theme_report, theme_store
    from .i18n import LANGS
    from .pipeline import STOCK_ID_RE

    presets = load_presets()
    ap = argparse.ArgumentParser(description="Theme analysis and SWOT for Taiwan stocks.")
    ap.add_argument("theme", nargs="?", help=f"preset ({', '.join(presets)}) or any theme name")
    ap.add_argument("--stocks", default="", help="comma-separated stock codes (replaces the preset list)")
    ap.add_argument("--keywords", default="", help="comma-separated news keywords (replaces the preset)")
    ap.add_argument("--days", type=int, default=3, help="days of news to scan (default 3, max 14)")
    ap.add_argument("--llm", action="store_true", help="use Claude for web research and SWOT")
    ap.add_argument("--refresh", action="store_true", help="ignore caches")
    ap.add_argument("--lang", choices=list(LANGS), default="en", help="report language (default en)")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--list", action="store_true", help="list saved analyses and exit")
    args = ap.parse_args(argv)

    if args.list:
        for m in theme_store.list_saved():
            cagr = "" if m.get("cagr") is None else f"  CAGR {m['cagr']:+.1%} ({m['cagr_source']})"
            print(f"{m['created'].replace('T', ' ')}  {m['id']}  {m['title']} [{m['lang']}]  "
                  f"{m['stocks']} stocks, {m['news_days']}d news{', Claude' if m['claude'] else ''}{cagr}")
        return 0
    if not args.theme:
        ap.error("a theme is required unless --list is given")

    stocks = [s for s in re.split(r"[,\s]+", args.stocks) if s]
    bad = [s for s in stocks if not STOCK_ID_RE.match(s)]
    if bad:
        print(f"error: invalid stock code(s): {', '.join(bad)}", file=sys.stderr)
        return 2
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    theme = make_theme(args.theme, keywords or None, stocks or None, lang=args.lang)
    if not theme.stock_ids and not args.llm:
        print("error: no stocks for this theme; pass --stocks or --llm to discover them", file=sys.stderr)
        return 2
    if args.llm and not llm.available():
        print("warning: no Claude credentials found (ANTHROPIC_API_KEY); continuing without --llm", file=sys.stderr)
        args.llm = False

    print(f"Analyzing {theme.title}: {len(theme.stock_ids)} stocks, {min(args.days, 14)} day(s) of news…")
    days = min(args.days, 14)
    key = theme_store.params_key(theme, days, args.llm, args.lang)
    query = {"name": theme.key, "days": days, "llm": "1" if args.llm else ""}
    if keywords:
        query["keywords"] = ", ".join(keywords)
    if stocks:
        query["stocks"] = " ".join(stocks)
    r = analyze(theme, news_days=days, use_llm=args.llm, refresh=args.refresh, lang=args.lang)
    print(f"saved as {theme_store.save(r, key, query)} (see --list, or Saved analyses in the web UI)")
    if r.llm_error:
        print(f"  Claude: {r.llm_error}", file=sys.stderr)
    if r.forecast_error:
        print(f"  Claude forecast: {r.forecast_error}", file=sys.stderr)
    for label, f in (("market forecast", r.market_forecast), ("tracked-revenue projection", r.projection)):
        if f:
            print(f"  {label}: {f['points'][0]['year']}–{f['points'][-1]['year']} CAGR {f['cagr']:+.1%} ({f['unit']})")
    for p in r.profiles:
        if not p.metrics:
            print(f"  {p.stock_id}: skipped ({p.error})")
            continue
        s = p.signals
        fwd = p.forward.get("per")
        fwd_text = f", fwd P/E {fwd:.1f}× ({p.forward['basis']})" if forecast._ok(fwd) else ""
        pf = p.price or {}
        if pf.get("years") and forecast._ok(pf["years"][-1]["base"]):
            last = pf["years"][-1]
            fwd_text += (f", FY{last['year']} implied NT$ {last['low']:,.0f}–{last['high']:,.0f} "
                         f"(base {last['base']:,.0f}, {pf['upside']:+.0%} vs close)")
        print(f"  {p.stock_id} {p.name} [{p.segment or 'unmapped'}]: S{len(s['strengths'])} W{len(s['weaknesses'])} "
              f"O{len(s['opportunities'])} T{len(s['threats'])}, {len(p.headlines)} headline(s){fwd_text}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^0-9A-Za-z_-]+", "_", theme.key).strip("_") or "theme"
    suffix = "" if args.lang == "en" else f".{args.lang}"
    path = out / f"theme_{slug}{suffix}.html"
    path.write_text(theme_report.render(r, link=lambda sid: f"{sid}{suffix}.html"), encoding="utf-8")
    print(f"report: {path.resolve()}")
    if args.open:
        webbrowser.open(path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
