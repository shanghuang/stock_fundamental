"""Render a self-contained HTML report (Plotly.js from CDN for interactive charts)."""
from __future__ import annotations

import html
import json
import math
from datetime import datetime

import pandas as pd

from .analysis import StockData
from .i18n import HTML_LANG, N_, Translator, normalize, translator

YI = 1e8  # 億 = NT$100 million, the conventional unit for Taiwan financials


def _vals(s: pd.Series, scale: float = 1.0, digits: int = 4) -> list:
    return [None if pd.isna(v) or math.isinf(v) else round(float(v) / scale, digits) for v in s]


def _labels(idx: pd.Index) -> list[str]:
    return [str(p) for p in idx]


def _pct(v, signed: bool = False) -> str:
    if v is None or pd.isna(v):
        return "–"
    return f"{v:+.1%}" if signed else f"{v:.1%}"


def _num(v, digits: int = 2, suffix: str = "") -> str:
    if v is None or pd.isna(v):
        return "–"
    return f"{v:,.{digits}f}{suffix}"


def _charts(q: pd.DataFrame, rev: pd.DataFrame, divs: pd.DataFrame, d: StockData, val: dict,
            tr: Translator) -> list[dict]:
    charts = []
    r = rev.tail(36)
    if not r.empty:
        x = _labels(r.index)
        charts.append({
            "id": "rev", "title": tr("Monthly revenue"), "unit": "NT$ 億",
            "traces": [{"type": "bar", "x": x, "y": _vals(r["revenue"], YI, 1), "name": tr("Revenue"), "slot": 1,
                        "hovertemplate": "%{x}<br>NT$ %{y:,.1f} 億<extra></extra>"}],
        })
        charts.append({
            "id": "rev_yoy", "title": tr("Monthly revenue, year over year"), "unit": "%",
            "traces": [{"type": "bar", "x": x, "y": _vals(r["yoy"]), "name": tr("YoY"), "slot": 1,
                        "hovertemplate": "%{x}<br>%{y:+.1%}<extra></extra>"}],
            "layout": {"yaxis": {"tickformat": ".0%", "zeroline": True}},
        })

    x = _labels(q.index)
    qaxis = {"type": "category", "tickangle": 0, "nticks": 8}  # stop Plotly parsing "2024Q1" as a date
    pct_hover = "%{fullData.name}: %{y:.1%}<extra></extra>"
    charts.append({
        "id": "margins", "title": tr("Margins by quarter"), "unit": tr("% of revenue"),
        "traces": [
            {"type": "scatter", "mode": "lines+markers", "x": x, "y": _vals(q[k]), "name": n, "slot": i + 1,
             "hovertemplate": pct_hover}
            for i, (k, n) in enumerate([("gross_margin", tr("Gross")), ("operating_margin", tr("Operating")),
                                        ("net_margin", tr("Net"))])
        ],
        "layout": {"xaxis": qaxis, "yaxis": {"tickformat": ".0%"}, "hovermode": "x unified"},
    })
    charts.append({
        "id": "eps", "title": tr("Earnings per share"), "unit": "NT$",
        "traces": [
            {"type": "bar", "x": x, "y": _vals(q["eps"], 1, 2), "name": tr("Quarterly EPS"), "slot": 1,
             "hovertemplate": "%{fullData.name}: NT$ %{y:.2f}<extra></extra>"},
            {"type": "scatter", "mode": "lines+markers", "x": x, "y": _vals(q["eps_ttm"], 1, 2),
             "name": tr("Trailing 12 months"), "slot": 2,
             "hovertemplate": "%{fullData.name}: NT$ %{y:.2f}<extra></extra>"},
        ],
        "layout": {"xaxis": qaxis, "hovermode": "x unified"},
    })
    charts.append({
        "id": "returns", "title": tr("Return on equity and assets (trailing 12 months)"), "unit": "%",
        "traces": [
            {"type": "scatter", "mode": "lines+markers", "x": x, "y": _vals(q[k]), "name": n, "slot": i + 1,
             "hovertemplate": pct_hover}
            for i, (k, n) in enumerate([("roe_ttm", "ROE"), ("roa_ttm", "ROA")])
        ],
        "layout": {"xaxis": qaxis, "yaxis": {"tickformat": ".0%"}, "hovermode": "x unified"},
    })
    yi_hover = "%{fullData.name}: NT$ %{y:,.0f} 億<extra></extra>"
    charts.append({
        "id": "cash", "title": tr("Cash flow by quarter"), "unit": "NT$ 億",
        "traces": [
            {"type": "bar", "x": x, "y": _vals(q[k], YI, 1), "name": n, "slot": i + 1, "hovertemplate": yi_hover}
            for i, (k, n) in enumerate([("ocf", tr("Operating cash flow")), ("capex", tr("Capital expenditure")),
                                        ("fcf", tr("Free cash flow"))])
        ],
        "layout": {"xaxis": qaxis, "barmode": "group", "hovermode": "x unified"},
    })

    if "PER" in d.daily:
        per = d.daily["PER"].replace(0, float("nan")).dropna()
        per = per[per.index >= per.index.max() - pd.DateOffset(years=val["years"])]
        shapes, notes = [], []
        for label, v in zip((tr("10th pct"), tr("Median"), tr("90th pct")), val.get("PER_quantiles", [])):
            shapes.append({"type": "line", "xref": "paper", "x0": 0, "x1": 1, "y0": v, "y1": v,
                           "line": {"dash": "dot", "width": 1}, "role": "ref"})
            notes.append({"xref": "paper", "x": 1, "y": v, "xanchor": "right", "yanchor": "bottom",
                          "text": f"{label} {v:.1f}", "showarrow": False, "role": "ref"})
        charts.append({
            "id": "per", "title": tr("Price-to-earnings ratio, daily"), "unit": "×",
            "traces": [{"type": "scatter", "mode": "lines", "x": [str(t.date()) for t in per.index],
                        "y": _vals(per, 1, 2), "name": "P/E", "slot": 1,
                        "hovertemplate": "%{x}<br>P/E %{y:.1f}×<extra></extra>"}],
            "layout": {"shapes": shapes, "annotations": notes, "hovermode": "x"},
        })

    if not divs.empty and divs["cash"].gt(0).any():
        charts.append({
            "id": "divs", "title": tr("Cash dividend per share, by fiscal year"), "unit": "NT$",
            "traces": [{"type": "bar", "y": _vals(divs["cash"], 1, 2),
                        # A fiscal year without four quarters of EPS is still being paid out.
                        "x": [f"{y}" if pd.notna(e) else tr("{year} (partial)", year=y)
                              for y, e in divs["eps"].items()],
                        "name": tr("Cash dividend"), "slot": 1,
                        "customdata": [_pct(p) for p in divs["payout"]],
                        "hovertemplate": tr("FY %{x}<br>NT$ %{y:.2f}<br>Payout ratio %{customdata}")
                                         + "<extra></extra>"}],
            "layout": {"xaxis": {"type": "category"}},
        })
    return charts


