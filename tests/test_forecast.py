import math
from datetime import date
from types import SimpleNamespace

import pytest

from tw_fundamental import forecast, llm, theme, theme_report
from tw_fundamental.i18n import translator
from tw_fundamental.theme import StockProfile, ThemeResult, make_theme


def stock(sid, segment="Up", **m):
    base = dict(revenue_12m=100e8, rev_12m_yoy=0.25, rev_3m_yoy=0.4, eps_ttm=10.0, close=500.0, per=50.0)
    base.update(m)
    return StockProfile(sid, f"Co{sid}", metrics=base, segment=segment, revenue_through="2026-10")


def test_fade_path_moves_growth_to_long_run():
    sizes = forecast.fade_path(100.0, 0.5, 0.05, years=6)
    growth = [b / a - 1 for a, b in zip(sizes, sizes[1:])]
    assert len(sizes) == 7 and growth[0] == pytest.approx(0.5) and growth[-1] == pytest.approx(0.05)
    assert growth == sorted(growth, reverse=True)


def test_recent_growth_weights_by_revenue():
    total, g, n = forecast.recent_growth([stock("1", revenue_12m=300e8, rev_12m_yoy=0.5),
                                          stock("2", revenue_12m=100e8, rev_12m_yoy=0.0),
                                          stock("3", revenue_12m=math.nan)])
    assert n == 2 and total == pytest.approx(400e8)
    assert g == pytest.approx(400 / 300 - 1)  # prior = 200 + 100


def test_extrapolate_projects_tracked_revenue():
    ps = [stock("1"), stock("2", segment="Down", rev_12m_yoy=-0.1)]
    segs = [{"name": "Up", "stock_ids": ["1"]}, {"name": "Down", "stock_ids": ["2"]}, {"name": "Gap", "stock_ids": []}]
    f = forecast.extrapolate(ps, segs)
    assert f["source"] == "extrapolated" and f["base_year"] == 2026 and f["through"] == "2026-10"
    assert [p["year"] for p in f["points"]] == list(range(2026, 2033))
    assert f["points"][0]["base"] == pytest.approx(200)  # NT$ 億
    assert all(p["low"] <= p["base"] <= p["high"] for p in f["points"])
    assert f["cagr_low"] < f["cagr"] < f["cagr_high"]
    assert [s["segment"] for s in f["segments"]] == ["Up", "Down"]
    assert f["segments"][0]["cagr"] > f["segments"][1]["cagr"]
    assert f["largest"]["share"] == pytest.approx(0.5)


def test_extrapolate_clips_extreme_growth_and_needs_data():
    f = forecast.extrapolate([stock("1", rev_12m_yoy=3.0)], [])
    assert f["recent_growth"] == pytest.approx(3.0) and f["start_growth"] == pytest.approx(0.6)
    assert forecast.extrapolate([stock("1", revenue_12m=math.nan)], []) is None


def test_from_claude_validates_points_and_estimates():
    raw = {"market": "CPO", "unit": "US$ billion",
           "points": [{"year": y, "low": 2.0 * (y - 2025), "base": 1.0 * (y - 2025), "high": 3.0 * (y - 2025)}
                      for y in range(2026, 2033)] + [{"year": 2040, "low": 1, "base": 1, "high": 1}],
           "segments": [{"segment": "Up", "cagr": 0.3, "note": "n"}],
           "published": [], "assumptions": ["a"],
           "eps_estimates": [{"stock_id": "1", "fiscal_year": 2026, "eps": 12.0, "source": "s1"},
                             {"stock_id": "1", "fiscal_year": 2027, "eps": 15.0, "source": "s2"},
                             {"stock_id": "2", "fiscal_year": 2027, "eps": -1.0, "source": "s"},
                             {"stock_id": "9", "fiscal_year": 2027, "eps": 5.0, "source": "s"},
                             {"stock_id": "3", "fiscal_year": 2025, "eps": 5.0, "source": "s"}]}
    f = forecast.from_claude(raw, ["1", "2", "3"], base_year=2026)
    assert [p["year"] for p in f["points"]] == list(range(2026, 2033))
    assert f["points"][0] == {"year": 2026, "low": 1.0, "base": 2.0, "high": 3.0}  # reordered
    assert f["cagr"] == pytest.approx(7 ** (1 / 6) - 1)
    assert f["eps_estimates"] == {"1": {"fiscal_year": 2027, "eps": 15.0, "source": "s2"}}
    assert forecast.from_claude({**raw, "points": raw["points"][:1]}, ["1"], base_year=2026) is None


