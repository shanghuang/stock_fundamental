"""Six-year market outlook, forward P/E and implied share prices for a theme.

Two sources, labelled as such in the report:
- Claude (web research, optional): published market-size forecasts triangulated into
  low/base/high paths, growth by supply-chain segment, and analysts' EPS estimates.
- Extrapolation (always available): the tracked companies' combined revenue, projected with
  growth that fades from its recent rate to a long-run rate. It describes those companies,
  not the whole market, and is a mechanical projection rather than a forecast.

Implied prices (price_forecast) multiply an EPS path for the next PRICE_YEARS fiscal years by
the stock's own 5-year P/E band. They show what the price would be if earnings follow the path
and the market keeps valuing the stock as it has; they are not price targets.
"""
from __future__ import annotations

from datetime import date

import numpy as np

HORIZON = 6                 # forecast years after the base year
LONG_RUN_GROWTH = 0.05      # nominal growth that the projection fades to by the last year
GROWTH_CLIP = (-0.3, 0.6)   # recent growth is clipped to this range before projecting
FWD_GROWTH_CLIP = (-0.5, 1.0)
PRICE_YEARS = 3             # fiscal years of EPS behind the implied prices
PE_HIGH_CAP = 2.0            # the high P/E is at most this multiple of the median
END_GROWTH_CLIP = (0.0, 0.15)  # yearly EPS growth after year 1 (segment or theme outlook)


def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and np.isnan(v))


def _clip(v: float, bounds: tuple[float, float]) -> float:
    return float(min(max(v, bounds[0]), bounds[1]))


def cagr(first: float, last: float, years: int) -> float:
    if not (_ok(first) and _ok(last)) or first <= 0 or last <= 0 or years <= 0:
        return np.nan
    return (last / first) ** (1 / years) - 1


def fade_path(start: float, g0: float, g_end: float, years: int = HORIZON) -> list[float]:
    """Sizes for years 0..years; growth moves linearly from g0 in year 1 to g_end in the last year."""
    sizes = [start]
    for t in range(1, years + 1):
        g = g0 + (g_end - g0) * (t - 1) / (years - 1) if years > 1 else g0
        sizes.append(sizes[-1] * (1 + g))
    return sizes


def recent_growth(profiles) -> tuple[float, float, int]:
    """Combined revenue of the last 12 months, its growth vs the 12 months before, and the company count."""
    cur = prior = 0.0
    n = 0
    for p in profiles:
        r, g = p.metrics.get("revenue_12m"), p.metrics.get("rev_12m_yoy")
        if _ok(r) and _ok(g) and g > -1 and r > 0:
            cur, prior, n = cur + r, prior + r / (1 + g), n + 1
    if prior <= 0:
        return np.nan, np.nan, 0
    return cur, cur / prior - 1, n


def extrapolate(profiles, segments: list[dict]) -> dict | None:
    """Project the tracked companies' combined revenue (NT$ 億) for HORIZON years."""
    total, g, n = recent_growth(profiles)
    if not _ok(g):
        return None
    through = max((p.revenue_through for p in profiles if p.revenue_through), default="")
    base_year = int(through[:4]) if through else date.today().year
    g0 = _clip(g, GROWTH_CLIP)
    spread = max(0.05, abs(g0 - LONG_RUN_GROWTH) / 2)
    paths = {"low": fade_path(total, g0 - spread, LONG_RUN_GROWTH - 0.02),
             "base": fade_path(total, g0, LONG_RUN_GROWTH),
             "high": fade_path(total, g0 + spread, LONG_RUN_GROWTH + 0.02)}
    points = [{"year": base_year + i, **{k: paths[k][i] / 1e8 for k in paths}} for i in range(HORIZON + 1)]

    revs = [(p.metrics["revenue_12m"], p) for p in profiles
            if _ok(p.metrics.get("revenue_12m")) and _ok(p.metrics.get("rev_12m_yoy"))]
    top_rev, top = max(revs, key=lambda t: t[0])
    largest = {"stock_id": top.stock_id, "label": f"{top.stock_id} {top.name}".strip(), "share": top_rev / total}
    seg_rows = []
    for seg in segments:
        members = [p for p in profiles if p.stock_id in seg.get("stock_ids", [])]
        _, sg, sn = recent_growth(members)
        if _ok(sg):
            path = fade_path(1.0, _clip(sg, GROWTH_CLIP), LONG_RUN_GROWTH)
            seg_rows.append({"segment": seg["name"], "recent": sg, "cagr": cagr(path[0], path[-1], HORIZON),
                             "companies": sn})
    return {"source": "extrapolated", "unit": "NT$ 億", "base_year": base_year, "through": through,
            "points": points, "cagr": cagr(points[0]["base"], points[-1]["base"], HORIZON),
            "cagr_low": cagr(points[0]["low"], points[-1]["low"], HORIZON),
            "cagr_high": cagr(points[0]["high"], points[-1]["high"], HORIZON),
            "recent_growth": g, "start_growth": g0, "long_run_growth": LONG_RUN_GROWTH, "spread": spread,
            "companies": n, "largest": largest, "segments": seg_rows}