_TABLE_ROWS = [
    (N_("Revenue (NT$ 億)"), "revenue", lambda v: _num(v / YI, 0)),
    (N_("Revenue YoY"), "revenue_yoy", lambda v: _pct(v, True)),
    (N_("Gross margin"), "gross_margin", _pct),
    (N_("Operating margin"), "operating_margin", _pct),
    (N_("Net margin"), "net_margin", _pct),
    (N_("EPS (NT$)"), "eps", _num),
    (N_("EPS YoY"), "eps_yoy", lambda v: _pct(v, True)),
    (N_("EPS, TTM (NT$)"), "eps_ttm", _num),
    (N_("ROE, TTM"), "roe_ttm", _pct),
    (N_("ROA, TTM"), "roa_ttm", _pct),
    (N_("Debt ratio"), "debt_ratio", _pct),
    (N_("Current ratio"), "current_ratio", _num),
    (N_("Operating cash flow (NT$ 億)"), "ocf", lambda v: _num(v / YI, 0)),
    (N_("Free cash flow (NT$ 億)"), "fcf", lambda v: _num(v / YI, 0)),
    (N_("OCF / net income, TTM"), "ocf_to_net_income_ttm", _num),
]


def _quarter_table(q: pd.DataFrame, tr: Translator) -> str:
    q = q.tail(12).iloc[::-1]
    head = "".join(f"<th>{p}</th>" for p in q.index)
    rows = "".join(
        f"<tr><th scope='row'>{tr(label)}</th>" + "".join(f"<td>{fmt(v)}</td>" for v in q[key]) + "</tr>"
        for label, key, fmt in _TABLE_ROWS
    )
    return f"<table><thead><tr><th></th>{head}</tr></thead><tbody>{rows}</tbody></table>"


