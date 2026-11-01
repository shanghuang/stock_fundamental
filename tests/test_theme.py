from types import SimpleNamespace

import pandas as pd
import pytest

from tw_fundamental import llm, theme
from tw_fundamental.theme import StockProfile, data_signals, make_theme


def test_make_theme_preset_is_case_insensitive_and_overridable():
    t = make_theme("cpo")
    assert t.key == "CPO" and "3363" in t.stock_ids
    t2 = make_theme("CPO", keywords=["矽光子"], stock_ids=["2330", "2330", "3363"])
    assert t2.keywords == ["矽光子"] and t2.stock_ids == ["2330", "3363"]


def test_make_theme_ad_hoc():
    t = make_theme("Robotics")
    assert t.title == "Robotics" and t.keywords == ["Robotics"] and t.stock_ids == []


def test_theme_headlines_filters_and_dedupes(monkeypatch):
    rows = pd.DataFrame([
        {"date": "2026-11-01 09:00", "title": "上詮 CPO 出貨 - 經濟日報", "source": "UDN", "link": "https://a"},
        {"date": "2026-11-01 10:00", "title": "上詮 CPO 出貨 - CMoney", "source": "CMoney", "link": "https://b"},
        {"date": "2026-11-01 11:00", "title": "上詮 股東會", "source": "X", "link": "javascript:alert(1)"},
        {"date": "2026-11-01 12:00", "title": "矽光子 新訂單", "source": "X", "link": "javascript:alert(1)"},
    ])
    monkeypatch.setattr(theme, "fetch", lambda *a, **k: rows)
    out = theme.theme_headlines("3363", ["cpo", "矽光子"], days=1)
    assert [h["title"] for h in out] == ["上詮 CPO 出貨 - 經濟日報", "矽光子 新訂單"]
    assert out[1]["link"] == ""  # non-http links dropped


def profile(**m):
    base = dict(rev_3m_yoy=0.5, rev_12m_yoy=0.3, gross_margin=0.5, gross_margin_chg=0.02, net_margin=0.2,
                roe_ttm=0.2, ocf_to_ni=1.2, fcf_ttm=1e9, debt_ratio=0.3, current_ratio=2.0,
                per=50.0, per_pct=0.9, eps_ttm=10.0)
    base.update(m)
    return StockProfile("1234", "Test", metrics=base, headlines=[{"date": "", "title": "", "source": "", "link": ""}])


MED = {"gross_margin": 0.3, "per": 20.0}


def test_data_signals_strong_company():
    s = data_signals(profile(), MED, news_days=3)
    assert any("High ROE" in x for x in s["strengths"])
    assert any("accelerating" in x for x in s["opportunities"])
    assert any("1 theme-related headline" in x for x in s["opportunities"])
    assert any("above 90%" in x for x in s["threats"])
    assert any("premium to the group median" in x for x in s["threats"])
    assert s["weaknesses"] == []


def test_data_signals_weak_company():
    s = data_signals(profile(roe_ttm=-0.05, net_margin=-0.1, fcf_ttm=-2e8, gross_margin=0.1,
                             rev_12m_yoy=-0.1, rev_3m_yoy=-0.3, eps_ttm=-1.0, per=float("nan"),
                             per_pct=float("nan"), debt_ratio=0.7, current_ratio=0.8), MED, 3)
    w = " | ".join(s["weaknesses"])
    for text in ["Low ROE", "Loss-making", "Negative free cash flow", "below the group median",
                 "High leverage", "Current ratio", "Revenue down"]:
        assert text in w
    assert any("decelerating" in x for x in s["threats"])
    assert any("No earnings support" in x for x in s["threats"])


def test_data_signals_skip_leverage_for_financials():
    p = profile(debt_ratio=0.92, current_ratio=0.5)
    p.is_financial = True
    s = data_signals(p, MED, 3)
    assert not any("leverage" in x or "Current ratio" in x for x in s["weaknesses"])


# --------------------------------------------------------------------------- supply chain

def test_presets_derive_stocks_and_roles_from_segments():
    t = theme.load_presets()["CPO"]
    assert t.stock_ids[0] == "3081"  # flow order: upstream segment first
    assert t.roles["3363"]["segment"] == "Optical components & fiber connectivity"
    assert [s["stage"] for s in t.segments][:2] == ["upstream", "upstream"]
    assert make_theme("cpo", stock_ids=["3363"]).roles["3363"]["role"].startswith("Fiber array")


def seg_theme():
    return theme.Theme("T", "T", stock_ids=["1111", "2222", "3333", "4444"],
                       segments=[{"name": "Down", "stage": "downstream", "description": ""},
                                 {"name": "Up", "stage": "upstream", "description": ""}],
                       roles={"1111": {"segment": "Up", "role": "laser"},
                              "2222": {"segment": "Up", "role": "epi"},
                              "3333": {"segment": "Down", "role": "module"}})


def test_assign_roles_priority_and_order():
    ps = [StockProfile(sid, industry="通信網路業 / 電子工業") for sid in ["1111", "2222", "3333", "4444"]]
    research = {"segments": [{"name": "Mid", "stage": "midstream", "description": "d"}],
                "roles": [{"stock_id": "2222", "segment": "Mid", "role": "engine", "exposure": "indirect"}]}
    segs = theme.assign_roles(seg_theme(), ps, research)
    assert [s["name"] for s in segs] == ["Up", "Mid", "Down", "Industry: 通信網路業"]
    by = {p.stock_id: p for p in ps}
    assert (by["1111"].segment, by["1111"].role_source) == ("Up", "preset")
    assert (by["2222"].segment, by["2222"].role_source, by["2222"].exposure) == ("Mid", "Claude", "indirect")
    assert by["4444"].role_source == "industry"


