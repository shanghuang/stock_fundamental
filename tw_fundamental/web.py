"""Web UI: search for a stock and view its report.

    python -m tw_fundamental.web [--port 8000]
"""
from __future__ import annotations

import argparse
import html
import json
import re
from urllib.parse import urlencode

from flask import Flask, Response, g, redirect, request, url_for

from .finmind import FinMindError
from .i18n import DEFAULT_LANG, HTML_LANG, LANGS, normalize, translator
from .pipeline import STOCK_ID_RE, build_report, list_stocks
from .report import BASE_CSS, tr_market
from . import llm, theme, theme_report

app = Flask(__name__)

YEAR_CHOICES = (3, 5, 8, 10)
QUICK_PICKS = ("2330", "2317", "2454", "2308", "2412", "2882")
LANG_COOKIE = "lang"


def _lang() -> str:
    """?lang= (remembered in a cookie), then the cookie, then the browser's Accept-Language."""
    if "lang" not in g:
        chosen = request.args.get("lang") or request.cookies.get(LANG_COOKIE)
        if not chosen:
            chosen = request.accept_languages.best_match(["en", "zh-TW", "zh"]) or DEFAULT_LANG
        g.lang = normalize(chosen)
    return g.lang


def tr(msg: str, **kw) -> str:
    return translator(_lang())(msg, **kw)


@app.after_request
def _remember_lang(resp: Response) -> Response:
    if request.args.get("lang"):
        resp.set_cookie(LANG_COOKIE, _lang(), max_age=365 * 24 * 3600, samesite="Lax")
    return resp


def _lang_switch() -> str:
    """Links to the current page in each other language (drops one-shot args like refresh)."""
    links = []
    for code, label in LANGS.items():
        if code == _lang():
            continue
        args = {k: v for k, v in request.args.items() if k != "refresh"}
        args["lang"] = code
        links.append(f"<a class='lang' href='{html.escape(request.path + '?' + urlencode(args))}' "
                     f"hreflang='{HTML_LANG[code]}' lang='{HTML_LANG[code]}'>{html.escape(label)}</a>")
    return "".join(links)


def _years() -> int:
    try:
        y = int(request.args.get("years", 5))
    except ValueError:
        return 5
    return y if y in YEAR_CHOICES else 5


def _year_options(selected: int) -> str:
    return "".join(f"<option value='{y}'{' selected' if y == selected else ''}>{tr('{n} years', n=y)}</option>"
                   for y in YEAR_CHOICES)


def _nav(stock_id: str, years: int) -> str:
    sid = html.escape(stock_id)
    return f"""<nav class="nav">
  <a href="{url_for('index')}">{tr('← All stocks')}</a>
  <a href="{url_for('themes')}">{tr('Themes')}</a>
  <a href="{url_for('report_page', stock_id=stock_id, years=years, refresh=1)}" title="{tr('Re-download from FinMind')}">{tr('Refresh data')}</a>
  <form action="{url_for('report_query')}" method="get">
    <input name="id" value="{sid}" size="8" aria-label="{tr('Stock code')}" placeholder="{tr('Code')}">
    <select name="years" aria-label="{tr('History')}">{_year_options(years)}</select>
    <button type="submit">{tr('View')}</button>
  </form>
  {_lang_switch()}
</nav>"""


def _page(title: str, body: str, status: int = 200) -> Response:
    doc = f"""<!DOCTYPE html>
<html lang="{HTML_LANG[_lang()]}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{BASE_CSS}{_INDEX_CSS}</style></head>
<body><div class="viz-root"><main>{body}</main></div></body></html>"""
    return Response(doc, status=status, mimetype="text/html")