def _dividend_table(divs: pd.DataFrame, tr: Translator) -> str:
    if divs.empty:
        return f"<p class='muted'>{tr('No dividend records in range.')}</p>"
    rows = "".join(
        f"<tr><th scope='row'>{fy}</th><td>{_num(r.cash)}</td><td>{_num(r.stock)}</td>"
        f"<td>{_num(r.eps)}</td><td>{_pct(r.payout)}</td></tr>"
        for fy, r in divs.iloc[::-1].iterrows()
    )
    head = "".join(f"<th>{h}</th>" for h in (tr("Fiscal year"), tr("Cash (NT$)"), tr("Stock (NT$)"),
                                             tr("EPS (NT$)"), tr("Cash payout")))
    return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"


def _price_section(price: dict | None, outlook: dict | None, tr: Translator,
                   saved_url=None) -> tuple[str, dict | None]:
    """Implied prices from the EPS path × the stock's own P/E band (see forecast.price_forecast)."""
    e = html.escape
    if not price or not price.get("years"):
        return (f"<div class='panel'><p class='muted'>{tr('No implied price: it needs a positive EPS and a P/E history. Loss-making companies have none.')}</p></div>",
                None)
    pe, years, close = price["pe"], price["years"], price["close"]
    fin = years[-1]
    source_label = {"analyst": tr("Analyst estimate"), "extrapolated": tr("Recent revenue growth"),
                    "projected": tr("Outlook growth")}
    rows = "".join(
        f"<tr><th scope='row'>{tr('FY{year}', year=y['year'])}</th><td>{_num(y['eps'])}</td>"
        f"<td>{_pct(y['growth'], True)}</td><td class='src'>{e(source_label[y['source']])}"
        + (f" <span class='muted'>({e(y['ref'])})</span>" if y.get("ref") else "")
        + f"</td><td>{_num(y['low'], 0)}</td><td><b>{_num(y['base'], 0)}</b></td><td>{_num(y['high'], 0)}</td>"
        f"<td>{_pct(y['base'] / close - 1, True)}</td></tr>"
        for y in years)
    head = "".join(f"<th{' class=src' if h == tr('EPS basis') else ''}>{h}</th>" for h in (
        tr("Fiscal year"), tr("EPS (NT$)"), tr("EPS growth"), tr("EPS basis"), tr("Low"), tr("Base"),
        tr("High"), tr("Base vs close")))

    def tile(label, value, sub=""):
        return (f"<div class='tile'><div class='tile-label'>{label}</div>"
                f"<div class='tile-value'>{value}</div><div class='tile-sub'>{sub}</div></div>")

    if pe["basis"] == "below":
        band_sub = tr("today’s {cur:.1f}× up to the 5-year 25th percentile", cur=pe["base"])
    else:
        band_sub = tr("5-year 25th–75th percentile, median {m:.1f}×", m=pe["base"])
    tiles = "".join([
        tile(tr("{year} base case", year=tr("FY{year}", year=fin["year"])), f"NT$ {_num(fin['base'], 0)}",
             tr("range NT$ {low}–{high}", low=_num(fin["low"], 0), high=_num(fin["high"], 0))),
        tile(tr("Base vs close"), _pct(price["upside"], True),
             tr("{annual} a year · close NT$ {close}", annual=_pct(price["annual"], True), close=_num(close, 1))),
        tile(tr("P/E band"), f"{pe['low']:.1f}–{pe['high']:.1f}×", band_sub),
        tile(tr("EPS growth after year 1"), _pct(price["end_growth"]),
             tr("theme outlook") if outlook else tr("long-run default")),
    ])

    if outlook:
        name = e(outlook["title"])
        if saved_url:
            name = f"<a href='{e(saved_url(outlook['id']))}'>{name}</a>"
        segment = tr(", segment “{segment}”", segment=e(outlook["segment"])) if outlook.get("segment") else ""
        src = tr("Outlook growth and any analysts’ estimates come from the theme analysis {name}{segment} "
                 "({time}).", name=name, segment=segment, time=e(outlook["created"][:16].replace("T", " ")))
        if not outlook.get("claude"):
            src += " " + e(tr("That run had no Claude research, so there are no analysts’ estimates."))
    else:
        src = e(tr("No theme analysis in the last 30 days includes this stock, so EPS grows at the long-run "
                   "{g} a year after year 1. Run a theme that includes it (with Claude, for analysts’ "
                   "estimates) to use its industry outlook.", g=_pct(price["end_growth"], False)))
    warn = (f"<p class='warn-note'><b>⚠ {tr('Possible earnings peak.')}</b> "
            f"{e(tr('The stock trades below its usual P/E, so the market may expect earnings to fall, as at a cyclical peak. The base case keeps today’s P/E and EPS gets no revenue momentum, but these prices may still be too high.'))}</p>"
            if price.get("peak") else "")
    note = tr("Implied price = EPS for the fiscal year × the stock’s own P/E band over the last 5 years "
              "(25th percentile = low, median = base, 75th percentile = high, at most twice the median). "
              "Year 1 EPS is an analyst estimate when there is one, otherwise trailing EPS grown with the "
              "last 3 months’ revenue; later years grow at the outlook rate, kept within 0–15% a year. "
              "Prices are for the end of each fiscal year and exclude dividends. They show what the price "
              "would be if earnings follow this path and the market values the stock as it has; they are "
              "not price targets.")
    chart = {
        "id": "price", "title": "", "unit": "NT$",
        "traces": [{"type": "scatter", "mode": "markers", "name": tr("Base case"), "slot": 1,
                    "x": [tr("FY{year}", year=y["year"]) for y in years], "y": [round(y["base"], 1) for y in years],
                    "error_y": {"type": "data", "symmetric": False, "thickness": 2, "width": 8,
                                "array": [round(y["high"] - y["base"], 1) for y in years],
                                "arrayminus": [round(y["base"] - y["low"], 1) for y in years]},
                    "customdata": [[round(y["low"], 1), round(y["high"], 1)] for y in years],
                    "hovertemplate": tr("%{x}<br>Base NT$ %{y:,.0f}<br>Range NT$ %{customdata[0]:,.0f}–%{customdata[1]:,.0f}")
                                     + "<extra></extra>"}],
        "layout": {"xaxis": {"type": "category"}, "yaxis": {"rangemode": "tozero"}, "hovermode": "closest",
                   "shapes": [{"type": "line", "xref": "paper", "x0": 0, "x1": 1, "y0": close, "y1": close,
                               "line": {"dash": "dot", "width": 1}, "role": "ref"}],
                   "annotations": [{"xref": "paper", "x": 0.17, "y": close, "xanchor": "center", "yanchor": "bottom",
                                    "text": tr("Close {close}", close=_num(close, 1)), "showarrow": False,
                                    "role": "ref"}]},
    }
    body = (f"<div class='tiles'>{tiles}</div>{warn}"
            f"<div class='price'><div class='panel scroll'><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
            f"<section class='chart'><h3>{e(tr('Implied price by fiscal year'))}</h3>"
            f"<div class='unit'>{e(tr('NT$ · dot = median P/E, bar = P/E band, dotted line = latest close'))}</div>"
            f"<div id='c_price' class='plot'></div></section></div>"
            f"<p class='muted small'>{src} {e(note)}</p>")
    return body, chart


