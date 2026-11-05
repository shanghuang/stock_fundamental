"""Web UI: search for a stock and view its report. Every page needs a signed-in account (users.py).

    python -m tw_fundamental.web [--port 8000]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode

from flask import Flask, Response, flash, g, get_flashed_messages, redirect, request, session, url_for
from flask.sessions import SecureCookieSessionInterface

from .finmind import FinMindError
from .i18n import DEFAULT_LANG, HTML_LANG, LANGS, normalize, translator
from .pipeline import STOCK_ID_RE, build_report, list_stocks
from .report import BASE_CSS, tr_market
from . import activity, llm, theme, theme_report, theme_store, users, wishlist

class _SessionInterface(SecureCookieSessionInterface):
    """Loads the signing key on first use; Flask opens the session before any before_request hook runs."""

    def open_session(self, app, request):
        if not app.secret_key:
            app.secret_key = users.secret_key()
        return super().open_session(app, request)


app = Flask(__name__)
app.session_interface = _SessionInterface()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set TWF_SECURE_COOKIES=1 when the site is served over HTTPS.
    SESSION_COOKIE_SECURE=os.environ.get("TWF_SECURE_COOKIES") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
PUBLIC_ENDPOINTS = {"login", "login_guest", "register", "static"}
# Guests can read stock reports and saved analyses, and nothing else.
GUEST_ENDPOINTS = {"index", "report_query", "report_page", "themes", "theme_page", "saved_list", "saved_view"}
MAX_FAILED_LOGINS, LOCKOUT_SECONDS = 5, 15 * 60
_failed_logins: dict[str, list[float]] = {}

YEAR_CHOICES = (3, 5, 8, 10)
QUICK_PICKS = ("2330", "2317", "2454", "2308", "2412", "2882")
LANG_COOKIE = "lang"
RECENT_SAVED = 5


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


# --------------------------------------------------------------------------- accounts

def _user() -> users.User | None:
    return g.get("user")


def _is_admin() -> bool:
    """Has admin rights: admins, and privileged users, who have the same rights."""
    return bool(_user() and _user().has_admin_rights)


def _is_guest() -> bool:
    return bool(_user() and _user().is_guest)


def _log(action: str, detail: str = "") -> None:
    activity.log(_user(), action, detail)


def _csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def _csrf_field() -> str:
    return f"<input type='hidden' name='csrf' value='{_csrf_token()}'>"


@app.before_request
def _authenticate():
    g.user = None
    uid = session.get("uid")
    if uid is not None:
        u = users.get(uid)
        # A password change, reset or disable bumps session_version and so ends old sessions.
        if u and u.is_active and session.get("sv") == u.session_version:
            g.user = u
        else:
            session.clear()
    elif session.get("guest"):
        g.user = users.GUEST
    if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
        return _page(tr("Error"), f"<h1>{tr('This form has expired')}</h1><p class='muted'>"
                     f"{tr('Reload the page and try again.')}</p>", 400)
    if g.user is None and request.endpoint not in PUBLIC_ENDPOINTS:
        target = request.full_path.rstrip("?")
        return redirect(url_for("login", next=target) if target != "/" else url_for("login"))
    if _is_guest() and request.endpoint and request.endpoint not in PUBLIC_ENDPOINTS | GUEST_ENDPOINTS:
        return _forbidden(tr("Guests can read stock reports and saved analyses. Sign in or create an account to do more."))
    return None


def _sign_in(u: users.User) -> None:
    session.clear()  # drop anything set before sign-in, so a planted session cannot carry over
    session.permanent = True
    session.update(uid=u.id, sv=u.session_version, csrf=secrets.token_urlsafe(24))


def _sign_in_guest() -> None:
    session.clear()
    session.update(guest=True, csrf=secrets.token_urlsafe(24))  # ends when the browser closes


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


def _throttled(keys: list[str]) -> bool:
    now = time.time()
    limited = False
    for k in keys:
        recent = [t for t in _failed_logins.get(k, []) if now - t < LOCKOUT_SECONDS]
        _failed_logins[k] = recent
        limit = MAX_FAILED_LOGINS if "|" in k else MAX_FAILED_LOGINS * 4  # per account+IP, per IP
        limited |= len(recent) >= limit
    return limited


def _user_menu() -> str:
    u = _user()
    if u is None:
        return ""
    if u.is_guest:
        return (f"<span class='who'><span>{tr('Guest')}</span><a href='{url_for('login')}'>{tr('Sign in')}</a>"
                f"<a href='{url_for('register')}'>{tr('Create account')}</a></span>")
    admin = ""
    if u.has_admin_rights:
        n = wishlist.pending_count()
        badge = f" <span class='badge'>{n}</span>" if n else ""
        admin = f"<a href='{url_for('admin_panel')}'>{tr('Admin panel')}{badge}</a>"
    return (f"<span class='who'><a href='{url_for('account')}' title='{tr('Account')}'>{html.escape(u.username)}</a>"
            f"{admin}<form method='post' action='{url_for('logout')}' class='inline'>{_csrf_field()}"
            f"<button type='submit' class='link'>{tr('Sign out')}</button></form></span>")


def _nav_right() -> str:
    return _user_menu() + _lang_switch() + _notices()


def _notice_items(u: users.User) -> str:
    items = []
    for r in wishlist.notifications(u.id):
        topic = html.escape(r.topic)
        if r.status == "done":
            items.append(f"<li>{tr('The topic you requested, “{topic}”, has been analyzed.', topic=topic)} "
                         f"<a href='{url_for('request_open', req_id=r.id)}'>{tr('Open analysis')}</a></li>")
        else:
            reply = f" <span class='muted'>{html.escape(r.reply)}</span>" if r.reply else ""
            items.append(f"<li>{tr('Your request for “{topic}” was declined.', topic=topic)}{reply}</li>")
    return "".join(items)


def _notice_pop(u: users.User, nxt: str) -> str:
    items = _notice_items(u)
    if not items:
        return ""
    return (f"<div class='notice-pop' role='status' aria-labelledby='notice-title'>"
            f"<b id='notice-title'>{tr('Topic requests')}</b><ul>{items}</ul>"
            f"<form method='post' action='{url_for('requests_seen')}' class='inline'>{_csrf_field()}"
            f"<input type='hidden' name='next' value='{html.escape(nxt)}'>"
            f"<button type='submit'>{tr('Dismiss')}</button></form></div>")


def _notices() -> str:
    """Pop-up for decided topic requests, checked every minute so it also appears on an open page."""
    u = _user()
    if u is None or u.is_guest:
        return ""
    poll = (f"<script>(() => {{ const box = document.getElementById('notices'); if (!box || box.dataset.on) return;"
            f" box.dataset.on = 1; setInterval(async () => {{ if (document.hidden || box.firstChild) return;"
            f" const q = new URLSearchParams({{ next: location.pathname + location.search }});"
            f" try {{ const r = await fetch({json.dumps(url_for('requests_notices'))} + '?' + q);"
            f" if (r.ok) box.innerHTML = await r.text(); }} catch (e) {{}} }}, 60000); }})();</script>")
    return f"<div id='notices'>{_notice_pop(u, request.full_path.rstrip('?'))}</div>{poll}"


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
        args = {k: v for k, v in request.args.items() if k not in ("refresh", "rerun", "req")}
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
  {_nav_right()}
</nav>"""


