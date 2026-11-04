from types import SimpleNamespace

import pytest

from conftest import CSRF, sign_in
from tw_fundamental import web
from tw_fundamental.finmind import FinMindError

STOCKS = [{"id": "2330", "name": "台積電", "market": "twse", "industry": "半導體業"}]


@pytest.fixture
def client(monkeypatch):
    calls = []

    def fake_build(stock_id, years=5, refresh=False, nav="", lang="en", saved_url=None):
        calls.append((stock_id, years, refresh))
        if stock_id == "9999":
            raise FinMindError("Unknown stock id: 9999")
        return SimpleNamespace(html=f"<html>{nav}report {stock_id} {years}</html>")

    monkeypatch.setattr(web, "list_stocks", lambda: STOCKS)
    monkeypatch.setattr(web, "build_report", fake_build)
    web.app.config["TESTING"] = True
    c = web.app.test_client()
    c.calls = calls
    c.user = sign_in(c)
    return c


def test_index_lists_stocks(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "台積電" in body and "2330" in body


def test_report_page(client):
    r = client.get("/report/2330?years=8")
    assert r.status_code == 200
    assert "report 2330 8" in r.get_data(as_text=True)
    assert "All stocks" in r.get_data(as_text=True)  # nav injected


def test_years_outside_choices_fall_back_to_default(client):
    client.get("/report/2330?years=99")
    assert client.calls[-1] == ("2330", 5, False)


def test_query_form_redirects(client):
    r = client.get("/report?id=2330+台積電&years=3")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/report/2330?years=3")


def test_unknown_stock_is_404(client):
    assert client.get("/report/9999").status_code == 404


def test_invalid_id_rejected_without_fetch(client):
    r = client.get("/report/..%2Fetc")
    assert r.status_code in (400, 404)
    assert client.calls == []


def test_refresh_redirects_to_clean_url(client):
    r = client.get("/report/2330?years=5&refresh=1")
    assert r.status_code == 302
    assert client.calls[-1] == ("2330", 5, True)


@pytest.fixture
def theme_client(client, monkeypatch, tmp_path):
    from tw_fundamental import theme
    from tw_fundamental.theme import StockProfile, ThemeResult

    calls = []

    def fake_analyze(t, news_days=3, use_llm=False, refresh=False, lang="en"):
        calls.append((t, news_days, use_llm))
        p = StockProfile("2330", "台積電", metrics={k: 0.1 for k in [
            "rev_3m_yoy", "rev_12m_yoy", "revenue_yoy_q", "gross_margin", "gross_margin_chg", "operating_margin",
            "net_margin", "eps_ttm", "roe_ttm", "debt_ratio", "current_ratio", "ocf_to_ni", "fcf_ttm", "per",
            "per_pct", "pbr", "dividend_yield", "close"]})
        return ThemeResult(t, [p], {}, news_days, lang=lang)

    monkeypatch.setattr(theme, "analyze", fake_analyze)
    monkeypatch.setattr(web.llm, "available", lambda: False)
    client.theme_calls = calls
    return client


def test_themes_page_lists_presets(theme_client):
    body = theme_client.get("/themes").get_data(as_text=True)
    assert "CPO" in body and "ANTHROPIC_API_KEY" in body


def test_theme_preset_runs_without_llm_when_unavailable(theme_client):
    r = theme_client.get("/theme?name=cpo&days=7&llm=1")
    assert r.status_code == 200
    t, days, use_llm = theme_client.theme_calls[-1]
    assert t.key == "CPO" and days == 7 and use_llm is False
    body = r.get_data(as_text=True)
    assert "no Claude credentials" in body and "/report/2330" in body


def test_custom_theme_parses_codes_and_keywords(theme_client):
    theme_client.get("/theme?name=Robots&keywords=機器人，robot&stocks=2049, 1590&days=99")
    t, days, _ = theme_client.theme_calls[-1]
    assert t.stock_ids == ["2049", "1590"] and t.keywords == ["機器人", "robot"] and days == 3


def test_custom_theme_rejects_bad_codes(theme_client):
    assert theme_client.get("/theme?name=X&stocks=2330,../x").status_code == 400
    assert theme_client.theme_calls == []


def test_custom_theme_without_stocks_or_llm(theme_client):
    r = theme_client.get("/theme?name=Brand+new&req=7")
    body = r.get_data(as_text=True)
    assert r.status_code == 400 and theme_client.theme_calls == []
    assert "No company names or industries match it" in body and "name='req' value='7'" in body
    assert "is not a preset" in body and "/theme?name=CPO" in body  # links to presets


def test_custom_theme_suggests_matching_stocks(theme_client):
    body = theme_client.get("/theme?name=半導體&days=7").get_data(as_text=True)
    assert "2330 台積電" in body and 'name="stocks" maxlength="200" value="2330"' in body
    assert 'name="days" value="7"' in body


def test_preset_keyword_opens_preset(theme_client):
    theme_client.get("/theme?name=DRAM")
    assert theme_client.theme_calls[0][0].key == "memory"


def test_no_stocks_page_offers_request_to_non_admins(theme_client):
    with theme_client.session_transaction() as s:
        s.clear()
    sign_in(theme_client, "bob", admin=False)
    body = theme_client.get("/theme?name=Brand+new").get_data(as_text=True)
    assert "only admins can have Claude find the stocks" in body and "Request “Brand new”" in body


# --------------------------------------------------------------------------- saved analyses

def test_same_theme_reuses_saved_run(theme_client):
    body = theme_client.get("/theme?name=cpo&days=3").get_data(as_text=True)
    assert "Saved analysis" not in body and len(theme_client.theme_calls) == 1
    body = theme_client.get("/theme?name=CPO&days=3").get_data(as_text=True)
    assert len(theme_client.theme_calls) == 1  # served from the saved run
    assert "Showing the saved run from" in body and "rerun=1" in body and "/report/2330" in body
    theme_client.get("/theme?name=cpo&days=7")  # different parameters run again
    assert len(theme_client.theme_calls) == 2


def test_rerun_and_refresh_bypass_saved_run(theme_client):
    theme_client.get("/theme?name=cpo")
    theme_client.get("/theme?name=cpo&rerun=1")
    theme_client.get("/theme?name=cpo&refresh=1")
    assert len(theme_client.theme_calls) == 3


def test_saved_list_view_and_delete(theme_client):
    from tw_fundamental import theme_store
    theme_client.get("/theme?name=cpo&keywords=CPO,矽光子")
    (meta,) = theme_store.list_saved()
    assert meta["query"] == {"name": "CPO", "days": 3, "llm": "", "keywords": "CPO, 矽光子"}
    body = theme_client.get("/themes/saved").get_data(as_text=True)
    assert meta["id"] in body and "CPO" in body
    assert "Recent analyses" in theme_client.get("/themes").get_data(as_text=True)
    body = theme_client.get(f"/theme/saved/{meta['id']}").get_data(as_text=True)
    assert "Saved analysis from" in body and "keywords=CPO" in body
    r = theme_client.post(f"/theme/saved/{meta['id']}/delete", data={"csrf": CSRF})
    assert r.status_code == 303 and theme_store.list_saved() == []
    assert theme_client.get(f"/theme/saved/{meta['id']}").status_code == 404
    assert theme_client.get("/theme/saved/..%2Fsecret").status_code == 404
