from types import SimpleNamespace

import pytest

from tw_fundamental import web
from tw_fundamental.finmind import FinMindError

STOCKS = [{"id": "2330", "name": "台積電", "market": "twse", "industry": "半導體業"}]


@pytest.fixture
def client(monkeypatch):
    calls = []

    def fake_build(stock_id, years=5, refresh=False, nav="", lang="en"):
        calls.append((stock_id, years, refresh))
        if stock_id == "9999":
            raise FinMindError("Unknown stock id: 9999")
        return SimpleNamespace(html=f"<html>{nav}report {stock_id} {years}</html>")

    monkeypatch.setattr(web, "list_stocks", lambda: STOCKS)
    monkeypatch.setattr(web, "build_report", fake_build)
    web.app.config["TESTING"] = True
    c = web.app.test_client()
    c.calls = calls
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
def theme_client(client, monkeypatch):
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
    assert theme_client.get("/theme?name=Brand+new").status_code == 400