def from_claude(raw: dict, stock_ids: list[str], base_year: int | None = None) -> dict | None:
    """Validate Claude's structured forecast: keep the horizon's years, order low ≤ base ≤ high."""
    base_year = base_year or date.today().year
    years = range(base_year, base_year + HORIZON + 1)
    by_year = {}
    for pt in raw.get("points", []):
        vals = sorted(float(pt[k]) for k in ("low", "base", "high"))
        if pt.get("year") in years and vals[0] > 0:
            by_year[pt["year"]] = {"year": pt["year"], "low": vals[0], "base": vals[1], "high": vals[2]}
    points = [by_year[y] for y in sorted(by_year)]
    if len(points) < 2:
        return None
    span = points[-1]["year"] - points[0]["year"]

    estimates: dict[str, dict] = {}
    by_year: dict[str, dict[int, dict]] = {}
    for e in raw.get("eps_estimates", []):
        sid, fy, eps = e.get("stock_id"), e.get("fiscal_year"), e.get("eps")
        if sid not in stock_ids or not isinstance(fy, int) or fy < base_year or not _ok(eps) or eps <= 0:
            continue
        if fy <= base_year + PRICE_YEARS:
            by_year.setdefault(sid, {})[fy] = {"eps": float(eps), "source": e.get("source", "")}
        # Prefer next year's estimate; it is the usual basis for a forward P/E.
        cur = estimates.get(sid)
        if cur is None or abs(fy - base_year - 1) < abs(cur["fiscal_year"] - base_year - 1):
            estimates[sid] = {"fiscal_year": fy, "eps": float(eps), "source": e.get("source", "")}
    return {"source": "claude", "market": raw.get("market", ""), "unit": raw.get("unit", ""),
            "base_year": points[0]["year"], "points": points,
            "cagr": cagr(points[0]["base"], points[-1]["base"], span),
            "cagr_low": cagr(points[0]["low"], points[-1]["low"], span),
            "cagr_high": cagr(points[0]["high"], points[-1]["high"], span),
            "segments": [{"segment": s["segment"], "cagr": float(s["cagr"]), "note": s.get("note", "")}
                         for s in raw.get("segments", []) if _ok(s.get("cagr"))],
            "published": raw.get("published", []), "assumptions": raw.get("assumptions", []),
            "eps_estimates": estimates, "eps_by_year": by_year, "sources": raw.get("sources", [])}


def forward_pe(p, estimates: dict[str, dict] | None = None) -> dict:
    """Forward EPS and P/E: analysts' estimate when Claude found one, else a trailing extrapolation.

    The extrapolation assumes next-12-month EPS grows with recent revenue (last 3 months YoY,
    falling back to 12 months) at a constant net margin, so it ignores operating leverage.
    """
    m = p.metrics
    close, eps_ttm = m.get("close"), m.get("eps_ttm")
    out = {"eps": np.nan, "per": np.nan, "growth": np.nan, "basis": "none", "fiscal_year": None, "source": ""}
    est = (estimates or {}).get(p.stock_id)
    if est and _ok(close):
        out.update(eps=est["eps"], per=close / est["eps"], basis="analyst", fiscal_year=est["fiscal_year"],
                   source=est.get("source", ""))
        if _ok(eps_ttm) and eps_ttm > 0:
            out["growth"] = est["eps"] / eps_ttm - 1
        return out
    g = m.get("rev_3m_yoy") if _ok(m.get("rev_3m_yoy")) else m.get("rev_12m_yoy")
    if p.is_financial or not (_ok(close) and _ok(eps_ttm) and _ok(g)) or eps_ttm <= 0:
        return out
    g = _clip(g, FWD_GROWTH_CLIP)
    eps = eps_ttm * (1 + g)
    out.update(eps=eps, per=close / eps if eps > 0 else np.nan, growth=g, basis="extrapolated")
    return out