def test_forward_pe_prefers_analyst_estimate():
    p = stock("1")
    f = forecast.forward_pe(p, {"1": {"fiscal_year": 2027, "eps": 20.0, "source": "https://x"}})
    assert f["basis"] == "analyst" and f["per"] == pytest.approx(25.0) and f["growth"] == pytest.approx(1.0)
    f = forecast.forward_pe(p)
    assert f["basis"] == "extrapolated" and f["eps"] == pytest.approx(14.0) and f["per"] == pytest.approx(500 / 14)


def test_forward_pe_skips_loss_makers_and_financials_and_clips():
    assert forecast.forward_pe(stock("1", eps_ttm=-2.0))["basis"] == "none"
    fin = stock("1")
    fin.is_financial = True
    assert forecast.forward_pe(fin)["basis"] == "none"
    assert forecast.forward_pe(stock("1", rev_3m_yoy=5.0))["growth"] == pytest.approx(1.0)
    assert forecast.forward_pe(stock("1", rev_3m_yoy=math.nan))["growth"] == pytest.approx(0.25)  # 12m fallback


def test_forward_pe_signals():
    p = stock("1", per_fwd=30.0)
    assert any("earnings expected to grow" in x for x in theme.data_signals(p, {}, 3)["opportunities"])
    p = stock("1", per_fwd=70.0)
    assert any("earnings expected to fall" in x for x in theme.data_signals(p, {}, 3)["threats"])


# --------------------------------------------------------------------------- Claude (fake client)

MARKET = llm.MarketForecast(
    market="CPO optical engines", unit="US$ billion",
    points=[llm.ForecastPoint(year=y, low=1.0 + i, base=2.0 + 2 * i, high=3.0 + 3 * i)
            for i, y in enumerate(range(2026, 2033))],
    published=[llm.PublishedForecast(publisher="Firm", published="2026-03", scope="CPO ports", figure="US$10bn by 2030")],
    segments=[llm.SegmentOutlook(segment="Up", cagr=0.4, note="n")], assumptions=["a"],
    eps_estimates=[llm.EPSEstimate(stock_id="1", fiscal_year=2027, eps=20.0, source="https://x")])


@pytest.fixture
def fake(monkeypatch, tmp_path):
    calls = {"create": [], "parse": []}

    def create(**kw):
        calls["create"].append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="notes")])

    def parse(**kw):
        calls["parse"].append(kw)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=MARKET)

    msgs = SimpleNamespace(create=create, parse=parse)
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path)
    return calls


def test_forecast_market_prompts_and_caches(fake):
    t = make_theme("CPO")
    out = llm.forecast_market(t, ["Laser", "Optics"])
    llm.forecast_market(t, ["Laser", "Optics"])
    assert len(fake["parse"]) == 1 and out["points"][0]["year"] == 2026
    assert fake["create"][0]["tools"][0]["max_uses"] == 15
    prompt = fake["create"][0]["messages"][0]["content"]
    assert "3081" in prompt and "Laser, Optics" in prompt and "法人預估" in prompt
    assert fake["parse"][0]["output_format"] is llm.MarketForecast