def _page(title: str, body: str, status: int = 200) -> Response:
    notes = "".join(f"<div class='flash {html.escape(cat)}'>{html.escape(msg)}</div>"
                    for cat, msg in get_flashed_messages(with_categories=True))
    if notes:
        # After the nav when there is one, so messages sit above the page heading.
        end = body.find("</nav>")
        body = body[:end + 6] + notes + body[end + 6:] if end >= 0 else notes + body
    doc = f"""<!DOCTYPE html>
<html lang="{HTML_LANG[_lang()]}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{BASE_CSS}{_INDEX_CSS}</style></head>
<body><div class="viz-root"><main>{body}</main></div></body></html>"""
    return Response(doc, status=status, mimetype="text/html")


def _theme_nav() -> str:
    return (f"<nav class='nav'><a href='{url_for('index')}'>{tr('← All stocks')}</a>"
            f"<a href='{url_for('themes')}'>{tr('Themes')}</a>"
            f"<a href='{url_for('saved_list')}'>{tr('Saved analyses')}</a>{_nav_right()}</nav>")


def _fmt_time(iso: str) -> str:
    return iso.replace("T", " ")[:16]


def _saved_table(items: list[dict]) -> str:
    def pct(v):
        return "–" if v is None or v != v else f"{v:+.1%}"

    rows = []
    for m in items:
        cagr = pct(m.get("cagr"))
        if cagr != "–":
            cagr += " " + (tr("(market)") if m.get("cagr_source") == "claude" else tr("(tracked)"))
        per = m.get("per_fwd")
        owner = m.get("owner", "")
        can_delete = _is_admin() or (owner and _user() and owner.lower() == _user().username.lower())
        delete = (f"<form method='post' action='{url_for('saved_delete', rid=m['id'])}' class='inline' "
                  f"onsubmit=\"return confirm({html.escape(json.dumps(tr('Delete this saved analysis?')))})\">"
                  f"{_csrf_field()}<button type='submit' class='link'>{tr('Delete')}</button></form>"
                  if can_delete else "")
        warn = f" <span class='muted' title='{tr('Some Claude steps failed')}'>⚠</span>" if m.get("warnings") else ""
        rows.append(
            f"<tr><td>{html.escape(_fmt_time(m['created']))}</td>"
            f"<td><a href='{url_for('saved_view', rid=m['id'])}'>{html.escape(m['title'])}</a>{warn}"
            f"<div class='small muted'>{html.escape(', '.join(m.get('keywords', [])[:6]))}</div></td>"
            f"<td>{m['stocks']}</td><td>{m['news_days']}</td>"
            f"<td>{tr('Yes') if m.get('claude') else tr('No')}</td>"
            f"<td>{html.escape(LANGS.get(m.get('lang'), m.get('lang', '')))}</td>"
            f"<td>{cagr}</td><td>{'–' if per is None or per != per else f'{per:.1f}×'}</td>"
            f"<td>{html.escape(owner) or '–'}</td><td>{delete}</td></tr>")
    head = "".join(f"<th>{h}</th>" for h in (
        tr("Generated"), tr("Theme"), tr("Stocks"), tr("News days"), tr("Claude"), tr("Language"),
        tr("6-year CAGR"), tr("Median forward P/E"), tr("By"), ""))
    return (f"<div class='panel scroll'><table id='saved'><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


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
<nav class="nav"><a href="{url_for('themes')}">{tr('Themes and SWOT →')}</a>{_nav_right()}</nav>
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
                         lang=_lang(), saved_url=lambda rid: url_for("saved_view", rid=rid))
    except FinMindError as e:
        return _error(stock_id, years, str(e), 404 if "Unknown stock" in str(e) else 502)
    if request.args.get("refresh") == "1":
        return redirect(url_for("report_page", stock_id=stock_id, years=years))
    _log("view-report", stock_id)
    return Response(r.html, mimetype="text/html")


NEWS_DAYS = (1, 3, 7, 14)


