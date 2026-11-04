import math
from datetime import datetime, timedelta

import pytest

from tw_fundamental import forecast, theme_store
from tw_fundamental.theme import StockProfile, Theme, ThemeResult


@pytest.fixture
def store(tmp_path):
    return tmp_path / "themes"  # conftest points theme_store here


def result(title="CPO"):
    p = StockProfile("2330", "台積電", metrics={"per": math.nan, "revenue_12m": 100e8, "rev_12m_yoy": 0.3,
                                               "eps_ttm": 10.0, "close": 500.0, "rev_3m_yoy": 0.2},
                     segment="Up", revenue_through="2026-10",
                     headlines=[{"date": "2026-11-01", "title": "t", "source": "s", "link": ""}])
    p.forward = forecast.forward_pe(p)
    t = Theme("CPO", title, keywords=["CPO"], stock_ids=["2330"],
              segments=[{"name": "Up", "stage": "upstream", "description": ""}])
    seg = [{"name": "Up", "stock_ids": ["2330"]}]
    return ThemeResult(t, [p], {"per_fwd": 41.7}, 3, swot={"2330": {"strengths": ["a"]}}, lang="zh-TW",
                       projection=forecast.extrapolate([p], seg), generated="2026-11-02T10:00")


def test_round_trip_keeps_everything():
    r = result()
    rid = theme_store.save(r, theme_store.params_key(r.theme, 3, False, "zh-TW"), {"name": "CPO", "days": 3})
    meta, back = theme_store.load(rid)
    assert back == r or (back.theme == r.theme and back.swot == r.swot)
    assert isinstance(back.profiles[0], StockProfile) and math.isnan(back.profiles[0].metrics["per"])
    assert back.projection["points"] == r.projection["points"] and back.generated == "2026-11-02T10:00"
    assert meta["title"] == "CPO" and meta["lang"] == "zh-TW" and meta["stocks"] == 1
    assert meta["cagr_source"] == "extrapolated" and meta["per_fwd"] == pytest.approx(41.7)


def test_list_find_recent_and_delete(store):
    r = result()
    key = theme_store.params_key(r.theme, 3, False, "en")
    old = theme_store.save(r, key, {})
    # Age the first run past the reuse window.
    meta_path = store / f"{old}.meta.json"
    meta_path.write_text(meta_path.read_text().replace(
        datetime.now().isoformat(timespec="seconds")[:13],
        (datetime.now() - timedelta(hours=13)).isoformat(timespec="seconds")[:13]))
    assert theme_store.find_recent(key) is None
    new = theme_store.save(result("CPO 2"), key, {})
    assert [m["id"] for m in theme_store.list_saved()][0] == new
    assert theme_store.find_recent(key)["id"] == new
    assert theme_store.find_recent(theme_store.params_key(r.theme, 7, False, "en")) is None
    assert theme_store.delete(new) and not theme_store.delete(new)
    assert theme_store.find_recent(key) is None


def test_params_key_depends_on_inputs():
    t = Theme("CPO", "CPO", keywords=["a"], stock_ids=["1"])
    k = theme_store.params_key(t, 3, False, "en")
    assert k == theme_store.params_key(Theme("CPO", "other title", keywords=["a"], stock_ids=["1"]), 3, False, "en")
    assert k != theme_store.params_key(t, 3, True, "en") != theme_store.params_key(t, 3, False, "zh-TW")


def test_prune_keeps_newest(monkeypatch):
    ids = []
    for i in range(3):
        monkeypatch.setattr(theme_store, "datetime", type("D", (datetime,), {
            "now": classmethod(lambda cls, i=i: datetime(2026, 11, 2, 10, 0, i))}))
        ids.append(theme_store.save(result(), "abcd1234", {}))
    theme_store.prune(keep=2)
    assert [m["id"] for m in theme_store.list_saved()] == ids[:0:-1]


def test_bad_ids_are_rejected():
    assert theme_store.load("../../etc/passwd") is None and not theme_store.delete("x")


def test_stock_outlook_prefers_claude_runs_with_a_price():
    def run(claude, growth, title):
        r = result(title)
        p = r.profiles[0]
        p.price = {"years": [{"year": 2027}], "end_growth": growth}
        if claude:
            r.research = {"summary": ""}
            r.market_forecast = {"eps_by_year": {"2330": {"2027": {"eps": 12.0, "source": "s"}}},
                                 "eps_estimates": {"2330": {"eps": 12.0, "fiscal_year": 2027, "source": "s"}}}
        return theme_store.save(r, theme_store.params_key(r.theme, 3, claude, "en"), {})

    assert theme_store.stock_outlook("2330") is None
    claude_rid = run(True, 0.12, "With Claude")
    plain_rid = run(False, 0.05, "Plain")  # newer, but without analysts' estimates
    o = theme_store.stock_outlook("2330")
    assert o["id"] == claude_rid != plain_rid and o["claude"] and o["end_growth"] == 0.12
    assert o["segment"] == "Up" and o["eps_by_year"]["2027"]["eps"] == 12.0 and o["eps_estimate"]["eps"] == 12.0
    assert theme_store.stock_outlook("2317") is None
    assert theme_store.get_meta(plain_rid)["stock_ids"] == ["2330"]