def test_analyze_with_claude_forecast(fake, monkeypatch):
    monkeypatch.setattr(theme, "profile_stock", lambda sid, *a, **k: stock(sid, segment=""))
    monkeypatch.setattr(llm, "research_theme", lambda *a, **k: {"summary": "S", "segments": [], "roles": [],
                                                               "candidates": []})
    monkeypatch.setattr(llm, "swot_analysis", lambda *a, **k: {})
    t = theme.Theme("T", "T", stock_ids=["1", "2"], segments=[{"name": "Up", "stage": "upstream", "description": ""}],
                    roles={"1": {"segment": "Up", "role": "r"}, "2": {"segment": "Up", "role": "r"}})
    r = theme.analyze(t, news_days=0, use_llm=True)
    by = {p.stock_id: p for p in r.profiles}
    assert by["1"].forward["basis"] == "analyst" and by["2"].forward["basis"] == "extrapolated"
    assert by["1"].metrics["per_fwd"] == pytest.approx(25.0)
    assert r.market_forecast["cagr"] == pytest.approx((14 / 2) ** (1 / 6) - 1) and r.projection is not None
    assert r.medians["per_fwd"] == pytest.approx((25.0 + 500 / 14) / 2)


def test_analyze_reports_forecast_failure(fake, monkeypatch):
    monkeypatch.setattr(theme, "profile_stock", lambda sid, *a, **k: stock(sid))
    monkeypatch.setattr(llm, "research_theme", lambda *a, **k: {"summary": "S", "segments": [], "roles": [],
                                                               "candidates": []})
    monkeypatch.setattr(llm, "swot_analysis", lambda *a, **k: {})

    def fail(*a, **k):
        raise llm.LLMError("rate limit")
    monkeypatch.setattr(llm, "forecast_market", fail)
    r = theme.analyze(theme.Theme("T", "T", stock_ids=["1"]), news_days=0, use_llm=True)
    assert r.forecast_error == "rate limit" and r.llm_error is None and r.market_forecast is None
    assert r.profiles[0].forward["basis"] == "extrapolated"


# --------------------------------------------------------------------------- report

def result(lang="en", claude=True):
    ps = [stock("1"), stock("2", eps_ttm=-1.0)]
    keys = ["gross_margin", "gross_margin_chg", "operating_margin", "roe_ttm", "per_pct", "dividend_yield",
            "revenue_ttm"]
    for p in ps:
        p.metrics.update({k: math.nan for k in keys})
        p.forward = forecast.forward_pe(p, {"1": {"fiscal_year": 2027, "eps": 20.0, "source": "https://x"}}
                                        if claude else None)
        p.metrics["per_fwd"] = p.forward["per"]
    segs = theme.segment_summary([{"name": "Up", "stage": "upstream", "description": ""}], ps)
    raw = {**MARKET.model_dump(), "sources": [{"title": "Src", "url": "https://src"}]}
    return ThemeResult(theme.Theme("T", "T"), ps, {"per": 50.0, "per_fwd": 25.0}, 3, segments=segs, lang=lang,
                       market_forecast=forecast.from_claude(raw, ["1", "2"], 2026) if claude else None,
                       projection=forecast.extrapolate(ps, segs))


def test_render_outlook_and_valuation():
    html = theme_report.render(result())
    for text in ["Market outlook", "Market size forecast", "CPO optical engines", "Firm", "Forward valuation",
                 "analyst estimate, FY2027", "Tracked companies grew", "Forecast CAGR (Claude)", "c_pe",
                 "https://src"]:
        assert text in html, text
    assert "25.0×" in html  # forward P/E from the analyst estimate
    assert "is 50% of the tracked companies’ combined revenue" in html


def test_render_without_claude_and_in_chinese():
    html = theme_report.render(result(claude=False))
    assert "Turn on Claude research for published market-size forecasts" in html and "c_mkt" not in html
    assert "extrapolated" in html and "Forecast CAGR (Claude)" not in html
    zh = theme_report.render(result("zh-TW"))
    assert "市場展望" in zh and "預估本益比" in zh and "法人預估，2027 年度" in zh


# --------------------------------------------------------------------------- implied prices

def priced(sid="1", **m):
    p = stock(sid, **{"per": 20.0, "per_q25": 10.0, "per_q50": 15.0, "per_q75": 25.0, **m})
    p.latest_quarter = "2026Q3"
    p.forward = forecast.forward_pe(p)
    return p


