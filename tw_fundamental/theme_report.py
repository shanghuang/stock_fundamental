"""HTML rendering for theme analysis (supply chain, market outlook, comparison, valuation, SWOT cards)."""
from __future__ import annotations

import html
import json
import math
from datetime import datetime
from typing import Callable

from .i18n import HTML_LANG, N_, Translator, normalize, translator
from .report import BASE_CSS
from .theme import StockProfile, ThemeResult

STAGE_LABELS = {"upstream": N_("Upstream"), "midstream": N_("Midstream"), "downstream": N_("Downstream"),
                "support": N_("Supporting"), "unmapped": N_("Not mapped (grouped by FinMind industry)")}
STAGE_SHORT = {**STAGE_LABELS, "unmapped": N_("Not mapped")}
QUADRANTS = [("strengths", N_("Strengths")), ("weaknesses", N_("Weaknesses")),
             ("opportunities", N_("Opportunities")), ("threats", N_("Threats"))]
EXPOSURE = {"direct": N_("direct exposure"), "indirect": N_("indirect exposure"),
            "peripheral": N_("peripheral exposure"), "unclear": N_("unclear exposure")}
LOW_HIGH = N_("low {low} · high {high}")


def _e(v) -> str:
    return html.escape(str(v))


def _bad(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def _pct(v, signed=False) -> str:
    return "–" if _bad(v) else (f"{v:+.1%}" if signed else f"{v:.1%}")


def _num(v, d=1, suffix="") -> str:
    return "–" if _bad(v) else f"{v:,.{d}f}{suffix}"


def _label(p: StockProfile) -> str:
    return f"{p.stock_id} {p.name}"


def _stock_cell(p: StockProfile, link: Callable[[str], str] | None) -> str:
    text = f"<b>{_e(p.stock_id)}</b> {_e(p.name)}"
    return f"<a href='{_e(link(p.stock_id))}'>{text}</a>" if link else text


def _li(items, tr: Translator) -> str:
    return "".join(f"<li>{_e(i)}</li>" for i in items) or f"<li class='muted'>{tr('None identified')}</li>"


def _exposure(value: str, tr: Translator) -> str:
    return tr(EXPOSURE[value]) if value in EXPOSURE else value


def _overview(r: ThemeResult, tr: Translator) -> str:
    parts = []
    if r.theme.description:
        parts.append(f"<p>{_e(r.theme.description)}</p>")
    if r.llm_error:
        parts.append("<p class='notice'>" + tr("<b>Claude research unavailable:</b> {error}. Showing data-driven analysis only.",
                                              error=_e(r.llm_error)) + "</p>")
    res = r.research
    if not res:
        if not r.llm_error:
            parts.append(f"<p class='muted'>{tr('Data-driven analysis only. Turn on Claude research for an industry overview, more candidate stocks and a qualitative SWOT (needs ANTHROPIC_API_KEY).')}</p>")
        return f"<div class='panel'>{''.join(parts)}</div>"

    parts.append(f"<p>{_e(res.get('summary', ''))}</p>")
    parts.append("<div class='cols3'>"
                 f"<div><h3>{tr('Demand drivers')}</h3><ul>{_li(res.get('drivers', []), tr)}</ul></div>"
                 f"<div><h3>{tr('Risks')}</h3><ul>{_li(res.get('risks', []), tr)}</ul></div>"
                 "</div>")
    cands = res.get("candidates", [])
    if cands:
        rows = "".join(
            f"<tr><td>{_e(c.get('stock_id'))}</td><td>{_e(c.get('name'))}</td><td>{_e(c.get('role'))}</td>"
            f"<td class='wrap'>{_e(c.get('rationale'))}</td>"
            f"<td>{tr('added') if c.get('stock_id') in r.added_by_research else ''}</td></tr>"
            for c in cands
        )
        head = "".join(f"<th>{h}</th>" for h in (tr("Code"), tr("Name"), tr("Role"), tr("Rationale"), ""))
        summary = tr("Companies named in research ({n}; {added} added to the analysis)",
                     n=len(cands), added=len(r.added_by_research))
        parts.append(f"<details><summary>{summary}</summary><table class='left'><thead><tr>{head}</tr></thead>"
                     f"<tbody>{rows}</tbody></table></details>")
    sources = res.get("sources", [])
    if sources:
        links = "".join(f"<li><a href='{_e(s['url'])}' target='_blank' rel='noopener noreferrer'>"
                        f"{_e(s.get('title') or s['url'])}</a></li>" for s in sources)
        parts.append(f"<details><summary>{tr('Sources ({n})', n=len(sources))}</summary><ul>{links}</ul></details>")
    return f"<div class='panel'>{''.join(parts)}</div>"


def _role_note(p: StockProfile, tr: Translator) -> str:
    if p.role_source == "Claude":
        if p.exposure:
            return tr("role from Claude research, {exposure}", exposure=_exposure(p.exposure, tr))
        return tr("role from Claude research")
    if p.role_source == "preset":
        return tr("role from the preset map")
    return tr("not mapped; grouped by FinMind industry")


def _supply_chain(r: ThemeResult, ok: list[StockProfile], link, tr: Translator) -> str:
    """Flow map (upstream → downstream, supporting and unmapped below) plus a segment table."""
    by_id = {p.stock_id: p for p in ok}

    def box(seg: dict) -> str:
        members = [by_id[sid] for sid in seg["stock_ids"]]
        items = "".join(
            f"<li>{_stock_cell(p, link)}"
            + (f" <span class='badge'>{_e(_exposure(p.exposure, tr))}</span>" if p.exposure else "")
            + (f"<div class='small'>{_e(p.role)}</div>" if p.role else "")
            + "<div class='small muted'>"
            + tr("3m revenue {rev} YoY · GM {gm}", rev=_pct(p.metrics.get("rev_3m_yoy"), True),
                 gm=_pct(p.metrics.get("gross_margin")))
            + "</div></li>"
            for p in members) or f"<li class='muted small'>{tr('No tracked company in this segment')}</li>"
        stats = ("<div class='segstats'>" + tr("Median 3m revenue {rev} · GM {gm}", rev=_pct(seg["rev_3m_yoy"], True),
                                               gm=_pct(seg["gross_margin"])) + "</div>") if members else ""
        desc = f"<div class='small muted'>{_e(seg['description'])}</div>" if seg.get("description") else ""
        return f"<div class='seg'><h4>{_e(seg['name'])}</h4>{desc}{stats}<ul class='members'>{items}</ul></div>"

    def lane(stage: str) -> str:
        segs = [g for g in r.segments if g["stage"] == stage]
        if not segs:
            return ""
        return f"<div class='lane'><h3>{tr(STAGE_LABELS[stage])}</h3>{''.join(box(g) for g in segs)}</div>"

    flow = [x for x in (lane(st) for st in ("upstream", "midstream", "downstream")) if x]
    arrow = "<div class='arrow' aria-hidden='true'>→</div>"
    side = "".join(lane(st) for st in ("support", "unmapped"))
    flow_html = (f"<div class='flow'>{arrow.join(flow)}</div>" if flow else "") + \
                (f"<div class='side'>{side}</div>" if side else "")

    rows = "".join(
        f"<tr><th scope='row'>{_e(g['name'])}</th><td>{tr(STAGE_SHORT[g['stage']])}</td>"
        f"<td>{len(g['stock_ids'])}</td>"
        f"<td>{_num(g['revenue_ttm'] / 1e8 if not _bad(g['revenue_ttm']) else None, 0)}</td>"
        f"<td>{_pct(g['rev_3m_yoy'], True)}</td><td>{_pct(g['rev_12m_yoy'], True)}</td>"
        f"<td>{_pct(g['gross_margin'])}</td><td>{_pct(g['operating_margin'])}</td><td>{_pct(g['roe_ttm'])}</td>"
        f"<td>{_num(g['per'], 1, '×')}</td><td>{g['headlines']}</td></tr>"
        for g in r.segments if g["stock_ids"])
    head = "".join(f"<th>{h}</th>" for h in (
        tr("Segment"), tr("Stage"), tr("Companies"), tr("Combined revenue, TTM (億)"), tr("Revenue, last 3m YoY"),
        tr("Revenue, last 12m YoY"), tr("Gross margin"), tr("Operating margin"), tr("ROE, TTM"), "P/E",
        tr("Theme headlines")))
    table = f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
    if r.research:
        source = tr("Segments and roles come from Claude's web research, falling back to the preset map.")
    elif any(p.role_source == "preset" for p in ok):
        source = tr("Segments and roles come from the preset map in themes.json, compiled from public reports; "
                    "verify them, or turn on Claude research to have them checked against current sources.")
    else:
        source = tr("This theme has no supply-chain map, so stocks are grouped by FinMind industry. Turn on "
                    "Claude research to place them in the supply chain, or add the theme to themes.json.")
    note = tr("Segment figures are medians of the tracked companies, so a segment with one or two companies "
              "is only indicative.")
    return (f"<p class='muted small'>{source} {note}</p>{flow_html}"
            f"<div class='panel scroll' style='margin-top:16px'>{table}</div>")


def _segment_chart(r: ThemeResult, ok: list[StockProfile], tr: Translator) -> dict | None:
    by_id = {p.stock_id: p for p in ok}
    segs = [g for g in r.segments if g["stock_ids"]]
    pts = [(g["name"], by_id[sid]) for g in segs for sid in g["stock_ids"]
           if not _bad(by_id[sid].metrics.get("rev_3m_yoy"))]
    if len(segs) < 2 or not pts:
        return None
    return {"order": [g["name"] for g in segs],
            "y": [n for n, _ in pts], "x": [round(p.metrics["rev_3m_yoy"], 4) for _, p in pts],
            "text": [_label(p) for _, p in pts],
            "my": [g["name"] for g in segs if not _bad(g["rev_3m_yoy"])],
            "mx": [round(g["rev_3m_yoy"], 4) for g in segs if not _bad(g["rev_3m_yoy"])],
            "names": [tr("Company"), tr("Segment median")], "median": tr("median")}


def _table(ok: list[StockProfile], med: dict, link, tr: Translator) -> str:
    cols = [
        (tr("Revenue, last 3m YoY"), lambda m: _pct(m["rev_3m_yoy"], True)),
        (tr("Revenue, last 12m YoY"), lambda m: _pct(m["rev_12m_yoy"], True)),
        (tr("Gross margin"), lambda m: _pct(m["gross_margin"])),
        (tr("GM change YoY"), lambda m: "–" if _bad(m["gross_margin_chg"])
         else tr("{v:+.1f} pts", v=m["gross_margin_chg"] * 100)),
        (tr("Operating margin"), lambda m: _pct(m["operating_margin"])),
        (tr("ROE, TTM"), lambda m: _pct(m["roe_ttm"])),
        (tr("EPS, TTM"), lambda m: _num(m["eps_ttm"], 2)),
        ("P/E", lambda m: _num(m["per"], 1, "×") if not _bad(m["per"]) and m["per"] > 0 else "–"),
        (tr("P/E vs own 5y"), lambda m: "–" if _bad(m["per_pct"]) else tr("{v:.0%} pct", v=m["per_pct"])),
        (tr("Yield"), lambda m: _num(m["dividend_yield"], 2, "%")),
    ]
    head = "".join(f"<th>{c}</th>" for c, _ in cols)
    rows = "".join(
        f"<tr><th scope='row'>{_stock_cell(p, link)}</th><td class='left'>{_e(p.segment or '–')}</td>"
        + "".join(f"<td>{fmt(p.metrics)}</td>" for _, fmt in cols)
        + f"<td>{len(p.headlines)}</td><td>{_e(p.latest_quarter)}</td></tr>"
        for p in ok
    )
    medians = (f"<tr class='median'><th scope='row'>{tr('Group median')}</th><td></td><td></td>"
               f"<td>{_pct(med.get('rev_12m_yoy'), True)}</td><td>{_pct(med.get('gross_margin'))}</td><td></td>"
               f"<td>{_pct(med.get('operating_margin'))}</td><td>{_pct(med.get('roe_ttm'))}</td><td></td>"
               f"<td>{_num(med.get('per'), 1, '×')}</td><td></td><td></td><td></td><td></td></tr>")
    return (f"<table><thead><tr><th>{tr('Stock')}</th><th class='left'>{tr('Segment')}</th>{head}"
            f"<th>{tr('Theme headlines')}</th><th>{tr('Latest quarter')}</th></tr></thead>"
            f"<tbody>{rows}{medians}</tbody></table>")


def _swot_card(p: StockProfile, swot: dict | None, news_days: int, link, tr: Translator) -> str:
    llm_ok = bool(swot) and "error" not in swot
    content = swot if llm_ok else p.signals
    quads = "".join(
        f"<div class='quad q-{k}'><h4>{tr(label)}</h4><ul>{_li(content.get(k, []), tr)}</ul></div>"
        for k, label in QUADRANTS
    )
    head = f"<h3>{_stock_cell(p, link)}</h3><div class='muted'>{_e(p.industry)}</div>"
    if p.segment:
        head += (f"<div class='role'><b>{_e(p.segment)}</b>{' · ' + _e(p.role) if p.role else ''} "
                 f"<span class='muted small'>({_e(_role_note(p, tr))})</span></div>")
    extra = ""
    if llm_ok:
        head += (f"<div class='role'><span class='badge'>{_e(_exposure(swot.get('exposure', ''), tr))}</span> "
                 f"{_e(swot.get('role_in_theme', ''))}</div>")
        extra += f"<p class='takeaway'>{_e(swot.get('takeaway', ''))}</p>"
        sig = "".join(f"<li><b>{tr(label)}:</b> {_e(i)}</li>" for k, label in QUADRANTS for i in p.signals.get(k, []))
        extra += (f"<details><summary>{tr('Data signals')}</summary>"
                  f"<ul>{sig or '<li>' + tr('None') + '</li>'}</ul></details>")
    else:
        note = (" " + tr("Claude SWOT failed: {error}.", error=_e(swot["error"]))) if swot and "error" in swot else ""
        head += f"<div class='muted small'>{tr('Data-driven signals.')}{note}</div>"
    if p.headlines:
        items = "".join(
            f"<li><span class='muted'>{_e(h['date'])}</span> "
            + (f"<a href='{_e(h['link'])}' target='_blank' rel='noopener noreferrer'>{_e(h['title'])}</a>"
               if h["link"] else _e(h["title"]))
            + "</li>" for h in p.headlines[:8])
        summary = tr("Theme headlines, last {days} day(s) ({n})", days=news_days, n=len(p.headlines))
        extra += f"<details><summary>{summary}</summary><ul class='news'>{items}</ul></details>"
    if p.error:
        extra += f"<p class='muted small'>{tr('Note: {error}', error=_e(p.error))}</p>"
    return f"<section class='card'>{head}<div class='swot'>{quads}</div>{extra}</section>"


def _tile(label: str, value: str, sub: str = "") -> str:
    return (f"<div class='tile'><div class='tile-label'>{_e(label)}</div><div class='tile-value'>{_e(value)}</div>"
            f"<div class='tile-sub'>{_e(sub)}</div></div>")


def _sources(sources: list[dict], tr: Translator) -> str:
    if not sources:
        return ""
    links = "".join(f"<li><a href='{_e(s['url'])}' target='_blank' rel='noopener noreferrer'>"
                    f"{_e(s.get('title') or s['url'])}</a></li>" for s in sources)
    return f"<details><summary>{tr('Sources ({n})', n=len(sources))}</summary><ul>{links}</ul></details>"


def _forecast_chart(cid: str, f: dict, title: str, unit: str, tr: Translator) -> dict:
    pts = f["points"]
    return {"id": cid, "title": title, "unit": unit, "x": [str(p["year"]) for p in pts],
            "base": [round(p["base"], 2) for p in pts], "low": [round(p["low"], 2) for p in pts],
            "high": [round(p["high"], 2) for p in pts], "names": [tr("Base case"), tr("Low–high range")]}


def _outlook(r: ThemeResult, tr: Translator) -> tuple[str, list[dict]]:
    """Six-year market outlook: Claude's sourced forecast and the tracked companies' projection."""
    f, proj = r.market_forecast, r.projection
    parts, tiles, charts = [], [], []
    if r.forecast_error:
        parts.append("<p class='notice'>" + tr("<b>Claude market forecast unavailable:</b> {error}.",
                                              error=_e(r.forecast_error)) + "</p>")
    if f:
        (y0, v0), (y1, v1) = [(p["year"], p["base"]) for p in (f["points"][0], f["points"][-1])]
        tiles += [_tile(tr("Market size, {year}", year=y0), _num(v0, 1), f["unit"]),
                  _tile(tr("Market size, {year}", year=y1), _num(v1, 1), f["unit"]),
                  _tile(tr("Forecast CAGR, {start}–{end}", start=y0, end=y1), _pct(f["cagr"], True),
                        tr(LOW_HIGH, low=_pct(f["cagr_low"], True), high=_pct(f["cagr_high"], True)))]
        charts.append(_forecast_chart("mkt", f, tr("Market size forecast"), f["unit"], tr))
        parts.append(f"<p><b>{tr('Market definition')}:</b> {_e(f['market'])}</p>")
        parts.append(f"<p class='muted small'>{tr('Claude’s reconciliation of published forecasts found by web search. Reports measure different scopes, so compare them below and check the sources.')}</p>")
    if proj:
        y0, y1 = proj["points"][0]["year"], proj["points"][-1]["year"]
        tiles += [_tile(tr("Tracked companies’ revenue growth, last 12 months"), _pct(proj["recent_growth"], True),
                        tr("{n} companies, through {month}", n=proj["companies"], month=proj["through"])),
                  _tile(tr("Projected CAGR of tracked revenue, {start}–{end}", start=y0, end=y1),
                        _pct(proj["cagr"], True),
                        tr(LOW_HIGH, low=_pct(proj["cagr_low"], True), high=_pct(proj["cagr_high"], True)))]
        charts.append(_forecast_chart("proj", proj, tr("Tracked companies’ combined revenue, projected"),
                                      tr("NT$ 億 · {year} is the 12 months through {month}", year=y0,
                                         month=proj["through"]), tr))
        parts.append("<p class='muted small'>" + tr(
            "The tracked-revenue projection starts from the last 12 months’ growth ({g0:+.1%}, capped between "
            "−30% and +60%) and fades to {lr:.0%} a year by {end}; the low and high cases start {spread:.0f} "
            "points lower and higher. It describes these companies only and is not a market forecast.",
            g0=proj["start_growth"], lr=proj["long_run_growth"], end=y1, spread=proj["spread"] * 100) + "</p>")
        top = proj.get("largest")
        if top and top["share"] >= 0.5:
            parts.append("<p class='muted small'>" + tr(
                "{name} is {share:.0%} of the tracked companies’ combined revenue, so the projection mostly "
                "reflects it; the segment table below is less affected.", name=_e(top["label"]),
                share=top["share"]) + "</p>")
    if f and proj:
        parts.append("<p>" + tr("Tracked companies grew {g:+.1%} over the last 12 months, against a forecast "
                                "market CAGR of {cagr:+.1%}.", g=proj["recent_growth"], cagr=f["cagr"]) + "</p>")
    if not f and not r.research and not r.forecast_error:
        parts.append(f"<p class='muted'>{tr('Turn on Claude research for published market-size forecasts and analysts’ EPS estimates.')}</p>")
    if not f and not proj:
        parts.append(f"<p class='muted'>{tr('Not enough revenue data for a projection.')}</p>")
        return f"<div class='panel'>{''.join(parts)}</div>", charts

    if f and f["published"]:
        rows = "".join(f"<tr><td>{_e(x['publisher'])}</td><td>{_e(x['published'])}</td>"
                       f"<td class='wrap'>{_e(x['scope'])}</td><td class='wrap'>{_e(x['figure'])}</td></tr>"
                       for x in f["published"])
        head = "".join(f"<th>{h}</th>" for h in (tr("Publisher"), tr("Published"), tr("Scope"), tr("Forecast")))
        parts.append(f"<details open><summary>{tr('Published forecasts ({n})', n=len(f['published']))}</summary>"
                     f"<table class='left'><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></details>")
    if f and f["assumptions"]:
        parts.append(f"<details><summary>{tr('Assumptions')}</summary><ul>{_li(f['assumptions'], tr)}</ul></details>")
    if f:
        parts.append(_sources(f.get("sources", []), tr))

    table = _segment_outlook(r, tr)
    chart_html = "".join(
        f"<section class='chart'><h3>{_e(c['title'])}</h3><div class='unit'>{_e(c['unit'])}</div>"
        f"<div id='c_{c['id']}' class='plot'></div></section>" for c in charts)
    return (f"<div class='tiles' style='margin-top:0'>{''.join(tiles)}</div>"
            f"<div class='grid' style='margin-top:16px'>{chart_html}</div>"
            f"<div class='panel' style='margin-top:16px'>{''.join(parts)}</div>{table}"), charts


def _segment_outlook(r: ThemeResult, tr: Translator) -> str:
    proj = {s["segment"]: s for s in (r.projection or {}).get("segments", [])}
    claude = {s["segment"]: s for s in (r.market_forecast or {}).get("segments", [])}
    stages = {g["name"]: g["stage"] for g in r.segments}
    names = [g["name"] for g in r.segments if g["name"] in proj or g["name"] in claude]
    names += [n for n in claude if n not in stages]
    if not names:
        return ""
    has_claude = bool(claude)
    rows = ""
    for n in names:
        pr, cl = proj.get(n, {}), claude.get(n, {})
        rows += (f"<tr><th scope='row'>{_e(n)}</th>"
                 f"<td>{tr(STAGE_SHORT[stages[n]]) if n in stages else '–'}</td>"
                 f"<td>{_pct(pr.get('recent'), True)}</td><td>{_pct(pr.get('cagr'), True)}</td>"
                 + (f"<td>{_pct(cl.get('cagr'), True)}</td><td class='wrap left'>{_e(cl.get('note', ''))}</td>"
                    if has_claude else "") + "</tr>")
    head = [tr("Segment"), tr("Stage"), tr("Tracked revenue, last 12m YoY"), tr("Projected CAGR (tracked)")]
    if has_claude:
        head += [tr("Forecast CAGR (Claude)"), tr("Note")]
    head_html = "".join(f"<th>{h}</th>" for h in head)
    return (f"<h3 style='margin-top:16px'>{tr('Growth by supply-chain segment')}</h3>"
            f"<div class='panel scroll' style='margin-top:8px'><table><thead><tr>{head_html}</tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")


BASIS = {"analyst": N_("analyst estimate, FY{year}"), "extrapolated": N_("extrapolated"), "none": N_("not available")}


def _basis(fwd: dict, tr: Translator) -> str:
    basis = fwd.get("basis", "none")
    text = _e(tr(BASIS[basis], year=fwd.get("fiscal_year")) if basis == "analyst" else tr(BASIS.get(basis, basis)))
    src = fwd.get("source", "")
    if basis == "analyst" and src:
        src_html = (f"<a href='{_e(src)}' target='_blank' rel='noopener noreferrer'>{tr('source')}</a>"
                    if src.startswith(("http://", "https://")) else _e(src))
        text += f" <span class='muted small'>({src_html})</span>"
    return text


def _valuation(r: ThemeResult, ok: list[StockProfile], link, tr: Translator) -> tuple[str, dict | None]:
    """Trailing vs forward P/E per stock, with the basis of each forward EPS."""
    def per(v):
        return _num(v, 1, "×") if not _bad(v) and v > 0 else "–"

    rows = "".join(
        f"<tr><th scope='row'>{_stock_cell(p, link)}</th><td class='left'>{_e(p.segment or '–')}</td>"
        f"<td>{_num(p.metrics.get('close'), 1)}</td><td>{_num(p.metrics.get('eps_ttm'), 2)}</td>"
        f"<td>{per(p.metrics.get('per'))}</td><td>{_num(p.forward.get('eps'), 2)}</td>"
        f"<td><b>{per(p.forward.get('per'))}</b></td><td>{_pct(p.forward.get('growth'), True)}</td>"
        f"<td class='left'>{_basis(p.forward, tr)}</td></tr>"
        for p in ok)
    medians = (f"<tr class='median'><th scope='row'>{tr('Group median')}</th><td></td><td></td><td></td>"
               f"<td>{per(r.medians.get('per'))}</td><td></td><td>{per(r.medians.get('per_fwd'))}</td>"
               f"<td></td><td></td></tr>")
    head = "".join(f"<th>{h}</th>" for h in (
        tr("Stock"), tr("Segment"), tr("Close (NT$)"), tr("EPS, TTM"), tr("P/E, TTM"), tr("Forward EPS"),
        tr("Forward P/E"), tr("EPS growth implied"), tr("Basis")))
    note = tr("Forward P/E = latest close ÷ forward EPS. Analysts’ estimates come from Claude’s web search, "
              "for the fiscal year shown. Otherwise forward EPS = trailing EPS × (1 + revenue growth over the "
              "last 3 months, limited to −50%…+100%), which assumes a constant net margin; loss-makers and "
              "financial companies get none.")
    table = (f"<p class='muted small'>{note}</p><div class='panel scroll'><table><thead><tr>{head}</tr></thead>"
             f"<tbody>{rows}{medians}</tbody></table></div>")

    pts = [(p, p.metrics["per"], p.forward["per"]) for p in ok
           if not _bad(p.metrics.get("per")) and p.metrics["per"] > 0
           and not _bad(p.forward.get("per")) and p.forward["per"] > 0]
    if not pts:
        return table, None
    pts.sort(key=lambda t: t[2])
    chart = {"y": [_label(p) for p, _, _ in pts], "ttm": [round(a, 1) for _, a, _ in pts],
             "fwd": [round(b, 1) for _, _, b in pts], "names": [tr("P/E, TTM"), tr("Forward P/E")]}
    chart_html = (f"<section class='chart' style='margin-top:16px'><h3>{tr('Trailing vs forward P/E')}</h3>"
                  f"<div class='unit'>{tr('× earnings, log scale · sorted by forward P/E')}</div>"
                  f"<div id='c_pe' class='plot' style='height:{28 * len(pts) + 80}px'></div></section>")
    return table + chart_html, chart


def _price_forecast(ok: list[StockProfile], link, tr: Translator) -> tuple[str, dict | None]:
    """Implied prices from the EPS path × the stock's own P/E band, with upside vs the latest close."""
    if ok and not any(p.price for p in ok):
        return f"<p class='muted'>{tr('This saved run is older than price forecasts. Run it again to see them.')}</p>", None
    have = [p for p in ok if p.price.get("years") and not _bad(p.price["years"][-1].get("base"))]
    if not have:
        return f"<p class='muted'>{tr('No company has both a positive EPS path and a P/E history.')}</p>", None
    n = len(have[0].price["years"])
    same = all([y["year"] for y in p.price["years"]] == [y["year"] for y in have[0].price["years"]] for p in have)
    years = [y["year"] for y in have[0].price["years"]]
    eps_heads = [tr("EPS FY{year}", year=y) for y in years] if same else [tr("EPS year +{n}", n=t + 1) for t in range(n)]
    last = tr("FY{year}", year=years[-1]) if same else tr("year +{n}", n=n)

    def eps_cell(y):
        mark = f"<sup title='{_e(tr('analyst estimate'))}'>A</sup>" if y["source"] == "analyst" else ""
        return f"<td>{_num(y['eps'], 2)}{mark}</td>"

    def pe_cell(pe):
        if pe["basis"] == "group":
            return f"<td title='{_e(tr('No P/E history; group median used'))}'>{pe['base']:.1f}×*</td>"
        title = (tr("5-year P/E: 25th percentile – 75th percentile, median {m:.1f}×", m=pe["base"])
                 if pe["basis"] == "history" else
                 tr("Today’s P/E {cur:.1f}× – the 5-year 25th percentile", cur=pe["base"]))
        return f"<td title='{_e(title)}'>{pe['low']:.1f}–{pe['high']:.1f}×</td>"

    peak_title = _e(tr("Trades below its usual P/E: the market may expect earnings to fall, as at a cyclical "
                       "peak. Today’s P/E is kept and EPS gets no revenue momentum, but these prices may "
                       "still be too high."))

    rows = []
    for p in sorted(have, key=lambda p: -p.price["upside"]):
        pf, fin = p.price, p.price["years"][-1]
        peak = f" <span class='warn' title='{peak_title}'>⚠</span>" if pf.get("peak") else ""
        rows.append(
            f"<tr><th scope='row'>{_stock_cell(p, link)}{peak}</th><td>{_num(pf['close'], 1)}</td>{pe_cell(pf['pe'])}"
            + "".join(eps_cell(y) for y in pf["years"])
            + f"<td>{_num(fin['low'], 0)}</td><td><b>{_num(fin['base'], 0)}</b></td><td>{_num(fin['high'], 0)}</td>"
            f"<td><b>{_pct(pf['upside'], True)}</b></td><td>{_pct(pf['annual'], True)}</td></tr>")
    head = "".join(f"<th>{h}</th>" for h in (
        tr("Stock"), tr("Close (NT$)"), tr("P/E band"), *eps_heads,
        tr("{year} low", year=last), tr("{year} base", year=last), tr("{year} high", year=last),
        tr("Base vs close"), tr("Annualized")))
    missing = [p for p in ok if p not in have]
    skipped = (f"<p class='muted small'>{tr('No implied price (loss-making, financial without estimates, or no P/E data): {names}.', names=', '.join(_label(p) for p in missing))}</p>"
               if missing else "")
    note = tr("Implied price = EPS for the fiscal year × the stock’s own P/E band over the last 5 years "
              "(25th percentile = low, median = base, 75th percentile = high, at most twice the median; "
              "* = group median, no history). "
              "EPS uses analysts’ estimates where Claude found them (A). Otherwise year 1 is the forward EPS "
              "above, and later years grow at the 6-year outlook of the company’s supply-chain segment, or "
              "the theme’s, kept within 0–15% a year. ⚠ marks a stock trading below its usual P/E, which "
              "often means the market expects earnings to fall: its base case keeps today’s P/E and its "
              "EPS gets no revenue momentum. Prices are for the end of each fiscal year and exclude "
              "dividends. They show what the price would be if earnings follow this path and the market "
              "values the stock as it has; they are not price targets.")
    peaks = [p for p in have if p.price.get("peak")]
    if peaks:
        note_peak = tr("{n} of {total} companies trade below their usual P/E (⚠): treat their implied prices "
                       "with extra caution.", n=len(peaks), total=len(have))
        note += " " + note_peak
    table = (f"<p class='muted small'>{_e(note)}</p><div class='panel scroll'><table><thead><tr>{head}</tr></thead>"
             f"<tbody>{''.join(rows)}</tbody></table></div>{skipped}")

    pts = sorted(have, key=lambda p: p.price["upside"])
    chart = {"y": [_label(p) for p in pts],
             "base": [round(p.price["upside"], 4) for p in pts],
             "low": [round(p.price["upside_low"], 4) if not _bad(p.price["upside_low"]) else None for p in pts],
             "high": [round(p.price["upside_high"], 4) if not _bad(p.price["upside_high"]) else None for p in pts],
             "prices": [[round(p.price["years"][-1][k], 1) for k in ("low", "base", "high")] + [round(p.price["close"], 1)]
                        for p in pts],
             "names": [tr("Base case"), tr("P/E band range")],
             "labels": [tr("Implied price"), tr("Close (NT$)")]}
    chart_html = (f"<section class='chart' style='margin-top:16px'><h3>"
                  f"{_e(tr('Implied price at the end of {year} vs the latest close', year=last))}</h3>"
                  f"<div class='unit'>{_e(tr('% above or below the close · line = P/E band, dot = median P/E'))}</div>"
                  f"<div id='c_price' class='plot' style='height:{28 * len(pts) + 80}px'></div></section>")
    return table + chart_html, chart


def _charts(ok: list[StockProfile], tr: Translator) -> list[dict]:
    charts = []
    for cid, title, key, unit in [("rev3m", tr("Revenue growth, last 3 months vs a year earlier"), "rev_3m_yoy",
                                   tr("% YoY")),
                                  ("gm", tr("Gross margin, latest quarter"), "gross_margin", tr("% of revenue"))]:
        rows = sorted(((p.metrics.get(key), _label(p)) for p in ok if not _bad(p.metrics.get(key))),
                      key=lambda t: t[0])
        if rows:
            charts.append({"id": cid, "title": title, "unit": unit,
                           "y": [lbl for _, lbl in rows], "x": [round(v, 4) for v, _ in rows]})
    return charts


def render(r: ThemeResult, nav: str = "", link: Callable[[str], str] | None = None) -> str:
    tr = translator(r.lang)
    ok = [p for p in r.profiles if p.metrics]
    failed = [p for p in r.profiles if not p.metrics]
    swot = r.swot or {}
    charts = _charts(ok, tr)
    seg_chart = _segment_chart(r, ok, tr)
    seg_chart_html = (
        f"<section class='chart' style='margin-top:16px'><h3>{tr('Revenue growth by supply-chain segment')}</h3>"
        f"<div class='unit'>{tr('% YoY, last 3 months · dots are companies, vertical ticks are segment medians')}</div>"
        f"<div id='c_seg' class='plot' style='height:{48 * len(seg_chart['order']) + 70}px'></div></section>"
    ) if seg_chart else ""
    chart_html = "".join(
        f"<section class='chart'><h3>{_e(c['title'])}</h3><div class='unit'>{_e(c['unit'])}</div>"
        f"<div id='c_{c['id']}' class='plot' style='height:{max(260, 26 * len(c['y']) + 60)}px'></div></section>"
        for c in charts
    )
    outlook_html, forecast_charts = _outlook(r, tr)
    valuation_html, pe_chart = _valuation(r, ok, link, tr)
    price_html, price_chart = _price_forecast(ok, link, tr)
    cards = "".join(_swot_card(p, swot.get(p.stock_id), r.news_days, link, tr) for p in ok)
    failed_html = (f"<h2>{tr('Skipped')}</h2><div class='panel'><ul>" + "".join(
        f"<li>{_e(p.stock_id)}: {_e(p.error)}</li>" for p in failed) + "</ul></div>") if failed else ""
    meta = tr("Keywords: {keywords} · {n} stocks · news from the last {days} day(s) · generated {time}",
              keywords=", ".join(r.theme.keywords), n=len(ok), days=r.news_days,
              time=r.generated.replace("T", " ") or f"{datetime.now():%Y-%m-%d %H:%M}")
    footer = " ".join([
        tr("Data: FinMind (TWSE / TPEx / MOPS filings, news headlines)."),
        tr("Research and SWOT: Claude with web search.") if r.research else "",
        tr("Forecasts and forward estimates are uncertain by nature; treat them as scenarios, not predictions."),
        tr("Data signals are rule-based and use thresholds such as ROE ≥ 15% and P/E percentile ≥ 80%."),
        tr("For research only; not investment advice."),
    ])

    return f"""<!DOCTYPE html>
<html lang="{HTML_LANG[normalize(r.lang)]}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(tr("{title} · Theme analysis", title=r.theme.title))}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>{BASE_CSS}{THEME_CSS}</style></head>
<body><div class="viz-root"><main>
{nav}
<h1>{_e(r.theme.title)}</h1>
<div class="meta">{_e(meta)}</div>

<h2>{tr('Overview')}</h2>
{_overview(r, tr)}

<h2>{tr('Supply chain')}</h2>
{_supply_chain(r, ok, link, tr)}
{seg_chart_html}

<h2>{tr('Market outlook')}</h2>
{outlook_html}

<h2>{tr('Comparison')}</h2>
<div class="grid">{chart_html}</div>
<div class="panel scroll" style="margin-top:16px">{_table(ok, r.medians, link, tr)}</div>

<h2>{tr('Forward valuation')}</h2>
{valuation_html}

<h2>{tr('Price forecast')}</h2>
{price_html}

<h2>{tr('SWOT by company')}</h2>
<div class="cards">{cards}</div>
{failed_html}

<footer>{_e(footer)}</footer>
</main></div>
<script>
const CHARTS = {json.dumps(charts, ensure_ascii=False).replace("</", "<\\/")};
const SEG = {json.dumps(seg_chart, ensure_ascii=False).replace("</", "<\\/")};
const FORECASTS = {json.dumps(forecast_charts, ensure_ascii=False).replace("</", "<\\/")};
const PE = {json.dumps(pe_chart, ensure_ascii=False).replace("</", "<\\/")};
const PRICE = {json.dumps(price_chart, ensure_ascii=False).replace("</", "<\\/")};
const THEMES = {{
  light: {{ bar: "#2a78d6", band: "rgba(42,120,214,0.16)", text: "#0b0b0b", muted: "#52514e", grid: "#e9e8e4",
           ref: "#7a7974", surface: "#fcfcfb" }},
  dark:  {{ bar: "#3987e5", band: "rgba(57,135,229,0.24)", text: "#f0efec", muted: "#c3c2b7", grid: "#2e2e2c",
           ref: "#8f8e86", surface: "#1a1a19" }},
}};
const mq = window.matchMedia("(prefers-color-scheme: dark)");
function draw() {{
  const t = mq.matches ? THEMES.dark : THEMES.light;
  for (const c of CHARTS) {{
    Plotly.react("c_" + c.id, [{{
      type: "bar", orientation: "h", x: c.x, y: c.y, marker: {{ color: t.bar }},
      hovertemplate: "%{{y}}<br>%{{x:.1%}}<extra></extra>",
    }}], {{
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: {{ color: t.muted, size: 12 }}, margin: {{ l: 120, r: 16, t: 8, b: 32 }},
      barcornerradius: 4, bargap: 0.3, showlegend: false,
      hoverlabel: {{ bgcolor: t.surface, bordercolor: t.grid, font: {{ color: t.text }} }},
      xaxis: {{ tickformat: ".0%", gridcolor: t.grid, zerolinecolor: t.ref }},
      yaxis: {{ type: "category", automargin: true, tickfont: {{ color: t.text }} }},
    }}, {{ displayModeBar: false, responsive: true }});
  }}
  if (SEG) {{
    Plotly.react("c_seg", [
      {{ type: "scatter", mode: "markers", name: SEG.names[0], x: SEG.x, y: SEG.y, text: SEG.text,
         marker: {{ color: t.bar, size: 10, line: {{ color: t.surface, width: 2 }} }},
         hovertemplate: "%{{text}}<br>%{{x:+.1%}}<extra></extra>" }},
      {{ type: "scatter", mode: "markers", name: SEG.names[1], x: SEG.mx, y: SEG.my,
         marker: {{ symbol: "line-ns-open", color: t.text, size: 22, line: {{ width: 2 }} }},
         hovertemplate: "%{{y}} " + SEG.median + "<br>%{{x:+.1%}}<extra></extra>" }},
    ], {{
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: {{ color: t.muted, size: 12 }}, margin: {{ l: 120, r: 16, t: 8, b: 32 }},
      showlegend: true, legend: {{ orientation: "h", x: 0, y: 1.02, yanchor: "bottom" }},
      hoverlabel: {{ bgcolor: t.surface, bordercolor: t.grid, font: {{ color: t.text }} }},
      xaxis: {{ tickformat: "+.0%", gridcolor: t.grid, zerolinecolor: t.ref }},
      yaxis: {{ type: "category", automargin: true, tickfont: {{ color: t.text }}, gridcolor: t.grid,
               categoryorder: "array", categoryarray: [...SEG.order].reverse() }},
    }}, {{ displayModeBar: false, responsive: true }});
  }}
  const base = (t) => ({{
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: {{ color: t.muted, size: 12 }}, showlegend: true,
    legend: {{ orientation: "h", x: 0, y: 1.02, yanchor: "bottom" }},
    hoverlabel: {{ bgcolor: t.surface, bordercolor: t.grid, font: {{ color: t.text }} }},
  }});
  for (const f of FORECASTS) {{
    const range = f.low.map((v, i) => [v, f.high[i]]);
    Plotly.react("c_" + f.id, [
      {{ type: "scatter", mode: "lines", x: f.x, y: f.high, line: {{ width: 0 }}, hoverinfo: "skip", showlegend: false }},
      {{ type: "scatter", mode: "lines", x: f.x, y: f.low, line: {{ width: 0 }}, fill: "tonexty", fillcolor: t.band,
         name: f.names[1], customdata: range,
         hovertemplate: f.names[1] + ": %{{customdata[0]:,.1f}} – %{{customdata[1]:,.1f}}<extra></extra>" }},
      {{ type: "scatter", mode: "lines+markers", x: f.x, y: f.base, name: f.names[0],
         line: {{ color: t.bar, width: 2 }}, marker: {{ color: t.bar, size: 8, line: {{ color: t.surface, width: 2 }} }},
         hovertemplate: f.names[0] + ": %{{y:,.1f}}<extra></extra>" }},
    ], {{
      ...base(t), margin: {{ l: 56, r: 16, t: 8, b: 32 }}, hovermode: "x unified",
      xaxis: {{ type: "category", gridcolor: t.grid, showgrid: false }},
      yaxis: {{ gridcolor: t.grid, zerolinecolor: t.ref, rangemode: "tozero", tickformat: ",.0f" }},
    }}, {{ displayModeBar: false, responsive: true }});
  }}
  if (PE) {{
    const lx = [], ly = [];
    PE.y.forEach((y, i) => {{ lx.push(PE.ttm[i], PE.fwd[i], null); ly.push(y, y, null); }});
    Plotly.react("c_pe", [
      {{ type: "scatter", mode: "lines", x: lx, y: ly, line: {{ color: t.ref, width: 2 }}, hoverinfo: "skip",
         showlegend: false }},
      {{ type: "scatter", mode: "markers", name: PE.names[0], x: PE.ttm, y: PE.y,
         marker: {{ symbol: "circle-open", color: t.text, size: 10, line: {{ width: 2 }} }},
         hovertemplate: "%{{y}}<br>" + PE.names[0] + " %{{x:.1f}}×<extra></extra>" }},
      {{ type: "scatter", mode: "markers", name: PE.names[1], x: PE.fwd, y: PE.y,
         marker: {{ color: t.bar, size: 11, line: {{ color: t.surface, width: 2 }} }},
         hovertemplate: "%{{y}}<br>" + PE.names[1] + " %{{x:.1f}}×<extra></extra>" }},
    ], {{
      ...base(t), margin: {{ l: 120, r: 16, t: 8, b: 32 }},
      xaxis: {{ type: "log", gridcolor: t.grid, ticksuffix: "×" }},
      yaxis: {{ type: "category", automargin: true, tickfont: {{ color: t.text }}, gridcolor: t.grid, categoryarray: PE.y,
               categoryorder: "array" }},
    }}, {{ displayModeBar: false, responsive: true }});
  }}
  if (PRICE) {{
    const lx = [], ly = [];
    PRICE.y.forEach((y, i) => {{
      if (PRICE.low[i] !== null && PRICE.high[i] !== null) {{ lx.push(PRICE.low[i], PRICE.high[i], null); ly.push(y, y, null); }}
    }});
    Plotly.react("c_price", [
      {{ type: "scatter", mode: "lines", name: PRICE.names[1], x: lx, y: ly, line: {{ color: t.bar, width: 6 }},
         opacity: 0.35, hoverinfo: "skip" }},
      {{ type: "scatter", mode: "markers", name: PRICE.names[0], x: PRICE.base, y: PRICE.y, customdata: PRICE.prices,
         marker: {{ color: t.bar, size: 11, line: {{ color: t.surface, width: 2 }} }},
         hovertemplate: "%{{y}}<br>" + PRICE.labels[0] + " NT$ %{{customdata[1]:,.0f}} (%{{x:+.0%}})<br>" +
           "%{{customdata[0]:,.0f}} – %{{customdata[2]:,.0f}}<br>" + PRICE.labels[1] + " %{{customdata[3]:,.1f}}<extra></extra>" }},
    ], {{
      ...base(t), margin: {{ l: 120, r: 16, t: 8, b: 32 }},
      xaxis: {{ tickformat: "+.0%", gridcolor: t.grid, zeroline: true, zerolinecolor: t.ref, zerolinewidth: 2 }},
      yaxis: {{ type: "category", automargin: true, tickfont: {{ color: t.text }}, gridcolor: t.grid,
               categoryarray: PRICE.y, categoryorder: "array" }},
    }}, {{ displayModeBar: false, responsive: true }});
  }}
}}
mq.addEventListener("change", draw);
draw();
</script>
</body></html>"""


THEME_CSS = """
.cols3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }
.cols3 ul, .card ul { margin: 6px 0 0; padding-left: 18px; }
.panel p { margin: 0 0 12px; }
details { margin-top: 12px; }
summary { cursor: pointer; color: var(--text-secondary); }
.notice { border-left: 3px solid var(--critical); padding-left: 10px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(520px, 1fr)); gap: 16px; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.card h3 a, table a { color: inherit; text-decoration: none; }
.card h3 a:hover, table a:hover { text-decoration: underline; }
.swot { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 12px; }
.quad { border: 1px solid var(--border); border-radius: 8px; padding: 10px; font-size: 13px; }
.quad h4 { margin: 0; font-size: 13px; }
.q-strengths h4::before { content: "S · "; } .q-weaknesses h4::before { content: "W · "; }
.q-opportunities h4::before { content: "O · "; } .q-threats h4::before { content: "T · "; }
.role { margin-top: 6px; }
.badge { display: inline-block; border: 1px solid var(--border); border-radius: 999px; padding: 0 8px;
  font-size: 12px; color: var(--text-secondary); }
.takeaway { margin: 12px 0 0; font-weight: 500; }
.warn { color: var(--critical); cursor: help; }
.news li { margin: 4px 0; }
.news a { color: var(--text-primary); }
table.left th, table.left td { text-align: left; }
td.wrap { white-space: normal; min-width: 280px; }
tr.median th, tr.median td { color: var(--text-secondary); font-style: italic; }
td.left, th.left { text-align: left; }
.flow { display: flex; gap: 8px; align-items: stretch; overflow-x: auto; }
.lane { flex: 1 1 0; min-width: 240px; display: flex; flex-direction: column; gap: 8px; }
.lane > h3 { margin: 0; font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--text-secondary); }
.arrow { align-self: center; font-size: 22px; color: var(--text-muted); }
.side { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-top: 16px; }
.side .lane { min-width: 0; }
.seg { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 12px; }
.seg h4 { margin: 0 0 2px; font-size: 14px; }
.segstats { margin-top: 6px; font-size: 12px; font-weight: 500; }
.members { list-style: none; padding: 0 !important; margin: 8px 0 0 !important; }
.members li { padding: 6px 0; border-top: 1px solid var(--border); font-size: 13px; }
.members a { color: inherit; text-decoration: none; } .members a:hover { text-decoration: underline; }
"""