@app.get("/themes")
def themes():
    presets = theme.load_presets(_lang())
    has_llm = llm.available() and _is_admin()
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
               f"{tr('(set ANTHROPIC_API_KEY to enable)') if _is_admin() else tr('(admins only)')}</label>")
    day_opts = "".join(
        f"<option value='{d}'{' selected' if d == 3 else ''}>"
        f"{tr('1 day of news') if d == 1 else tr('{n} days of news', n=d)}</option>"
        for d in NEWS_DAYS)
    saved = theme_store.list_saved()
    recent_html = ""
    if saved:
        more = (f"<p><a href='{url_for('saved_list')}'>{tr('All saved analyses ({n})', n=len(saved))} →</a></p>"
                if len(saved) > RECENT_SAVED else "")
        recent_html = (f"<h2>{tr('Recent analyses')}</h2>"
                       f"<p class='muted small'>{tr('Saved runs open instantly, without new data requests or Claude charges.')}</p>"
                       f"{_saved_table(saved[:RECENT_SAVED])}{more}")
    head = f"""
{_theme_nav()}
<h1>{tr('Market themes')}</h1>
<div class="meta">{tr('Compare the stocks in a theme and get a SWOT for each one')}</div>
"""
    if _is_guest():
        return _page(tr("Market themes"), f"""{head}
<div class="panel" style="margin-top:20px">{tr('As a guest you can open saved analyses. To start an analysis or request a topic, <a href="{login}">sign in</a> or <a href="{register}">create an account</a>.', login=url_for('login'), register=url_for('register'))}</div>

<h2>{tr('Presets')}</h2>
<div class="presets">{cards}</div>
{recent_html}""")
    body = f"""{head}
<div class="panel options">
  <select id="days" aria-label="{tr('News window')}">{day_opts}</select>
  {llm_opt}
  <div class="muted small">{tr('Without a FinMind token, scanning news costs one request per stock per day (about 300 requests per hour). Analysis takes 10–60 seconds; with Claude it takes 1–3 minutes.')}</div>
</div>

<h2>{tr('Presets')}</h2>
<div class="presets">{cards}</div>
{recent_html}

<h2>{tr('Custom theme')}</h2>
<form class="panel custom" id="custom" action="{url_for('theme_page')}" method="get">
  <label>{tr('Theme name')} <input name="name" required maxlength="40" placeholder="{tr('e.g. 機器人, HBM, 重電')}"></label>
  <label>{tr('News keywords')} <input name="keywords" maxlength="300" placeholder="{tr('comma-separated, e.g. 機器人, 人形機器人, robot')}"></label>
  <label>{tr('Stock codes')} <input name="stocks" maxlength="200" placeholder="{tr('e.g. 2049 1590 4583 (optional when Claude research is on)')}"></label>
  <input type="hidden" name="days"><input type="hidden" name="llm">
  <button type="submit">{tr('Analyze')}</button>
</form>
<p class="muted small">{tr("You can also use a preset name with your own keywords or codes; they replace the preset's.")}</p>
{_request_section()}

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


def _request_status(r: wishlist.TopicRequest) -> str:
    if r.status == "done":
        link = (f" · <a href='{url_for('saved_view', rid=r.saved_id)}'>{tr('Open analysis')}</a>"
                if r.saved_id else "")
        return f"{tr('Analyzed')}{link}"
    if r.status == "declined":
        reply = f"<div class='small muted'>{html.escape(r.reply)}</div>" if r.reply else ""
        return f"{tr('Declined')}{reply}"
    return tr("Waiting for an admin")


def _request_section() -> str:
    """Themes page: ask for a topic (others) or a link to the queue (admins)."""
    if _is_admin():
        n = wishlist.pending_count()
        return (f"<h2 id='requests'>{tr('Topic requests')}</h2><p class='muted'>"
                f"{tr('Waiting for review: {n}.', n=n) if n else tr('No topic requests are waiting.')} "
                f"<a href='{url_for('admin_requests')}'>{tr('Review requests')} →</a></p>")
    # Only open requests: a decided one leaves this list and reaches the user as a notification.
    mine = wishlist.list_requests(user_id=_user().id, status="pending")
    rows = []
    for r in mine:
        withdraw = (f"<form method='post' action='{url_for('request_withdraw', req_id=r.id)}' class='inline'>"
                    f"{_csrf_field()}<button type='submit' class='link'>{tr('Withdraw')}</button></form>")
        note = f"<div class='small muted'>{html.escape(r.note)}</div>" if r.note else ""
        rows.append(f"<tr><td><b>{html.escape(r.topic)}</b>{note}</td><td>{_fmt_time(r.created)}</td>"
                    f"<td>{_request_status(r)}</td><td>{withdraw}</td></tr>")
    head = "".join(f"<th>{h}</th>" for h in (tr("Topic"), tr("Requested"), tr("Status"), ""))
    table = (f"<h3>{tr('Your open requests')}</h3><div class='panel scroll'><table id='requests-mine'>"
             f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>") if rows else ""
    return f"""<h2 id="requests">{tr('Request a topic')}</h2>
<p class="muted small">{tr('Ask an admin to analyze a theme, with Claude research. You get a notice here when it is ready.')}</p>
<form class="panel custom" method="post" action="{url_for('request_submit')}">{_csrf_field()}
  <label>{tr('Topic')} <input name="topic" required maxlength="{wishlist.MAX_TOPIC}" placeholder="{tr('e.g. 機器人, HBM, 重電')}"></label>
  <label>{tr('Note for the admin (optional)')} <textarea name="note" rows="2" maxlength="{wishlist.MAX_NOTE}" placeholder="{tr('Companies or angles you are interested in')}"></textarea></label>
  <button type="submit">{tr('Send request')}</button>