def tr_market(market: str, lang: str) -> str:
    tr = translator(lang)
    return {"twse": tr("TWSE 上市"), "tpex": tr("TPEx 上櫃")}.get(market, market)


def render(d: StockData, q: pd.DataFrame, rev: pd.DataFrame, divs: pd.DataFrame,
           val: dict, checks: list[tuple[bool, str]], nav: str = "", lang: str = "en",
           price: dict | None = None, outlook: dict | None = None, saved_url=None) -> str:
    tr = translator(lang)
    last = q.dropna(subset=["eps"]).iloc[-1] if q["eps"].notna().any() else pd.Series(dtype=float)
    latest_q = q["eps"].last_valid_index() or "–"

    def tile(label: str, value: str, sub: str = "") -> str:
        return (f"<div class='tile'><div class='tile-label'>{label}</div>"
                f"<div class='tile-value'>{value}</div><div class='tile-sub'>{sub}</div></div>")

    def pct_sub(key: str) -> str:
        p = val.get(f"{key}_pct")
        return "" if p is None else tr("{p:.0%} of days in range were lower", p=p)

    tiles = "".join([
        tile(tr("Close"), _num(val.get("close"), 1), f"NT$ · {val.get('date') or ''}"),
        tile("P/E", _num(val.get("PER"), 1, "×"), pct_sub("PER")),
        tile("P/B", _num(val.get("PBR"), 2, "×"), pct_sub("PBR")),
        tile(tr("Dividend yield"), _num(val.get("dividend_yield"), 2, "%"), pct_sub("dividend_yield")),
        tile(tr("EPS, TTM"), _num(last.get("eps_ttm")), tr("NT$ · through {q}", q=latest_q)),
        tile(tr("ROE, TTM"), _pct(last.get("roe_ttm")), tr("through {q}", q=latest_q)),
    ])
    check_items = "".join(
        f"<li class='{'pass' if ok else 'fail'}'><span class='mark' aria-hidden='true'>{'✓' if ok else '✗'}</span>"
        f"<span class='status'>{tr('Pass') if ok else tr('Fail')}</span> {html.escape(text)}</li>"
        for ok, text in checks
    )
    charts = _charts(q, rev, divs, d, val, tr)
    chart_divs = "".join(
        f"<section class='chart'><h3>{html.escape(c['title'])}</h3><div class='unit'>{html.escape(c['unit'])}</div>"
        f"<div id='c_{c['id']}' class='plot'></div></section>"
        for c in charts
    )
    title = f"{d.stock_id} {d.name}"
    price_html, price_chart = _price_section(price, outlook, tr, saved_url)
    if price_chart:
        charts.append(price_chart)

    return _TEMPLATE.format(
        css=BASE_CSS,
        nav=nav,
        html_lang=HTML_LANG[normalize(lang)],
        title=html.escape(title),
        doc_title=html.escape(tr("{title} · Fundamental report", title=title)),
        meta=html.escape(f"{tr_market(d.market, lang)} · {d.industry}"),
        tiles=tiles,
        notice=(f"<p class='muted'>{tr('Financial company: leverage, liquidity and cash-flow checks are skipped, and gross margin does not apply. Banks and insurers file a different statement layout, so read the charts with care.')}</p>")
        if d.is_financial else "",
        checks=check_items or f"<li class='muted'>{tr('Not enough data for checks.')}</li>",
        charts=chart_divs,
        quarter_table=_quarter_table(q, tr),
        dividend_table=_dividend_table(divs, tr),
        h_checks=tr("Quick checks"), h_trends=tr("Trends"), h_quarterly=tr("Quarterly detail"),
        h_dividends=tr("Dividends"), h_price=tr("Price forecast"), price=price_html,
        footer=html.escape(tr("Data: FinMind (TWSE / TPEx / MOPS filings). Consolidated statements; TTM = trailing four quarters. 1 億 = NT$100,000,000. Generated {generated}. For research only — not investment advice.",
                              generated=datetime.now().strftime("%Y-%m-%d %H:%M"))),
        data=json.dumps(charts, ensure_ascii=False).replace("</", "<\\/"),
    )