def eps_path(p, estimates: dict[int, dict] | None, end_growth: float, years: int = PRICE_YEARS,
             momentum: bool = True) -> list[dict]:
    """EPS for the `years` fiscal years after the latest reported one.

    Each year uses the analysts' estimate when there is one. Otherwise year 1 is the forward EPS
    extrapolated from recent revenue (when `momentum`), and every other year grows from the year
    before at `end_growth`, the outlook for the company's segment or theme. Empty when there is no
    positive starting EPS.
    """
    estimates = {int(k): v for k, v in (estimates or {}).items()}  # JSON round trips make keys strings
    base = int(p.latest_quarter[:4]) if p.latest_quarter[:4].isdigit() else date.today().year
    fwd = p.forward or {}
    prev = estimates.get(base, {}).get("eps", p.metrics.get("eps_ttm"))
    if not _ok(prev) or prev <= 0:
        return []
    out = []
    for t in range(1, years + 1):
        year = base + t
        est = estimates.get(year)
        if est:
            eps, source, ref = est["eps"], "analyst", est.get("source", "")
        elif t == 1 and momentum and fwd.get("basis") == "extrapolated":
            eps, source, ref = fwd["eps"], "extrapolated", ""
        else:
            eps, source, ref = prev * (1 + end_growth), "projected", ""
        out.append({"year": year, "eps": float(eps), "source": source, "ref": ref, "growth": eps / prev - 1})
        prev = eps
    return out


def pe_band(p, group_pe: float | None = None) -> dict | None:
    """Low / base / high P/E for implied prices.

    Normally the stock's 5-year 25th percentile, median and 75th percentile ("history"), with the
    high end capped at PE_HIGH_CAP × the median: P/E spikes when earnings briefly collapse. When the
    stock trades below its 25th percentile, the market is pricing in lower earnings, often a
    cyclical peak; the band then runs from today's P/E (low and base) to the 25th percentile
    ("below"). Without P/E history, the group median ("group").
    """
    m = p.metrics
    q = [m.get(k) for k in ("per_q25", "per_q50", "per_q75")]
    cur = m.get("per")
    if all(_ok(v) and v > 0 for v in q):
        if _ok(cur) and 0 < cur < q[0]:
            return {"low": float(cur), "base": float(cur), "high": float(q[0]), "basis": "below"}
        return {"low": float(q[0]), "base": float(q[1]), "high": float(min(q[2], PE_HIGH_CAP * q[1])),
                "basis": "history"}
    if _ok(group_pe) and group_pe > 0:
        return {"low": float(group_pe), "base": float(group_pe), "high": float(group_pe), "basis": "group"}
    return None


def price_forecast(p, estimates: dict[int, dict] | None = None, end_growth: float = LONG_RUN_GROWTH,
                   group_pe: float | None = None, today: date | None = None) -> dict:
    """Implied share prices for the next PRICE_YEARS fiscal years: EPS path × P/E band.

    FinMind's P/E is trailing (price ÷ last four quarters' EPS), so a fiscal year's EPS times that
    multiple is a price at the end of that year. Prices exclude dividends. A stock trading below
    its usual P/E band gets no revenue momentum in its EPS path, and `peak` is set.
    """
    today = today or date.today()
    close = p.metrics.get("close")
    end_growth = _clip(end_growth if _ok(end_growth) else LONG_RUN_GROWTH, END_GROWTH_CLIP)
    out = {"years": [], "pe": None, "close": close, "end_growth": end_growth, "peak": False,
           "upside": np.nan, "upside_low": np.nan, "upside_high": np.nan, "annual": np.nan}
    band = pe_band(p, group_pe)
    if band is None or not _ok(close) or close <= 0:
        return out
    peak = band["basis"] == "below"
    path = eps_path(p, estimates, end_growth, momentum=not peak)
    if not path:
        return out
    for row in path:
        row.update({k: row["eps"] * band[k] if row["eps"] > 0 else np.nan for k in ("low", "base", "high")})
    last = path[-1]
    years_ahead = max((date(last["year"], 12, 31) - today).days / 365.25, 0.25)
    out.update(years=path, pe=band, peak=peak, years_ahead=years_ahead,
               upside=last["base"] / close - 1, upside_low=last["low"] / close - 1,
               upside_high=last["high"] / close - 1,
               annual=(last["base"] / close) ** (1 / years_ahead) - 1 if last["base"] > 0 else np.nan)
    return out