</form>
{table}"""


@app.post("/requests")
def request_submit():
    try:
        r = wishlist.submit(_user(), request.form.get("topic", ""), request.form.get("note", ""))
    except users.UserError as e:
        flash(_user_error(e), "error")
    else:
        _log("request-topic", r.topic)
        flash(tr("Request for “{topic}” sent. You will be notified when an admin decides.", topic=r.topic), "ok")
    return redirect(url_for("themes", _anchor="requests"), code=303)


@app.post("/requests/<int:req_id>/withdraw")
def request_withdraw(req_id: int):
    r = wishlist.get(req_id)
    if wishlist.withdraw(_user().id, req_id):
        _log("withdraw-request", r.topic)
        flash(tr("Request withdrawn."), "ok")
    return redirect(url_for("themes", _anchor="requests"), code=303)


@app.get("/requests/<int:req_id>/open")
def request_open(req_id: int):
    r = wishlist.get(req_id)
    if r is None or r.user_id != _user().id:
        return _page(tr("Not found"), f"{_theme_nav()}<h1>{tr('No such request')}</h1>", 404)
    wishlist.mark_seen(r.user_id, r.id)
    return redirect(url_for("saved_view", rid=r.saved_id) if r.saved_id else url_for("themes", _anchor="requests"))


@app.post("/requests/seen")
def requests_seen():
    wishlist.mark_seen(_user().id)
    return redirect(_safe_next(request.form.get("next")), code=303)


@app.get("/requests/notices")
def requests_notices():
    return Response(_notice_pop(_user(), _safe_next(request.args.get("next"))), mimetype="text/html")


@app.get("/theme")
def theme_page():
    name = request.args.get("name", "").strip()[:40]
    if not name:
        return redirect(url_for("themes"))
    keywords = [k.strip()[:30] for k in re.split(r"[,，、]", request.args.get("keywords", "")) if k.strip()][:12]
    stocks = [s for s in re.split(r"[,，\s]+", request.args.get("stocks", "")) if s]
    bad = [s for s in stocks if not STOCK_ID_RE.match(s)]
    nav = _theme_nav()
    if bad:
        return _page(tr("Invalid input"), f"{nav}<h1>{tr('Invalid stock codes')}</h1><p class='muted'>"
                     f"{html.escape(', '.join(bad))}</p>", 400)
    try:
        days = int(request.args.get("days", 3))
    except ValueError:
        days = 3
    days = days if days in NEWS_DAYS else 3
    want_llm = request.args.get("llm") == "1"
    use_llm = want_llm and llm.available() and _is_admin()

    t = theme.make_theme(name, keywords or None, stocks or None, lang=_lang())
    if _is_guest():  # reads only: the newest saved run of the theme, never a new one
        latest = theme_store.latest_for(t.key, _lang())
        if latest:
            return _saved_response(latest["id"])
        return _forbidden(tr("“{name}” has no saved analysis yet, and guests cannot start one.", name=name))
    if not t.stock_ids and not use_llm:
        return _no_stocks_page(name, keywords, days)
    refresh = request.args.get("refresh") == "1"
    key = theme_store.params_key(t, days, use_llm, _lang())
    if not refresh and request.args.get("rerun") != "1":
        recent = theme_store.find_recent(key)
        if recent:
            return _saved_response(recent["id"], reused=True, fulfilled=_fulfill_request(recent["id"], t))
    query = {"name": t.key, "days": days, "llm": "1" if use_llm else ""}
    if keywords:
        query["keywords"] = ", ".join(keywords)
    if stocks:
        query["stocks"] = " ".join(stocks)
    result = theme.analyze(t, news_days=days, use_llm=use_llm, refresh=refresh, lang=_lang())
    if want_llm and not use_llm:
        result.llm_error = (tr("Claude research is available to admins only") if not _is_admin()
                            else tr("no Claude credentials found (set ANTHROPIC_API_KEY)"))
    rid = theme_store.save(result, key, query, owner=_user().username)
    _log("run-theme", f"{t.title} · Claude" if use_llm else t.title)
    html_doc = theme_report.render(result, nav=nav + _fulfilled_banner(_fulfill_request(rid, t)),
                                   link=lambda sid: url_for("report_page", stock_id=sid))
    return Response(html_doc, mimetype="text/html")


def _no_stocks_page(name: str, keywords: list[str], days: int) -> Response:
    """A theme that is not a preset needs stock codes or Claude: suggest codes and offer the options."""
    try:
        listed = list_stocks()
    except FinMindError:
        listed = []
    found = theme.suggest_stocks([name, *keywords], listed)
    if not _is_admin():
        why = tr("“{name}” is not a preset, and only admins can have Claude find the stocks for a theme.", name=name)
    elif not llm.available():
        why = tr("“{name}” is not a preset, and Claude research is off (set ANTHROPIC_API_KEY to let Claude "
                 "find the stocks).", name=name)
    else:
        why = tr("“{name}” is not a preset. Turn on Claude research to find its stocks, or enter codes.", name=name)
    if found:
        chips = "".join(f"<span class='chip'>{html.escape(s['id'])} {html.escape(s['name'])}</span>" for s in found)
        hint = (f"<p>{tr('Companies whose name or industry matches “{name}”:', name=name)}</p>"
                f"<div class='chips'>{chips}</div><p class='muted small'>"
                f"{tr('They are filled in below. Remove any that do not fit, or add your own.')}</p>")
    else:
        hint = f"<p class='muted'>{tr('No company names or industries match it. Enter the stock codes yourself.')}</p>"
    req = request.args.get("req", "")
    extra = (f"<input type='hidden' name='req' value='{html.escape(req)}'>" if req.isdigit() else "")
    llm_box = ("<label><input type='checkbox' name='llm' value='1'> " + tr("Claude research and SWOT") + "</label>"
               if _is_admin() and llm.available() else "")
    form = f"""<form class="panel custom" action="{url_for('theme_page')}" method="get">{extra}
  <input type="hidden" name="days" value="{days}">
  <label>{tr('Theme name')} <input name="name" required maxlength="40" value="{html.escape(name)}"></label>
  <label>{tr('News keywords')} <input name="keywords" maxlength="300" value="{html.escape(', '.join(keywords))}" placeholder="{tr('comma-separated, e.g. 機器人, 人形機器人, robot')}"></label>
  <label>{tr('Stock codes')} <input name="stocks" maxlength="200" value="{' '.join(s['id'] for s in found)}" placeholder="{tr('e.g. 2049 1590 4583 (optional when Claude research is on)')}"></label>
  {llm_box}<button type="submit">{tr('Analyze')}</button>