BASE_CSS = """.viz-root {
  color-scheme: light;
  --surface-0: #f4f4f2; --surface-1: #fcfcfb; --border: #e3e2de;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #7a7974;
  --good: #008300; --critical: #c62f2e;
}
@media (prefers-color-scheme: dark) {
  .viz-root {
    color-scheme: dark;
    --surface-0: #111110; --surface-1: #1a1a19; --border: #2e2e2c;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #8f8e86;
    --good: #3fae3f; --critical: #e66767;
  }
}
* { box-sizing: border-box; }
body { margin: 0; }
.viz-root { background: var(--surface-0); color: var(--text-primary);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang TC", "Noto Sans TC", sans-serif;
  min-height: 100vh; padding: 32px; }
main { max-width: 1200px; margin: 0 auto; }
h1 { margin: 0; font-size: 28px; }
h2 { font-size: 18px; margin: 36px 0 12px; }
h3 { font-size: 15px; margin: 0; }
.meta, .muted, .unit, .tile-sub, .tile-label { color: var(--text-secondary); }
.unit { font-size: 12px; margin-bottom: 4px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-top: 20px; }
.tile, .chart, .panel { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.tile-label { font-size: 12px; }
.tile-value { font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile-sub { font-size: 12px; }
.checks { list-style: none; margin: 0; padding: 0; columns: 2 380px; }
.checks li { padding: 4px 0; break-inside: avoid; }
.checks .mark { display: inline-block; width: 1.2em; font-weight: 700; }
.checks .status { font-weight: 600; margin-right: 4px; }
.checks .pass .mark, .checks .pass .status { color: var(--good); }
.checks .fail .mark, .checks .fail .status { color: var(--critical); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(520px, 1fr)); gap: 16px; }
.plot { height: 300px; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; font-size: 13px; }
th, td { padding: 6px 10px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
th[scope=row], thead th:first-child { text-align: left; color: var(--text-secondary); font-weight: 500; }
thead th { color: var(--text-secondary); font-weight: 600; }
.small { font-size: 12px; }
.price { display: grid; gap: 12px; margin-top: 12px; }
.price .plot { height: 240px; }
.src { text-align: left !important; }
.warn-note { margin: 12px 0 0; padding: 10px 14px; border-radius: 10px; background: var(--surface-1);
  border: 1px solid var(--border); border-left: 3px solid var(--critical); }
footer { margin-top: 40px; font-size: 12px; color: var(--text-muted); }
.nav { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 24px; }
.nav a { color: var(--text-secondary); text-decoration: none; }
.nav a:hover { color: var(--text-primary); }
.nav form { display: flex; gap: 8px; margin-left: auto; }
input, select, button, textarea { font: inherit; color: var(--text-primary); background: var(--surface-1);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; }
button { cursor: pointer; }
button:hover { border-color: var(--text-secondary); }
.nav .who { margin-left: auto; display: flex; gap: 12px; align-items: center; }
.nav .who + .lang, .nav form + .who { margin-left: 0; }
.nav form.inline { margin-left: 0; display: inline; }
form.inline { display: inline; margin: 0; }
button.link { border: 0; background: none; padding: 0; color: var(--text-secondary); text-decoration: underline; }
button.link:hover { color: var(--text-primary); }
.badge { display: inline-block; min-width: 18px; padding: 0 6px; border-radius: 999px; font-size: 11px;
  line-height: 18px; text-align: center; background: var(--critical); color: #fff; font-weight: 600; }
#notices { display: contents; }
.notice-pop { position: fixed; right: 20px; bottom: 20px; z-index: 50; width: min(380px, calc(100vw - 40px));
  background: var(--surface-1); color: var(--text-primary); border: 1px solid var(--border);
  border-left: 3px solid var(--good); border-radius: 10px; padding: 14px 16px;
  box-shadow: 0 8px 24px rgb(0 0 0 / 0.18); animation: notice-in 0.25s ease-out; }
.notice-pop ul { margin: 8px 0 12px; padding-left: 18px; }
.notice-pop li { margin-bottom: 6px; }
.notice-pop a { color: inherit; font-weight: 600; text-decoration: underline; }
@keyframes notice-in { from { transform: translateY(12px); opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .notice-pop { animation: none; } }
"""