@app.get("/")
def index():
    try:
        stocks = list_stocks()
    except FinMindError as e:
        return _page(tr("Error"), f"<h1>{tr('Could not load the stock list')}</h1>"
                     f"<p class='muted'>{html.escape(str(e))}</p>", 502)
    by_id = {s["id"]: s for s in stocks}
    picks = "".join(
        f"<a class='chip' href='{url_for('report_page', stock_id=i)}'>{i} {html.escape(by_id[i]['name'])}</a>"
        for i in QUICK_PICKS if i in by_id
    )
    industries = sorted({part for s in stocks for part in s["industry"].split(" / ")})
    industry_opts = "".join(f"<option>{html.escape(i)}</option>" for i in industries)
    lang = _lang()
    i18n = {"matches": tr("{n} matches"), "showing": tr("{n} matches · showing first {limit}")}
    body = f"""
<nav class="nav"><a href="{url_for('themes')}">{tr('Themes and SWOT →')}</a>{_lang_switch()}</nav>
<h1>{tr('Taiwan stock fundamentals')}</h1>
<div class="meta">{tr('{n:,} listed and OTC companies · data from FinMind', n=len(stocks))}</div>

<div class="panel search">
  <input id="q" type="search" autofocus autocomplete="off"
         placeholder="{tr('Search by code or name, e.g. 2330 or 台積電')}" aria-label="{tr('Search stocks')}">
  <div class="filters">
    <select id="market" aria-label="{tr('Market')}">
      <option value="">{tr('All markets')}</option><option value="twse">{tr_market('twse', lang)}</option><option value="tpex">{tr_market('tpex', lang)}</option>
    </select>
    <select id="industry" aria-label="{tr('Industry')}"><option value="">{tr('All industries')}</option>{industry_opts}</select>
    <select id="years" aria-label="{tr('History')}">{_year_options(5)}</select>
  </div>
  <div class="chips"><span class="muted">{tr('Popular:')}</span> {picks}</div>
  <div class="chips" id="recent-row" hidden><span class="muted">{tr('Recent:')}</span> <span id="recent"></span></div>
</div>

<div class="muted count" id="count"></div>
<div class="panel scroll">
  <table id="results">
    <thead><tr><th>{tr('Code')}</th><th>{tr('Name')}</th><th>{tr('Industry')}</th><th>{tr('Market')}</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<div id="loading" hidden><div class="panel">{tr('Loading report… the first load of a stock takes a few seconds.')}</div></div>

<script>
const STOCKS = {json.dumps(stocks, ensure_ascii=False).replace("</", "<\\/")};
const MARKET = {json.dumps({m: tr_market(m, lang) for m in ("twse", "tpex")}, ensure_ascii=False)};
const I18N = {json.dumps(i18n, ensure_ascii=False).replace("</", "<\\/")};
const LIMIT = 100;
const $ = (id) => document.getElementById(id);

function reportUrl(id) {{ return "/report/" + encodeURIComponent(id) + "?years=" + $("years").value; }}

function rank(s, q) {{
  if (!q) return 0;
  if (s.id === q) return 0;
  if (s.id.startsWith(q)) return 1;
  const n = s.name.toLowerCase();
  if (n.startsWith(q)) return 2;
  if (n.includes(q)) return 3;
  return -1;
}}

function matches() {{
  const q = $("q").value.trim().toLowerCase();
  const m = $("market").value, ind = $("industry").value;
  return STOCKS
    .filter((s) => (!m || s.market === m) && (!ind || s.industry.split(" / ").includes(ind)))
    .map((s) => [rank(s, q), s]).filter(([r]) => r >= 0)
    .sort((a, b) => a[0] - b[0] || a[1].id.localeCompare(b[1].id))
    .map(([, s]) => s);
}}

function render() {{
  const list = matches();
  const tbody = $("results").tBodies[0];
  tbody.replaceChildren();
  for (const s of list.slice(0, LIMIT)) {{
    const tr = tbody.insertRow();
    const a = document.createElement("a");
    a.href = reportUrl(s.id); a.textContent = s.id; a.dataset.id = s.id;
    tr.insertCell().append(a);
    for (const v of [s.name, s.industry, MARKET[s.market] || s.market]) tr.insertCell().textContent = v;
    tr.addEventListener("click", (e) => {{ if (e.target.tagName !== "A") open_(s.id); }});
  }}
  const n = list.length.toLocaleString();
  $("count").textContent = list.length > LIMIT
    ? I18N.showing.replace("{{n}}", n).replace("{{limit}}", LIMIT)
    : I18N.matches.replace("{{n}}", n);
}}

function remember(id) {{
  const r = JSON.parse(localStorage.getItem("twf-recent") || "[]").filter((x) => x !== id);
  localStorage.setItem("twf-recent", JSON.stringify([id, ...r].slice(0, 8)));
}}

function showRecent() {{
  const byId = Object.fromEntries(STOCKS.map((s) => [s.id, s]));
  const ids = JSON.parse(localStorage.getItem("twf-recent") || "[]").filter((id) => byId[id]);
  $("recent-row").hidden = !ids.length;
  $("recent").replaceChildren(...ids.map((id) => {{
    const a = document.createElement("a");
    a.className = "chip"; a.href = reportUrl(id); a.dataset.id = id;
    a.textContent = id + " " + byId[id].name;
    return a;
  }}));
}}

function open_(id) {{ remember(id); $("loading").hidden = false; location.href = reportUrl(id); }}

document.addEventListener("click", (e) => {{
  const a = e.target.closest("a[href^='/report/']");
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
  e.preventDefault();
  open_(a.dataset.id || decodeURIComponent(a.pathname.split("/").pop()));
}});
$("q").addEventListener("input", render);
$("q").addEventListener("keydown", (e) => {{
  if (e.key !== "Enter") return;
  const first = matches()[0];
  if (first) open_(first.id);
}});
for (const id of ["market", "industry"]) $(id).addEventListener("change", render);
$("years").addEventListener("change", () => {{ render(); showRecent(); }});
window.addEventListener("pageshow", () => {{ $("loading").hidden = true; showRecent(); }});
render();
</script>"""
    return _page(tr("Taiwan stock fundamentals"), body)