</form>"""
    ask = ""
    if not _is_admin():
        ask = (f"<h2>{tr('Or ask an admin')}</h2><form class='panel custom' method='post' "
               f"action='{url_for('request_submit')}'>{_csrf_field()}"
               f"<input type='hidden' name='topic' value='{html.escape(name)}'>"
               f"<p class='muted small'>{tr('An admin can run “{name}” with Claude research; you get a notice when it is ready.', name=name)}</p>"
               f"<button type='submit'>{tr('Request “{name}”', name=name)}</button></form>")
    presets = ", ".join(f"<a href='{url_for('theme_page', name=k, days=days)}'>{html.escape(p.title)}</a>"
                        for k, p in theme.load_presets(_lang()).items())
    body = (f"{_theme_nav()}<h1>{html.escape(name)}</h1><div class='meta'>{html.escape(why)}</div>"
            f"<div style='margin-top:20px'>{hint}</div>{form}{ask}"
            f"<p class='muted small' style='margin-top:20px'>{tr('Presets')}: {presets}</p>")
    return _page(tr("No stocks"), body, 400)


def _fulfill_request(rid: str, t: theme.Theme) -> list[int]:
    """When an admin runs a theme, mark the requests for it analyzed: the one being answered
    (?req=<id>, with its same-topic duplicates) and any request whose topic resolves to this theme,
    e.g. a request for "DRAM" when the memory preset runs."""
    if not _is_admin():
        return []
    admin = _user().username
    ids = []
    if request.args.get("req", "").isdigit():
        ids += wishlist.fulfill(int(request.args["req"]), rid, admin)
    key = t.key.lower()
    ids += wishlist.fulfill_matching(
        lambda topic: topic.strip().lower() in (key, t.title.lower())
        or theme.make_theme(topic, lang=_lang()).key.lower() == key, rid, admin)
    return ids


def _fulfilled_banner(ids: list[int]) -> str:
    if not ids:
        return ""
    names = sorted({r.username for r in map(wishlist.get, ids) if r})
    return (f"<div class='panel' style='margin-bottom:20px'>"
            f"{html.escape(tr('Request marked as analyzed. Notified: {names}.', names=', '.join(names)))} "
            f"<a href='{url_for('admin_requests')}' style='color:inherit;font-weight:600;margin-left:4px'>"
            f"{tr('Back to requests')}</a></div>")


def _saved_response(rid: str, reused: bool = False, fulfilled: list[int] | None = None) -> Response:
    loaded = theme_store.load(rid)
    if loaded is None:
        return _page(tr("Not found"), f"{_theme_nav()}<h1>{tr('Saved analysis not found')}</h1>", 404)
    meta, result = loaded
    _log("view-saved", meta["title"])
    query = {k: v for k, v in meta.get("query", {}).items() if v not in ("", None)}
    rerun = url_for("theme_page", **query, lang=result.lang, rerun=1)
    note = (tr("Showing the saved run from {time}, because the same analysis ran less than {hours} hours ago.",
               time=_fmt_time(meta["created"]), hours=theme_store.MAX_AGE_HOURS) if reused
            else tr("Saved analysis from {time}.", time=_fmt_time(meta["created"])))
    # Rendered inside the report template, which has its own CSS, so style inline.
    again = ("" if _is_guest() else f"<a href='{html.escape(rerun)}' style='color:inherit;font-weight:600;margin-left:4px'>"
             f"{tr('Run again with fresh data')}</a>")
    banner = f"<div class='panel' style='margin-bottom:20px'>{html.escape(note)} {again}</div>"
    html_doc = theme_report.render(result, nav=_theme_nav() + _fulfilled_banner(fulfilled or []) + banner,
                                   link=lambda sid: url_for("report_page", stock_id=sid))
    return Response(html_doc, mimetype="text/html")


@app.get("/themes/saved")
def saved_list():
    saved = theme_store.list_saved()
    table = _saved_table(saved) if saved else f"<p class='muted'>{tr('No saved analyses yet.')}</p>"
    body = (f"{_theme_nav()}<h1>{tr('Saved analyses')}</h1>"
            f"<div class='meta'>{tr('Every theme analysis is saved. The newest {n} are kept.', n=theme_store.KEEP)}</div>"
            f"<div style='margin-top:20px'>{table}</div>")
    return _page(tr("Saved analyses"), body)


@app.get("/theme/saved/<rid>")
def saved_view(rid: str):
    return _saved_response(rid)


@app.post("/theme/saved/<rid>/delete")
def saved_delete(rid: str):
    meta = theme_store.get_meta(rid)
    if meta is None:
        return _page(tr("Not found"), f"{_theme_nav()}<h1>{tr('Saved analysis not found')}</h1>", 404)
    if not (_is_admin() or meta.get("owner", "").lower() == _user().username.lower()):
        return _forbidden(tr("Only the person who ran an analysis, or an admin, can delete it."))
    theme_store.delete(rid)
    _log("delete-saved", meta.get("title", rid))
    return redirect(url_for("saved_list"), code=303)


# --------------------------------------------------------------------------- account pages

def _auth_nav() -> str:
    return f"<nav class='nav'><span class='brand'>{tr('Taiwan stock fundamentals')}</span>{_lang_switch()}</nav>"


def _auth_page(title: str, form: str, error: str = "", status: int = 200) -> Response:
    err = f"<div class='flash error'>{html.escape(error)}</div>" if error else ""
    return _page(title, f"{_auth_nav()}<div class='auth panel'><h1>{html.escape(title)}</h1>{err}{form}</div>",
                 status)


def _forbidden(message: str) -> Response:
    return _page(tr("Not allowed"), f"{_theme_nav()}<h1>{tr('Not allowed')}</h1>"
                 f"<p class='muted'>{html.escape(message)}</p>", 403)


def _user_error(e: users.UserError) -> str:
    return tr(e.message, **e.kw)


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = request.values.get("next", "")
    error, username = "", ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()[:64]
        keys = [f"{username.lower()}|{request.remote_addr}", f"ip:{request.remote_addr}"]
        if _throttled(keys):
            error = tr("Too many failed sign-ins. Wait 15 minutes and try again.")
        else:
            u = users.authenticate(username, request.form.get("password", ""))
            if u:
                _sign_in(u)
                activity.log(u, "sign-in")
                return redirect(_safe_next(nxt), code=303)
            for k in keys:
                _failed_logins.setdefault(k, []).append(time.time())
            known = users.get_by_name(username)  # never log unknown names: one could be a mistyped password
            if known:
                activity.log(known, "sign-in-failed")
            error = tr("Wrong username or password, or the account is disabled.")
    form = f"""<form method="post" class="stack">{_csrf_field()}
  <input type="hidden" name="next" value="{html.escape(nxt)}">
  <label>{tr('Username')} <input name="username" value="{html.escape(username)}" required autofocus autocomplete="username"></label>
  <label>{tr('Password')} <input type="password" name="password" required autocomplete="current-password"></label>
  <button type="submit">{tr('Sign in')}</button>
</form>
<p class="muted">{tr('No account yet?')} <a href="{url_for('register', next=nxt) if nxt else url_for('register')}">{tr('Create one')}</a></p>
<form method="post" action="{url_for('login_guest')}" class="stack">{_csrf_field()}
  <input type="hidden" name="next" value="{html.escape(nxt)}">
  <button type="submit">{tr('Continue as guest')}</button>
  <span class="small muted">{tr('Read stock reports and saved analyses. Guests cannot run analyses or request topics.')}</span>
</form>"""
    return _auth_page(tr("Sign in"), form, error, 401 if error else 200)


@app.post("/login/guest")
def login_guest():
    if _user() is None:
        _sign_in_guest()
        activity.log(users.GUEST, "guest")
    return redirect(_safe_next(request.form.get("next")), code=303)


@app.route("/register", methods=["GET", "POST"])
def register():
    if _user() and not _is_guest():
        return redirect(url_for("index"))
    nxt = request.values.get("next", "")
    error, username = "", ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()[:64]
        password = request.form.get("password", "")
        if password != request.form.get("confirm", ""):
            error = tr("The two passwords do not match.")
        else:
            try:
                u = users.create(username, password)
            except users.UserError as e:
                error = _user_error(e)
            else:
                users.record_login(u.id)
                _sign_in(u)
                activity.log(u, "register")
                flash(tr("Welcome, {name}. You are the first user, so your account is an admin.", name=u.username)
                      if u.is_admin else tr("Welcome, {name}.", name=u.username), "ok")
                return redirect(_safe_next(nxt), code=303)
    first = users.count() == 0
    hint = f"<p class='muted small'>{tr('The first account becomes the admin.')}</p>" if first else ""
    form = f"""{hint}<form method="post" class="stack">{_csrf_field()}
  <input type="hidden" name="next" value="{html.escape(nxt)}">
  <label>{tr('Username')} <input name="username" value="{html.escape(username)}" required autofocus
    pattern="[A-Za-z0-9_.\\-]{{3,32}}" maxlength="32" autocomplete="username"></label>
  <label>{tr('Password')} <input type="password" name="password" required minlength="{users.MIN_PASSWORD}" autocomplete="new-password">
    <span class="small muted">{tr('At least {n} characters.', n=users.MIN_PASSWORD)}</span></label>
  <label>{tr('Repeat password')} <input type="password" name="confirm" required autocomplete="new-password"></label>
  <button type="submit">{tr('Create account')}</button>