def test_eps_path_uses_estimates_then_outlook_growth():
    p = priced()  # EPS TTM 10, forward EPS 14 (revenue +40%)
    path = forecast.eps_path(p, None, 0.1)
    assert [r["year"] for r in path] == [2027, 2028, 2029]
    assert [r["source"] for r in path] == ["extrapolated", "projected", "projected"]
    assert [r["eps"] for r in path] == pytest.approx([14.0, 15.4, 16.94])
    path = forecast.eps_path(p, {"2028": {"eps": 20.0, "source": "s"}}, 0.1)  # keys may be strings after JSON
    assert [r["source"] for r in path] == ["extrapolated", "analyst", "projected"]
    assert path[2]["eps"] == pytest.approx(22.0)
    assert forecast.eps_path(p, None, 0.1, momentum=False)[0]["eps"] == pytest.approx(11.0)
    assert forecast.eps_path(priced(eps_ttm=-1.0), None, 0.1) == []


def test_pe_band_history_cap_below_and_group():
    assert forecast.pe_band(priced()) == {"low": 10.0, "base": 15.0, "high": 25.0, "basis": "history"}
    assert forecast.pe_band(priced(per_q75=80.0))["high"] == pytest.approx(30.0)  # 2 × median
    assert forecast.pe_band(priced(per=4.0)) == {"low": 4.0, "base": 4.0, "high": 10.0, "basis": "below"}
    assert forecast.pe_band(priced(per_q50=math.nan), 18.0)["basis"] == "group"
    assert forecast.pe_band(priced(per_q50=math.nan)) is None


def test_price_forecast_prices_and_returns():
    pf = forecast.price_forecast(priced(), end_growth=0.1, today=date(2026, 12, 31))
    last = pf["years"][-1]
    assert (last["low"], last["base"], last["high"]) == pytest.approx((169.4, 254.1, 423.5))
    assert pf["upside"] == pytest.approx(254.1 / 500 - 1) and not pf["peak"]
    assert pf["annual"] == pytest.approx((254.1 / 500) ** (1 / pf["years_ahead"]) - 1)
    assert pf["years_ahead"] == pytest.approx(3, abs=0.01)
    peak = forecast.price_forecast(priced(per=4.0), end_growth=0.5)  # end growth is capped at 15%
    assert peak["peak"] and peak["end_growth"] == 0.15 and peak["years"][0]["source"] == "projected"
    assert peak["years"][-1]["base"] == pytest.approx(10 * 1.15 ** 3 * 4.0)
    assert forecast.price_forecast(priced(eps_ttm=-1.0))["years"] == []


def test_from_claude_keeps_estimates_by_year():
    raw = {"points": [{"year": y, "low": 1, "base": 2, "high": 3} for y in (2026, 2027)],
           "eps_estimates": [{"stock_id": "1", "fiscal_year": y, "eps": e, "source": "s"}
                             for y, e in ((2027, 12.0), (2028, 15.0), (2030, 30.0))]}
    f = forecast.from_claude(raw, ["1"], base_year=2026)
    assert sorted(f["eps_by_year"]["1"]) == [2027, 2028]  # 2030 is past the price horizon
    assert f["eps_estimates"]["1"]["fiscal_year"] == 2027


def test_end_growth_prefers_segment_then_theme():
    p = stock("1", segment="Up")
    assert theme.end_growth(p, {"segments": [{"segment": "Up", "cagr": 0.3}], "cagr": 0.2}, None) == 0.3
    assert theme.end_growth(p, None, {"segments": [], "cagr": 0.12}) == 0.12
    assert theme.end_growth(p, None, None) == forecast.LONG_RUN_GROWTH


def test_render_price_forecast():
    r = result()
    r.profiles[0].price = forecast.price_forecast(priced(), end_growth=0.1)
    peak = priced("3", per=4.0)
    peak.name = "Peak"
    peak.metrics.update({k: math.nan for k in ["gross_margin", "gross_margin_chg", "operating_margin", "roe_ttm",
                                               "per_pct", "dividend_yield", "revenue_ttm"]})
    peak.price = forecast.price_forecast(peak, end_growth=0.1)
    r.profiles.append(peak)
    html = theme_report.render(r)
    for text in ["Price forecast", "EPS FY2029", "FY2029 base", "c_price", "⚠", "1 of 2 companies trade below",
                 "No implied price", "Co2"]:
        assert text in html, text
    zh = theme_report.render(ThemeResult(**{**r.__dict__, "lang": "zh-TW"}))
    assert "股價推估" in zh
    for q in r.profiles:
        q.price = {}
    assert "older than price forecasts" in theme_report.render(r)