@app.get("/report")
def report_query():
    # Accept "2330", "2330 台積電", etc.
    raw = request.args.get("id", "").strip()
    stock_id = raw.split()[0] if raw else ""
    if not stock_id:
        return redirect(url_for("index"))
    return redirect(url_for("report_page", stock_id=stock_id, years=_years()))


@app.get("/report/<stock_id>")
def report_page(stock_id: str):
    years = _years()
    if not STOCK_ID_RE.match(stock_id):
        return _error(stock_id, years, tr("“{code}” is not a valid stock code.", code=stock_id), 400)
    try:
        r = build_report(stock_id, years, refresh=request.args.get("refresh") == "1", nav=_nav(stock_id, years),
                         lang=_lang())
    except FinMindError as e:
        return _error(stock_id, years, str(e), 404 if "Unknown stock" in str(e) else 502)
    if request.args.get("refresh") == "1":
        return redirect(url_for("report_page", stock_id=stock_id, years=years))
    return Response(r.html, mimetype="text/html")


NEWS_DAYS = (1, 3, 7, 14)


@app.get("/themes")
def themes():
    presets = theme.load_presets(_lang())
    has_llm = llm.available()
    cards = "".join(
        f"""<a class="preset card" data-key="{html.escape(k)}" href="{url_for('theme_page', name=k)}">
  <h3>{html.escape(t.title)}</h3><p class="muted">{html.escape(t.description)}</p>
  <div class="small muted">{tr('{n} stocks', n=len(t.stock_ids))} · {html.escape(', '.join(t.keywords[:5]))}</div></a>"""
        for k, t in presets.items()
    )
    llm_opt = (f"<label><input type='checkbox' id='llm' checked> {tr('Claude research and SWOT')} "
               f"<span class='muted small'>{tr('(web search; billed to your Anthropic account)')}</span></label>"
               if has_llm else
               f"<label class='muted'><input type='checkbox' id='llm' disabled> {tr('Claude research and SWOT')} "
               f"{tr('(set ANTHROPIC_API_KEY to enable)')}</label>")
    day_opts = "".join(
        f"<option value='{d}'{' selected' if d == 3 else ''}>"
        f"{tr('1 day of news') if d == 1 else tr('{n} days of news', n=d)}</option>"
        for d in NEWS_DAYS)
    body = f"""
<nav class="nav"><a href="{url_for('index')}">{tr('← All stocks')}</a>{_lang_switch()}</nav>
<h1>{tr('Market themes')}</h1>
<div class="meta">{tr('Compare the stocks in a theme and get a SWOT for each one')}</div>

<div class="panel options">
  <select id="days" aria-label="{tr('News window')}">{day_opts}</select>
  {llm_opt}
  <div class="muted small">{tr('Without a FinMind token, scanning news costs one request per stock per day (about 300 requests per hour). Analysis takes 10–60 seconds; with Claude it takes 1–3 minutes.')}</div>
</div>

<h2>{tr('Presets')}</h2>
<div class="presets">{cards}</div>

<h2>{tr('Custom theme')}</h2>
<form class="panel custom" id="custom" action="{url_for('theme_page')}" method="get">
  <label>{tr('Theme name')} <input name="name" required maxlength="40" placeholder="{tr('e.g. 機器人, HBM, 重電')}"></label>
  <label>{tr('News keywords')} <input name="keywords" maxlength="300" placeholder="{tr('comma-separated, e.g. 機器人, 人形機器人, robot')}"></label>
  <label>{tr('Stock codes')} <input name="stocks" maxlength="200" placeholder="{tr('e.g. 2049 1590 4583 (optional when Claude research is on)')}"></label>
  <input type="hidden" name="days"><input type="hidden" name="llm">
  <button type="submit">{tr('Analyze')}</button>
</form>
<p class="muted small">{tr("You can also use a preset name with your own keywords or codes; they replace the preset's.")}</p>

<div id="loading" hidden><div class="panel">{tr('Analyzing theme… this can take a minute or more.')}</div></div>
<script>
const $ = (id) => document.getElementById(id);
const opts = () => ({{ days: $("days").value, llm: $("llm").checked && !$("llm").disabled ? "1" : "" }});
for (const a of document.querySelectorAll("a.preset")) {{
  a.addEventListener("click", (e) => {{
    if (e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    const q = new URLSearchParams({{ name: a.dataset.key, ...opts() }});
    $("loading").hidden = false;
    location.href = a.pathname + "?" + q;
  }});
}}
$("custom").addEventListener("submit", () => {{
  const o = opts();
  $("custom").elements.days.value = o.days;
  $("custom").elements.llm.value = o.llm;
  $("loading").hidden = false;
}});
window.addEventListener("pageshow", () => {{ $("loading").hidden = true; }});
</script>"""
    return _page(tr("Market themes"), body)