</form>
<p class="muted">{tr('Already have an account?')} <a href="{url_for('login', next=nxt) if nxt else url_for('login')}">{tr('Sign in')}</a></p>"""
    return _auth_page(tr("Create account"), form, error, 400 if error else 200)


@app.post("/logout")
def logout():
    if _user():
        _log("sign-out")
    session.clear()
    return redirect(url_for("login"), code=303)


@app.route("/account", methods=["GET", "POST"])
def account():
    u = _user()
    error = ""
    if request.method == "POST":
        new = request.form.get("new", "")
        if new != request.form.get("confirm", ""):
            error = tr("The two passwords do not match.")
        else:
            try:
                users.change_password(u.id, request.form.get("current", ""), new)
            except users.UserError as e:
                error = _user_error(e)
            else:
                _sign_in(users.get(u.id))  # keep this session; others were signed out
                activity.log(u, "change-password")
                flash(tr("Password changed. Your other sessions were signed out."), "ok")
                return redirect(url_for("account"), code=303)
    mine = sum(1 for m in theme_store.list_saved() if m.get("owner", "").lower() == u.username.lower())
    err = f"<div class='flash error'>{html.escape(error)}</div>" if error else ""
    body = f"""{_theme_nav()}<h1>{tr('Account')}</h1>
<div class="panel" style="margin-top:20px"><table class="kv">
  <tr><th scope="row">{tr('Username')}</th><td>{html.escape(u.username)}</td></tr>
  <tr><th scope="row">{tr('Role')}</th><td>{_role(u)}{'' if u.has_admin_rights else ' · ' + tr('Claude research is available to admins only')}</td></tr>
  <tr><th scope="row">{tr('Created')}</th><td>{_fmt_time(u.created)}</td></tr>
  <tr><th scope="row">{tr('Last sign-in')}</th><td>{_fmt_time(u.last_login or '') or '–'}</td></tr>
  <tr><th scope="row">{tr('Saved analyses')}</th><td>{mine}</td></tr>
</table></div>
<h2>{tr('Change password')}</h2>{err}
<form method="post" class="panel stack" style="max-width:420px">{_csrf_field()}
  <label>{tr('Current password')} <input type="password" name="current" required autocomplete="current-password"></label>
  <label>{tr('New password')} <input type="password" name="new" required minlength="{users.MIN_PASSWORD}" autocomplete="new-password"></label>
  <label>{tr('Repeat password')} <input type="password" name="confirm" required autocomplete="new-password"></label>
  <button type="submit">{tr('Change password')}</button>
</form>"""
    return _page(tr("Account"), body, 400 if error else 200)


ADMIN_ACTIONS = ("grant-admin", "revoke-admin", "grant-privileged", "revoke-privileged", "disable", "enable",
                 "reset-password", "delete")


def _role(u: users.User) -> str:
    return tr("Admin") if u.is_admin else tr("Privileged") if u.is_privileged else tr("User")


def _admin_tabs(current: str) -> str:
    tabs = (("admin_panel", tr("Overview")), ("admin_users", tr("Users")), ("admin_activity", tr("Activity")),
            ("admin_requests", tr("Requests")))
    links = "".join(f"<a href='{url_for(ep)}'{' class=on aria-current=page' if ep == current else ''}>{label}</a>"
                    for ep, label in tabs)
    return f"{_theme_nav()}<h1>{tr('Admin panel')}</h1><nav class='tabs'>{links}</nav>"


def _activity_table(entries: list[activity.Entry]) -> str:
    def who(e: activity.Entry) -> str:
        name = tr("Guest") if e.user_id == users.GUEST.id else e.username
        if e.user_id < 0:  # a deleted account
            return html.escape(name)
        return f"<a href='{url_for('admin_activity', user=e.user_id)}'>{html.escape(name)}</a>"

    rows = "".join(
        f"<tr><td>{_fmt_time(e.created)}</td><td>{who(e)}</td>"
        f"<td>{tr(activity.ACTIONS[e.action]) if e.action in activity.ACTIONS else html.escape(e.action)}</td>"
        f"<td>{html.escape(e.detail)}</td></tr>" for e in entries)
    head = "".join(f"<th>{h}</th>" for h in (tr("Time"), tr("Username"), tr("Action"), tr("Details")))
    return (f"<div class='panel scroll'><table id='activity'><thead><tr>{head}</tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")


@app.get("/admin")
def admin_panel():
    if not _is_admin():
        return _forbidden(tr("This page is for admins."))
    everyone = users.list_users()

    def tile(label: str, n: int, ep: str) -> str:
        return (f"<a class='tile' href='{url_for(ep)}'><div class='tile-label'>{label}</div>"
                f"<div class='tile-value'>{n}</div></a>")

    tiles = "".join([
        tile(tr("Users"), len(everyone), "admin_users"),
        tile(tr("Admins"), sum(u.is_admin for u in everyone), "admin_users"),
        tile(tr("Privileged users"), sum(u.is_privileged for u in everyone), "admin_users"),
        tile(tr("Active in the last 7 days"), activity.active_users(7), "admin_activity"),
        tile(tr("Requests waiting"), wishlist.pending_count(), "admin_requests"),
    ])
    latest = activity.recent(limit=15)
    table = _activity_table(latest) if latest else f"<p class='muted'>{tr('No activity yet.')}</p>"
    return _page(tr("Admin panel"), f"{_admin_tabs('admin_panel')}<div class='tiles'>{tiles}</div>"
                 f"<h2>{tr('Latest activity')}</h2>{table}"
                 f"<p><a href='{url_for('admin_activity')}'>{tr('All activity')} →</a></p>")


@app.get("/admin/activity")
def admin_activity():
    if not _is_admin():
        return _forbidden(tr("This page is for admins."))
    user_id = request.args.get("user", type=int)
    action = request.args.get("action") if request.args.get("action") in activity.ACTIONS else None
    before = request.args.get("before", type=int)
    entries = activity.recent(user_id, action, before, activity.PAGE_SIZE)
    user_opts = "".join(
        f"<option value='{i}'{' selected' if i == user_id else ''}>{html.escape(name)}</option>"
        for i, name in [(u.id, u.username) for u in users.list_users()] + [(users.GUEST.id, tr("Guest"))])
    action_opts = "".join(f"<option value='{k}'{' selected' if k == action else ''}>{tr(v)}</option>"
                          for k, v in activity.ACTIONS.items())
    form = f"""<form class="panel filters" method="get">
  <label>{tr('Username')} <select name="user"><option value="">{tr('Everyone')}</option>{user_opts}</select></label>
  <label>{tr('Action')} <select name="action"><option value="">{tr('All actions')}</option>{action_opts}</select></label>
  <button type="submit">{tr('Show')}</button>