def test_segment_summary_and_leader_signals():
    ps = [profile() for _ in range(3)]
    for p, sid, gm, r3 in zip(ps, ["1", "2", "3"], [0.5, 0.3, 0.1], [0.2, 0.9, -0.1]):
        p.stock_id, p.segment, p.role_source = sid, "Up", "preset"
        p.metrics.update(gross_margin=gm, rev_3m_yoy=r3, revenue_ttm=1e9)
    segs = [{"name": "Up", "stage": "upstream", "description": ""},
            {"name": "Gap", "stage": "downstream", "description": ""}]
    summ = theme.segment_summary(segs, ps)
    assert summ[0]["stock_ids"] == ["1", "2", "3"] and summ[0]["gross_margin"] == pytest.approx(0.3)
    assert summ[0]["revenue_ttm"] == pytest.approx(3e9) and summ[1]["stock_ids"] == []
    s1, s3 = theme.segment_signals(ps[0], ps), theme.segment_signals(ps[2], ps)
    assert any("Highest gross margin" in x for x in s1["strengths"])
    assert any("Lowest gross margin" in x for x in s3["weaknesses"])
    assert any("Lowest revenue growth" in x for x in s3["threats"])
    assert theme.segment_signals(ps[0], ps[:2]) == {"strengths": [], "weaknesses": [],
                                                     "opportunities": [], "threats": []}


# --------------------------------------------------------------------------- LLM layer (fake client)

class FakeMessages:
    def __init__(self):
        self.create_calls, self.parse_calls = [], []

    def create(self, **kw):
        self.create_calls.append(kw)
        if len(self.create_calls) == 1:  # first turn: server-side loop pauses mid-search
            return SimpleNamespace(stop_reason="pause_turn", content=[
                SimpleNamespace(type="text", text="Part one."),
                SimpleNamespace(type="web_search_tool_result", content=[
                    SimpleNamespace(url="https://example.com/cpo", title="CPO outlook")]),
            ])
        return SimpleNamespace(stop_reason="end_turn", content=[
            SimpleNamespace(type="web_search_tool_result", content={"error_code": "max_uses_exceeded"}),
            SimpleNamespace(type="text", text="Part two."),
        ])

    def parse(self, **kw):
        self.parse_calls.append(kw)
        fmt = kw["output_format"]
        if fmt is llm.ThemeResearch:
            out = llm.ThemeResearch(summary="S", drivers=["d"], risks=["r"],
                                    segments=[llm.Segment(name="Fiber", stage="midstream", description="")],
                                    roles=[llm.RoleAssignment(stock_id="3363", segment="Fiber", role="FAU",
                                                              exposure="direct")],
                                    candidates=[llm.Candidate(stock_id="3363", name="上詮", role="FAU",
                                                              rationale="r")])
        else:
            out = llm.StockSWOT(stock_id="WRONG", role_in_theme="r", exposure="direct", strengths=["a"],
                                weaknesses=["b"], opportunities=["c"], threats=["d"], takeaway="t")
        return SimpleNamespace(stop_reason="end_turn", parsed_output=out)


@pytest.fixture
def fake_client(monkeypatch, tmp_path):
    msgs = FakeMessages()
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path)
    return msgs


def test_research_resumes_pause_turn_and_collects_sources(fake_client):
    r = llm.research_theme(make_theme("CPO"))
    assert len(fake_client.create_calls) == 2
    resumed = fake_client.create_calls[1]["messages"]
    assert [m["role"] for m in resumed] == ["user", "assistant"]  # no extra "continue" message
    notes = fake_client.parse_calls[0]["messages"][0]["content"]
    assert "Part one." in notes and "Part two." in notes
    assert r["sources"] == [{"title": "CPO outlook", "url": "https://example.com/cpo"}]
    assert r["candidates"][0]["stock_id"] == "3363"
    assert r["roles"][0]["segment"] == "Fiber"
    parse_prompt = fake_client.parse_calls[0]["messages"][0]["content"]
    assert "Tracked codes: 3081, 2455" in parse_prompt and "Fiber array units" in parse_prompt  # preset map sent
    call = fake_client.create_calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"


def test_research_is_cached(fake_client):
    t = make_theme("CPO")
    llm.research_theme(t)
    llm.research_theme(t)
    assert len(fake_client.parse_calls) == 1


def test_swot_uses_our_stock_id(fake_client):
    p = profile()
    out = llm.swot_analysis(make_theme("CPO"), {"summary": "S"}, [p], {"per": float("nan")})
    assert out["1234"]["stock_id"] == "1234" and out["1234"]["exposure"] == "direct"


def test_swot_failure_is_per_stock(fake_client, monkeypatch):
    def refuse(**kw):
        return SimpleNamespace(stop_reason="refusal", parsed_output=None)
    monkeypatch.setattr(fake_client, "parse", refuse)
    out = llm.swot_analysis(make_theme("CPO"), {"summary": "S"}, [profile()], {})
    assert "error" in out["1234"]
