from types import SimpleNamespace

import pytest

from conftest import CSRF, sign_in
from tw_fundamental import activity, llm, portfolio, theme_store, users, web
from tw_fundamental.finmind import FinMindError
from tw_fundamental.theme import StockProfile, Theme, ThemeResult

LISTED = [{"id": "2330", "name": "台積電", "market": "twse", "industry": "半導體業"}]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web, "list_stocks", lambda: LISTED)
    return web.app.test_client()


def post(client, path, **form):
    return client.post(path, data={"csrf": CSRF, **form})


def saved_run(key="cpo", title="CPO", lang="en", query=None):
    t = Theme(key, title, keywords=[key])
    r = ThemeResult(t, [StockProfile("2330", "台積電")], {}, 3, lang=lang)
    return theme_store.save(r, theme_store.params_key(t, 3, False, lang), query or {"name": key}, owner="alice")


# --------------------------------------------------------------------------- portfolio.py

def test_stocks_are_per_user_and_adding_twice_is_harmless():
    a, b = users.create("alice", "password123"), users.create("bob", "password123")
    assert portfolio.add_stock(a.id, "2330") and not portfolio.add_stock(a.id, "2330")
    assert [s.stock_id for s in portfolio.stocks(a.id)] == ["2330"] and portfolio.stocks(b.id) == []
    assert portfolio.remove_stock(a.id, "2330") and not portfolio.remove_stock(a.id, "2330")


def test_adding_a_theme_again_updates_its_details():
    u = users.create("alice", "password123")
    assert portfolio.add_theme(u.id, "robots", "Robots", stocks="2049")
    assert not portfolio.add_theme(u.id, "ROBOTS", "Robots", stocks="2049 1590")
    assert [t.stocks for t in portfolio.themes(u.id)] == ["2049 1590"]


def test_limits(monkeypatch):
    monkeypatch.setattr(portfolio, "MAX_STOCKS", 1)
    monkeypatch.setattr(portfolio, "MAX_THEMES", 1)
    u = users.create("alice", "password123")
    portfolio.add_stock(u.id, "2330")
    portfolio.add_theme(u.id, "cpo", "CPO")
    assert not portfolio.add_stock(u.id, "2330")  # already there, so not over the limit
    assert not portfolio.add_theme(u.id, "cpo", "CPO")
    with pytest.raises(users.UserError, match="up to 1 stocks"):
        portfolio.add_stock(u.id, "2317")
    with pytest.raises(users.UserError, match="up to 1 themes"):
        portfolio.add_theme(u.id, "memory", "Memory")


def test_deleting_an_account_empties_its_portfolio():
    users.create("admin", "password123")
    u = users.create("bob", "password123")
    portfolio.add_stock(u.id, "2330")
    portfolio.add_theme(u.id, "cpo", "CPO")
    users.delete(u.id)
    assert portfolio.stocks(u.id) == [] and portfolio.themes(u.id) == []


# --------------------------------------------------------------------------- web

def test_add_a_stock_by_code_and_see_it_listed(client):
    u = sign_in(client)
    assert post(client, "/portfolio/stock/add", id="2330 台積電").status_code == 303
    body = client.get("/portfolio").get_data(as_text=True)
    assert "台積電" in body and "半導體業" in body
    assert [e.action for e in activity.recent(u.id)] == ["portfolio-add"]


@pytest.mark.parametrize("code", ["9999", "abc", "", "<b>"])
def test_unknown_stocks_are_rejected(client, code):
    u = sign_in(client)
    r = post(client, "/portfolio/stock/add", id=code)
    assert r.status_code == 303 and r.location.endswith("/portfolio")
    assert portfolio.stocks(u.id) == []
    assert "<b>" not in client.get("/portfolio").get_data(as_text=True).replace("<b>台", "")


def test_page_works_when_the_stock_list_is_unavailable(client, monkeypatch):
    def down():
        raise FinMindError("down")
    u = sign_in(client)
    portfolio.add_stock(u.id, "2330")
    monkeypatch.setattr(web, "list_stocks", down)
    assert ">2330<" in client.get("/portfolio").get_data(as_text=True)
    assert post(client, "/portfolio/stock/add", id="2317").status_code == 303  # format checked only
    assert len(portfolio.stocks(u.id)) == 2


def test_report_button_adds_removes_and_returns_to_the_report(client, monkeypatch):
    seen = {}

    def build(sid, years, nav="", **k):
        seen["nav"] = nav
        return SimpleNamespace(html="report")
    monkeypatch.setattr(web, "build_report", build)
    u = sign_in(client)
    client.get("/report/2330?refresh=1")
    assert "refresh=1" not in seen["nav"].split("portfolio/stock/add")[1].split("</form>")[0]
    r = post(client, "/portfolio/stock/add", id="2330", next="/report/2330?years=5")
    assert r.location.endswith("/report/2330?years=5")
    client.get("/report/2330")
    assert "/portfolio/stock/remove" in seen["nav"]
    post(client, "/portfolio/stock/remove", id="2330")
    assert portfolio.stocks(u.id) == []