</form>"""
    table = _activity_table(entries) if entries else f"<p class='muted'>{tr('No matching activity.')}</p>"
    older = ""
    if len(entries) == activity.PAGE_SIZE:
        args = {k: v for k, v in (("user", user_id), ("action", action)) if v is not None}
        older = f"<p><a href='{url_for('admin_activity', **args, before=entries[-1].id)}'>{tr('Older')} →</a></p>"
    note = tr("Sign-ins, reports and analyses opened, and admin actions from the last {n} days. Guests share one "
              "identity. IP addresses are not stored.", n=activity.KEEP_DAYS)
    return _page(tr("Activity"), f"{_admin_tabs('admin_activity')}<p class='muted small'>{note}</p>{form}{table}{older}")


@app.get("/admin/users")
def admin_users():
    if not _is_admin():
        return _forbidden(tr("This page is for admins."))
    me = _user()
    counts: dict[str, int] = {}
    for m in theme_store.list_saved():
        counts[m.get("owner", "").lower()] = counts.get(m.get("owner", "").lower(), 0) + 1

    def button(u: users.User, action: str, label: str, confirm: str = "") -> str:
        js = f" onsubmit=\"return confirm({html.escape(json.dumps(confirm))})\"" if confirm else ""
        return (f"<form method='post' action='{url_for('admin_action', user_id=u.id, action=action)}' "
                f"class='inline'{js}>{_csrf_field()}<button type='submit' class='link'>{label}</button></form>")

    rows = []
    for u in users.list_users():
        if u.id == me.id:
            actions = f"<span class='muted small'>{tr('(you)')}</span>"
        else:
            if u.is_admin:
                roles = [button(u, "revoke-admin", tr("Remove admin")),
                         button(u, "grant-privileged", tr("Make privileged"))]
            elif u.is_privileged:
                roles = [button(u, "revoke-privileged", tr("Remove privileges")),
                         button(u, "grant-admin", tr("Make admin"))]
            else:
                roles = [button(u, "grant-privileged", tr("Make privileged")), button(u, "grant-admin", tr("Make admin"))]
            actions = " · ".join([
                *roles,
                button(u, "disable", tr("Disable")) if u.is_active else button(u, "enable", tr("Enable")),
                button(u, "reset-password", tr("Reset password"),
                       tr("Give {name} a new temporary password?", name=u.username)),
                button(u, "delete", tr("Delete"), tr("Delete the account {name}? Their saved analyses stay.",
                                                       name=u.username)),
            ])
        rows.append(
            f"<tr><td><b>{html.escape(u.username)}</b></td><td>{_role(u)}</td>"
            f"<td>{tr('Active') if u.is_active else tr('Disabled')}</td><td>{_fmt_time(u.created)}</td>"
            f"<td>{_fmt_time(u.last_login or '') or '–'}</td><td>{counts.get(u.username.lower(), 0)}</td>"
            f"<td><a href='{url_for('admin_activity', user=u.id)}'>{tr('Activity')}</a></td>"
            f"<td>{actions}</td></tr>")
    head = "".join(f"<th>{h}</th>" for h in (tr("Username"), tr("Role"), tr("Status"), tr("Created"),
                                              tr("Last sign-in"), tr("Saved analyses"), tr("Activity"), ""))
    body = (f"{_admin_tabs('admin_users')}"
            f"<div class='meta'>{tr('Anyone can create an account. Admins and privileged users have the same rights: they can run Claude research and manage users.')}</div>"
            f"<div class='panel scroll' style='margin-top:20px'><table id='users'><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")
    return _page(tr("Users"), body)


@app.post("/admin/users/<int:user_id>/<action>")
def admin_action(user_id: int, action: str):
    if not _is_admin():
        return _forbidden(tr("This page is for admins."))
    target = users.get(user_id)
    if target is None or action not in ADMIN_ACTIONS:
        return _page(tr("Not found"), f"{_theme_nav()}<h1>{tr('No such user')}</h1>", 404)
    if target.id == _user().id:
        flash(tr("Use the Account page to change your own account."), "error")
        return redirect(url_for("admin_users"), code=303)
    name = target.username
    try:
        if action == "grant-admin":
            users.set_admin(user_id, True)
            flash(tr("{name} is now an admin.", name=name), "ok")
        elif action == "revoke-admin":
            users.set_admin(user_id, False)
            flash(tr("{name} is no longer an admin.", name=name), "ok")
        elif action == "grant-privileged":
            users.set_privileged(user_id, True)
            flash(tr("{name} is now a privileged user, with the same rights as an admin.", name=name), "ok")
        elif action == "revoke-privileged":
            users.set_privileged(user_id, False)
            flash(tr("{name} is no longer a privileged user.", name=name), "ok")
        elif action == "disable":
            users.set_active(user_id, False)
            flash(tr("{name} is disabled and signed out.", name=name), "ok")
        elif action == "enable":
            users.set_active(user_id, True)
            flash(tr("{name} is enabled.", name=name), "ok")
        elif action == "reset-password":
            temp = users.reset_password(user_id)
            flash(tr("Temporary password for {name}: {password} — give it to them privately; it is shown only "
                     "once. They can change it on their Account page.", name=name, password=temp), "ok")
        elif action == "delete":
            users.delete(user_id)
            flash(tr("Deleted the account {name}.", name=name), "ok")
        _log(action, name)
    except users.UserError as e:
        flash(_user_error(e), "error")
    return redirect(url_for("admin_users"), code=303)


@app.get("/admin/requests")
def admin_requests():
    if not _is_admin():
        return _forbidden(tr("This page is for admins."))
    groups: dict[str, list[wishlist.TopicRequest]] = {}
    for r in reversed(wishlist.list_requests(status="pending")):  # oldest first
        groups.setdefault(r.topic.lower(), []).append(r)
    has_llm = llm.available()
    day_opts = "".join(f"<option value='{d}'{' selected' if d == 3 else ''}>"
                       f"{tr('1 day of news') if d == 1 else tr('{n} days of news', n=d)}</option>" for d in NEWS_DAYS)
    cards = []
    for group in groups.values():
        first = group[0]
        who = "".join(f"<li><b>{html.escape(r.username)}</b> <span class='muted small'>{_fmt_time(r.created)}</span>"
                      f"{f'<div>{html.escape(r.note)}</div>' if r.note else ''}</li>" for r in group)
        llm_box = (f"<label><input type='checkbox' name='llm' value='1' checked> {tr('Claude research and SWOT')}</label>"
                   if has_llm else f"<span class='muted small'>{tr('(set ANTHROPIC_API_KEY to enable)')}</span>")
        cards.append(f"""<div class="panel request">
  <h3>{html.escape(first.topic)}</h3>
  <div class="muted small">{tr('Requested by {n} people', n=len(group)) if len(group) > 1 else tr('Requested by')}</div>
  <ul class="who-list">{who}</ul>
  <form class="custom analyze" action="{url_for('theme_page')}" method="get">
    <input type="hidden" name="req" value="{first.id}">
    <label>{tr('Theme name')} <input name="name" required maxlength="40" value="{html.escape(first.topic)}"></label>
    <label>{tr('News keywords')} <input name="keywords" maxlength="300" placeholder="{tr('comma-separated, e.g. 機器人, 人形機器人, robot')}"></label>
    <label>{tr('Stock codes')} <input name="stocks" maxlength="200" placeholder="{tr('e.g. 2049 1590 4583 (optional when Claude research is on)')}"></label>
    <div class="options"><select name="days" aria-label="{tr('News window')}">{day_opts}</select>{llm_box}</div>
    <button type="submit">{tr('Run analysis')}</button>
  </form>
  <form method="post" action="{url_for('admin_request_decline', req_id=first.id)}" class="decline">{_csrf_field()}
    <input name="reply" maxlength="{wishlist.MAX_NOTE}" placeholder="{tr('Reason (optional, shown to the requester)')}">
    <button type="submit">{tr('Decline')}</button>
  </form>