@app.get("/theme")
def theme_page():
    name = request.args.get("name", "").strip()[:40]
    if not name:
        return redirect(url_for("themes"))
    keywords = [k.strip()[:30] for k in re.split(r"[,，、]", request.args.get("keywords", "")) if k.strip()][:12]
    stocks = [s for s in re.split(r"[,，\s]+", request.args.get("stocks", "")) if s]
    bad = [s for s in stocks if not STOCK_ID_RE.match(s)]
    nav = (f"<nav class='nav'><a href='{url_for('index')}'>{tr('← All stocks')}</a>"
           f"<a href='{url_for('themes')}'>{tr('Themes')}</a>{_lang_switch()}</nav>")
    if bad:
        return _page(tr("Invalid input"), f"{nav}<h1>{tr('Invalid stock codes')}</h1><p class='muted'>"
                     f"{html.escape(', '.join(bad))}</p>", 400)
    try:
        days = int(request.args.get("days", 3))
    except ValueError:
        days = 3
    days = days if days in NEWS_DAYS else 3
    want_llm = request.args.get("llm") == "1"
    use_llm = want_llm and llm.available()

    t = theme.make_theme(name, keywords or None, stocks or None, lang=_lang())
    if not t.stock_ids and not use_llm:
        return _page(tr("No stocks"), f"{nav}<h1>{html.escape(name)}</h1><p class='muted'>"
                     f"{tr('This theme has no stocks. Enter stock codes, or turn on Claude research to discover them.')}"
                     "</p>", 400)
    result = theme.analyze(t, news_days=days, use_llm=use_llm, refresh=request.args.get("refresh") == "1",
                           lang=_lang())
    if want_llm and not use_llm:
        result.llm_error = tr("no Claude credentials found (set ANTHROPIC_API_KEY)")
    html_doc = theme_report.render(result, nav=nav, link=lambda sid: url_for("report_page", stock_id=sid))
    return Response(html_doc, mimetype="text/html")



def _error(stock_id: str, years: int, message: str, status: int) -> Response:
    body = f"{_nav(stock_id, years)}<h1>{tr('No report')}</h1><p class='muted'>{html.escape(message)}</p>"
    return _page(tr("No report"), body, status)


_INDEX_CSS = """
.search { margin-top: 20px; display: grid; gap: 12px; }
.search input[type=search] { font-size: 18px; padding: 10px 14px; width: 100%; }
.filters { display: flex; gap: 8px; flex-wrap: wrap; }
.chips { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.chips > span:not(.muted) { display: contents; }
.chips[hidden] { display: none; }
.chip { border: 1px solid var(--border); border-radius: 999px; padding: 2px 10px;
  color: var(--text-primary); text-decoration: none; background: var(--surface-0); }
.chip:hover { border-color: var(--text-secondary); }
.count { margin: 20px 0 8px; }
#results td, #results th { text-align: left; }
#results tbody tr { cursor: pointer; }
#results tbody tr:hover { background: var(--surface-0); }
#results a { color: inherit; font-weight: 600; text-decoration: none; }
#loading { position: fixed; inset: 0; display: grid; place-items: center; background: rgb(0 0 0 / 0.35); }
#loading[hidden] { display: none; }
.options { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; margin-top: 20px; }
.options .small { flex-basis: 100%; }
.presets { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
.preset { display: block; color: inherit; text-decoration: none; background: var(--surface-1);
  border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.preset:hover { border-color: var(--text-secondary); }
.preset h3 { margin: 0 0 6px; }
.preset p { margin: 0 0 8px; }
.custom { display: grid; gap: 10px; max-width: 720px; }
.custom label { display: grid; gap: 4px; color: var(--text-secondary); }
.custom button { justify-self: start; }
.small { font-size: 12px; }
.nav .lang { margin-left: auto; border: 1px solid var(--border); border-radius: 999px; padding: 0 10px; }
.nav form + .lang { margin-left: 0; }
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Web UI for Taiwan stock fundamental reports.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