def test_no_add_button_on_the_error_page_for_an_unknown_stock(client, monkeypatch):
    def fail(*a, **k):
        raise FinMindError("Unknown stock 9999")
    monkeypatch.setattr(web, "build_report", fail)
    sign_in(client)
    assert "portfolio/stock/add" not in client.get("/report/9999").get_data(as_text=True)


def test_next_cannot_leave_the_site(client):
    sign_in(client)
    r = post(client, "/portfolio/stock/add", id="2330", next="https://evil.example/")
    assert "evil" not in r.location


def test_add_a_preset_theme_shows_its_title_in_the_page_language(client):
    u = sign_in(client)
    key = next(iter(web.theme.load_presets("en")))
    post(client, "/portfolio/theme/add", name=key)
    assert [t.key for t in portfolio.themes(u.id)] == [key]
    title = web.theme.load_presets("zh-TW")[key].title
    assert title in client.get("/portfolio?lang=zh-TW").get_data(as_text=True)
    # A preset already in the portfolio is no longer offered.
    assert f"<option value='{key}'" not in client.get("/portfolio").get_data(as_text=True)


def test_theme_opens_its_newest_saved_run_without_running_anything(client):
    u = sign_in(client)
    rid = saved_run()
    portfolio.add_theme(u.id, "cpo", "CPO")
    assert f"/theme/saved/{rid}" in client.get("/portfolio").get_data(as_text=True)


def test_custom_theme_without_a_saved_run_keeps_its_stocks_and_keywords(client):
    u = sign_in(client)
    post(client, "/portfolio/theme/add", name="Robots", keywords="機器人, robot", stocks="2049 1590")
    assert portfolio.themes(u.id)[0].stocks == "2049 1590"
    body = client.get("/portfolio").get_data(as_text=True)
    assert "/theme?name=Robots" in body and "stocks=2049" in body and "Not analyzed yet" in body


def test_custom_theme_with_bad_codes_is_rejected(client):
    u = sign_in(client)
    post(client, "/portfolio/theme/add", name="Robots", stocks="not-a-code")
    assert portfolio.themes(u.id) == []


def test_theme_button_returns_to_the_saved_run_not_a_rerun(client, monkeypatch):
    """Going back to a rerun=1 address would run the analysis again, possibly with Claude."""
    def analyze(t, **k):
        return ThemeResult(t, [StockProfile("2330", "台積電")], {}, 3, lang="en")
    monkeypatch.setattr(web.theme, "analyze", analyze)
    monkeypatch.setattr(llm, "available", lambda: False)
    sign_in(client)
    captured = {}
    monkeypatch.setattr(web.theme_report, "render", lambda r, nav="", link=None: captured.update(nav=nav) or "page")
    client.get("/theme?name=robots&stocks=2049&rerun=1")
    form = captured["nav"].split("portfolio/theme/add")[1].split("</form>")[0]
    assert "rerun" not in form and "/theme/saved/" in form and "value='2049'" in form


def test_saved_run_shows_the_button_and_it_toggles(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(web.theme_report, "render", lambda r, nav="", link=None: captured.update(nav=nav) or "page")
    u = sign_in(client)
    rid = saved_run()
    client.get(f"/theme/saved/{rid}")
    assert "/portfolio/theme/add" in captured["nav"]
    portfolio.add_theme(u.id, "cpo", "CPO")
    client.get(f"/theme/saved/{rid}")
    assert "/portfolio/theme/remove" in captured["nav"]
    post(client, "/portfolio/theme/remove", name="cpo")
    assert portfolio.themes(u.id) == []


def test_users_only_see_their_own_portfolio(client):
    u = sign_in(client, "alice")
    other = users.create("bob", "password123")
    portfolio.add_stock(other.id, "2330")
    assert "台積電" not in client.get("/portfolio").get_data(as_text=True)
    post(client, "/portfolio/stock/remove", id="2330")
    assert [s.stock_id for s in portfolio.stocks(other.id)] == ["2330"] and u.id != other.id


def test_guests_have_no_portfolio(client, monkeypatch):
    with client.session_transaction() as s:
        s.update(guest=True, csrf=CSRF)
    seen = {}

    def build(sid, years, nav="", **k):
        seen["nav"] = nav
        return SimpleNamespace(html="report")
    monkeypatch.setattr(web, "build_report", build)
    client.get("/report/2330")
    assert "/portfolio" not in seen["nav"]
    assert client.get("/portfolio").status_code == 403
    for path in ("/portfolio/stock/add", "/portfolio/theme/add"):
        assert post(client, path, id="2330", name="cpo").status_code == 403


def test_portfolio_needs_an_account(client):
    r = client.get("/portfolio")
    assert r.status_code == 302 and "/login" in r.location


def test_forms_need_a_csrf_token(client):
    u = sign_in(client)
    assert client.post("/portfolio/stock/add", data={"id": "2330"}).status_code == 400
    assert portfolio.stocks(u.id) == []