</div>""")
    pending = "".join(cards) or f"<p class='muted'>{tr('No topic requests are waiting.')}</p>"
    decided = [r for r in wishlist.list_requests() if r.status != "pending"][:30]
    rows = "".join(
        f"<tr><td>{_fmt_time(r.decided or '')}</td><td><b>{html.escape(r.topic)}</b></td>"
        f"<td>{html.escape(r.username)}</td><td>{_request_status(r)}</td>"
        f"<td>{html.escape(r.decided_by or '')}</td></tr>" for r in decided)
    head = "".join(f"<th>{h}</th>" for h in (tr("Decided"), tr("Topic"), tr("Requested by"), tr("Status"),
                                              tr("Admin")))
    history = (f"<h2>{tr('Recently decided')}</h2><div class='panel scroll'><table id='requests-done'>"
               f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>") if rows else ""
    body = f"""{_admin_tabs('admin_requests')}
<div class="meta">{tr('Run a requested topic to notify everyone who asked for it, or decline it.')}</div>
<div class="requests">{pending}</div>
{history}
<div id="loading" hidden><div class="panel">{tr('Analyzing theme… this can take a minute or more.')}</div></div>
<script>
for (const f of document.querySelectorAll("form.analyze"))
  f.addEventListener("submit", () => {{ document.getElementById("loading").hidden = false; }});
window.addEventListener("pageshow", () => {{ document.getElementById("loading").hidden = true; }});
</script>"""
    return _page(tr("Topic requests"), body)


@app.post("/admin/requests/<int:req_id>/decline")
def admin_request_decline(req_id: int):
    if not _is_admin():
        return _forbidden(tr("This page is for admins."))
    ids = wishlist.decline(req_id, _user().username, request.form.get("reply", ""))
    if ids:
        _log("decline-request", wishlist.get(req_id).topic)
        flash(tr("Declined “{topic}”; the requesters will be notified.", topic=wishlist.get(req_id).topic), "ok")
    else:
        flash(tr("That request was already decided."), "error")
    return redirect(url_for("admin_requests"), code=303)


def _error(stock_id: str, years: int, message: str, status: int) -> Response:
    body = f"{_nav(stock_id, years)}<h1>{tr('No report')}</h1><p class='muted'>{html.escape(message)}</p>"
    return _page(tr("No report"), body, status)


_INDEX_CSS = """
.tabs { display: flex; gap: 4px; flex-wrap: wrap; margin: 12px 0 20px; border-bottom: 1px solid var(--border); }
.tabs a { padding: 6px 12px; color: var(--text-secondary); text-decoration: none; border-bottom: 2px solid transparent;
  margin-bottom: -1px; }
.tabs a:hover { color: var(--text-primary); }
.tabs a.on { color: var(--text-primary); font-weight: 600; border-bottom-color: var(--text-primary); }
a.tile { color: inherit; text-decoration: none; }
a.tile:hover { border-color: var(--text-secondary); }
#activity td, #activity th { text-align: left; white-space: normal; }
#activity td:first-child { white-space: nowrap; }
form.filters { align-items: end; margin-bottom: 12px; }
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
#saved td, #saved th, #users td, #users th { text-align: left; vertical-align: top; }
.auth { max-width: 420px; margin: 40px auto 0; }
.auth h1 { margin-bottom: 16px; }
.stack { display: grid; gap: 12px; }
.stack label { display: grid; gap: 4px; color: var(--text-secondary); }
.stack button { justify-self: start; }
.flash { border-left: 3px solid var(--good); background: var(--surface-1); padding: 8px 12px; margin: 0 0 16px;
  border-radius: 4px; }
.flash.error { border-left-color: var(--critical); }
table.kv th { width: 180px; }
table.kv td { text-align: left; }
.brand { font-weight: 600; }
#saved a { color: inherit; font-weight: 600; }
form.inline { display: inline; margin: 0; }
button.link { border: 0; background: none; padding: 0; color: var(--text-secondary); text-decoration: underline; }
button.link:hover { color: var(--critical); }
#requests-mine td, #requests-mine th, #requests-done td, #requests-done th { text-align: left; vertical-align: top; }
#requests-mine a, #requests-done a { color: inherit; font-weight: 600; }
.requests { display: grid; gap: 12px; margin-top: 20px; }
.request h3 { margin: 0 0 4px; }
.request .who-list { margin: 6px 0 12px; padding-left: 18px; }
.request .options { margin-top: 0; }
.request .decline { display: flex; gap: 8px; margin-top: 12px; max-width: 720px; }
.request .decline input { flex: 1; }
.custom textarea { font: inherit; }
#requests ~ h3 { margin: 20px 0 8px; }
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