def test_stock_report_price_section(monkeypatch):
    from tw_fundamental import report
    p = priced(per=8.0)  # below its usual band
    pf = forecast.price_forecast(p, None, 0.1, today=date(2026, 11, 4))
    outlook = {"id": "20261104-120000-abcdef01", "title": "Memory", "created": "2026-11-04T12:00:00",
               "claude": False, "segment": "Chipmakers", "end_growth": 0.1}
    body, chart = report._price_section(pf, outlook, translator("en"), lambda rid: f"/theme/saved/{rid}")
    assert "<a href='/theme/saved/20261104-120000-abcdef01'>Memory</a>" in body and "Chipmakers" in body
    assert "Possible earnings peak" in body and "no analysts’ estimates" in body and "FY2029" in body
    assert chart["id"] == "price" and len(chart["traces"][0]["y"]) == 3
    body, _ = report._price_section(forecast.price_forecast(priced(), None, 0.1), None, translator("zh-TW"))
    assert "近 30 天沒有包含此股的主題分析" in body
    body, chart = report._price_section(forecast.price_forecast(priced(eps_ttm=-1.0)), None, translator("en"))
    assert chart is None and "No implied price" in body


def test_price_steps_show_every_calculation():
    from tw_fundamental import report
    en, zh = translator("en"), translator("zh-TW")
    p = forecast.price_forecast(priced(per_q75=40.0), {2028: {"eps": 20.0, "source": "Broker X"}}, 0.1,
                                today=date(2026, 12, 31))  # EPS 10 -> 14 (revenue), 20 (analyst), 22 (outlook)
    steps = report.price_steps(p, en)
    assert "EPS 14.00 = latest EPS 10.00 × (1 +40.0%)" in steps and "revenue growth over the last 3 months" in steps
    assert "EPS 20.00: analysts’ estimate (Broker X)." in steps
    assert "EPS 22.00 = previous year’s EPS 20.00 × (1 +10.0%)" in steps
    assert "Low 22.00 × 10.00 = 220; base 22.00 × 15.00 = 330; high 22.00 × 30.00 = 660." in steps
    assert "twice the median" in steps  # the 75th percentile (40×) was capped
    assert "330 ÷ 500.0 − 1 = -34.0%" in steps
    assert report.price_formula(p, en) == "FY2029 base NT$ 330 = EPS 22.00 × P/E 15.00×"
    assert "2029 年基準 NT$ 330" in report.price_formula(p, zh) and "每股盈餘" not in report.price_steps(p, zh)

    below = forecast.price_forecast(priced(per=8.0), None, 0.1)
    steps = report.price_steps(below, en)
    assert "today’s P/E, below its usual band" in steps and "EPS 11.00 = latest EPS 10.00 × (1 +10.0%): outlook growth" in steps
    group = forecast.price_forecast(priced(per_q50=math.nan), None, 0.1, group_pe=12.0)
    assert "group median" in report.price_steps(group, en)


def test_analyst_loss_year_has_no_price_in_the_steps():
    from tw_fundamental import report
    pf = forecast.price_forecast(priced(), {2029: {"eps": -2.0, "source": "s"}}, 0.1)
    assert "EPS is not positive" in report.price_steps(pf, translator("en"))
    assert "is not positive" in report.price_formula(pf, translator("en"))


def test_reports_include_the_calculation():
    r = result()
    p = r.profiles[0]
    p.metrics.update(per_q25=10.0, per_q50=15.0, per_q75=25.0, per=20.0)
    p.latest_quarter = "2026Q3"
    p.price = forecast.price_forecast(p, None, 0.1)
    html = theme_report.render(r)
    assert "How each price is calculated" in html and "<details class='how'>" in html and "P/E 15.00×" in html
