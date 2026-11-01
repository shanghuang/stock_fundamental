"""Market-theme analysis (e.g. CPO): related stocks, theme news, and SWOT.

Without an API key everything here is data-driven: fundamentals from FinMind,
theme headlines filtered by keyword, and SWOT signals derived from the numbers.
With Claude enabled (see llm.py) the theme is researched on the web and each
stock gets a qualitative SWOT grounded in the same data.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import analysis
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


def make_theme(name: str, keywords: list[str] | None = None, stock_ids: list[str] | None = None,
               lang: str = "en") -> Theme:
    """A preset (case-insensitive key match) with optional overrides, or a new ad-hoc theme."""
    presets = {k.lower(): t for k, t in load_presets(lang).items()}
    base = presets.get(name.strip().lower())
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
    p = StockProfile(stock_id)
    try:
        d = analysis.load(stock_id, years=5, refresh=refresh)
    except (FinMindError, OSError, ValueError) as e:
        p.error = str(e)
        return p
    p.name, p.industry, p.is_financial = d.name, d.industry, d.is_financial

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
    p.metrics = {
        "rev_3m_yoy": _window_yoy(rev, 3),
        "rev_12m_yoy": _window_yoy(rev, 12),
        "revenue_ttm": float(rev4.sum()) if len(rev4) == 4 and rev4.notna().all() else np.nan,
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
        "pbr": float(val.get("PBR", np.nan)),
        "dividend_yield": float(val.get("dividend_yield", np.nan)),
        "close": float(val.get("close", np.nan)),
    }
    try:
        p.headlines = theme_headlines(stock_id, theme.keywords, news_days)
    except FinMindError as e:
        p.error = f"news unavailable: {e}"
    return p


# --------------------------------------------------------------------------- SWOT signals

def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and np.isnan(v))


def group_medians(profiles: list[StockProfile]) -> dict:
    keys = ["gross_margin", "operating_margin", "roe_ttm", "rev_12m_yoy", "per"]
    out = {}
    for k in keys:
        vals = [p.metrics.get(k) for p in profiles if p.metrics]
        vals = [v for v in vals if _ok(v) and not (k == "per" and v <= 0)]
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

    with ThreadPoolExecutor(max_workers=workers) as pool:
        profiles = list(pool.map(lambda sid: profile_stock(sid, theme, news_days, refresh), theme.stock_ids))
    segments = assign_roles(theme, profiles, research, lang)
    ok = [p for p in profiles if p.metrics]
    med = group_medians(ok)
    for p in ok:
        p.signals = data_signals(p, med, news_days, lang)
        for k, items in segment_signals(p, [q for q in ok if q.segment == p.segment], lang).items():
            p.signals[k].extend(items)

    result = ThemeResult(theme, profiles, med, news_days, research=research,
                         llm_error=research_error, added_by_research=added,
                         segments=segment_summary(segments, profiles), lang=lang)
    if use_llm and research is not None and ok:
        from . import llm
        try:
            result.swot = llm.swot_analysis(theme, research, ok, med, refresh=refresh, lang=lang)
        except llm.LLMError as e:
            result.llm_error = str(e)
    return result


def _stock_list() -> list[dict]:
    from .pipeline import list_stocks
    return list_stocks()


def profile_for_prompt(p: StockProfile) -> dict:
    """Compact, JSON-safe view of a profile for the LLM prompt."""
    def clean(v):
        if isinstance(v, float):
            return None if np.isnan(v) else round(v, 4)
        return v
    d = asdict(p)
    d["metrics"] = {k: clean(v) for k, v in p.metrics.items()}
    d["headlines"] = [{"date": h["date"], "title": h["title"]} for h in p.headlines[:10]]
    d.pop("error", None)
    return d


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m tw_fundamental.theme CPO [--stocks 3363,3081] [--keywords CPO,矽光子] [--llm]"""
    import argparse
    import sys
    import webbrowser

    from . import llm, theme_report
    from .i18n import LANGS
    from .pipeline import STOCK_ID_RE

    presets = load_presets()
    ap = argparse.ArgumentParser(description="Theme analysis and SWOT for Taiwan stocks.")
    ap.add_argument("theme", help=f"preset ({', '.join(presets)}) or any theme name")
    ap.add_argument("--stocks", default="", help="comma-separated stock codes (replaces the preset list)")
    ap.add_argument("--keywords", default="", help="comma-separated news keywords (replaces the preset)")
    ap.add_argument("--days", type=int, default=3, help="days of news to scan (default 3, max 14)")
    ap.add_argument("--llm", action="store_true", help="use Claude for web research and SWOT")
    ap.add_argument("--refresh", action="store_true", help="ignore caches")
    ap.add_argument("--lang", choices=list(LANGS), default="en", help="report language (default en)")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args(argv)

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
    r = analyze(theme, news_days=min(args.days, 14), use_llm=args.llm, refresh=args.refresh, lang=args.lang)
    if r.llm_error:
        print(f"  Claude: {r.llm_error}", file=sys.stderr)
    for p in r.profiles:
        if not p.metrics:
            print(f"  {p.stock_id}: skipped ({p.error})")
            continue
        s = p.signals
        print(f"  {p.stock_id} {p.name} [{p.segment or 'unmapped'}]: S{len(s['strengths'])} W{len(s['weaknesses'])} "
              f"O{len(s['opportunities'])} T{len(s['threats'])}, {len(p.headlines)} headline(s)")

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