_TEMPLATE = """<!DOCTYPE html>
<html lang="{html_lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{doc_title}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
{css}</style>
</head>
<body>
<div class="viz-root">
<main>
  {nav}
  <h1>{title}</h1>
  <div class="meta">{meta}</div>
  <div class="tiles">{tiles}</div>

  <h2>{h_checks}</h2>
  <div class="panel">{notice}<ul class="checks">{checks}</ul></div>

  <h2>{h_price}</h2>
  {price}

  <h2>{h_trends}</h2>
  <div class="grid">{charts}</div>

  <h2>{h_quarterly}</h2>
  <div class="panel scroll">{quarter_table}</div>

  <h2>{h_dividends}</h2>
  <div class="panel scroll">{dividend_table}</div>

  <footer>{footer}</footer>
</main>
</div>
<script>
const CHARTS = {data};
const THEMES = {{
  light: {{ series: ["#2a78d6", "#eb6834", "#1baf7a"], text: "#0b0b0b", muted: "#52514e",
            grid: "#e9e8e4", ref: "#7a7974", surface: "#fcfcfb" }},
  dark:  {{ series: ["#3987e5", "#d95926", "#199e70"], text: "#f0efec", muted: "#c3c2b7",
            grid: "#2e2e2c", ref: "#8f8e86", surface: "#1a1a19" }},
}};
const mq = window.matchMedia("(prefers-color-scheme: dark)");

function draw() {{
  const t = mq.matches ? THEMES.dark : THEMES.light;
  for (const c of CHARTS) {{
    const traces = c.traces.map(({{slot, ...tr}}) => {{
      const color = t.series[slot - 1];
      if (tr.type === "bar") tr.marker = {{ color, line: {{ width: 0 }} }};
      else {{
        tr.line = {{ color, width: 2 }};
        tr.marker = {{ color, size: 8, line: {{ color: t.surface, width: 2 }} }};
        if (tr.error_y) tr.error_y = {{ ...tr.error_y, color }};
      }}
      return tr;
    }});
    const extra = c.layout || {{}};
    const recolor = (items) => (items || []).map(({{role, ...s}}) =>
      s.line ? {{ ...s, line: {{ ...s.line, color: t.ref }} }} : {{ ...s, font: {{ color: t.muted, size: 11 }}, bgcolor: t.surface }});
    const layout = {{
      ...extra,
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: {{ color: t.muted, size: 12 }},
      margin: {{ l: 56, r: 12, t: 8, b: 40 }},
      showlegend: c.traces.length > 1,
      legend: {{ orientation: "h", x: 0, y: 1.12, font: {{ color: t.text }} }},
      barcornerradius: 4, bargap: 0.25, bargroupgap: 0.08,
      hoverlabel: {{ bgcolor: t.surface, bordercolor: t.grid, font: {{ color: t.text }} }},
      xaxis: {{ gridcolor: t.grid, linecolor: t.grid, ...(extra.xaxis || {{}}), showgrid: false }},
      yaxis: {{ gridcolor: t.grid, zerolinecolor: t.ref, ...(extra.yaxis || {{}}) }},
      shapes: recolor(extra.shapes), annotations: recolor(extra.annotations),
    }};
    Plotly.react("c_" + c.id, traces, layout, {{ displayModeBar: false, responsive: true }});
  }}
}}
mq.addEventListener("change", draw);
draw();
</script>
</body>
</html>
"""
