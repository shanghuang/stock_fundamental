import ast
import json
import string
from pathlib import Path

import pandas as pd
import pytest

from conftest import sign_in
from tw_fundamental import i18n, llm, theme, web
from tw_fundamental.analysis import checks
from tw_fundamental.theme import StockProfile, data_signals

PKG = Path(i18n.__file__).parent


def marked_strings() -> set[str]:
    """Every literal passed to tr(...) or N_(...) in the package."""
    out = set()
    for f in PKG.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("tr", "N_")
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                out.add(node.args[0].value)
    return out


def fields(s: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(s) if name is not None}


def test_every_marked_string_is_translated():
    missing = marked_strings() - set(i18n.catalog("zh-TW"))
    assert not missing, f"add to locales/zh-TW.json: {sorted(missing)}"


def test_catalog_has_no_stale_entries():
    stale = set(i18n.catalog("zh-TW")) - marked_strings()
    assert not stale, f"unused translations: {sorted(stale)}"


def test_translations_keep_placeholders():
    for en, zh in i18n.catalog("zh-TW").items():
        assert fields(en) == fields(zh), en


def test_translator_formats_and_falls_back():
    tr = i18n.translator("zh-TW")
    assert tr("Debt ratio {v:.1%} — threshold 60%", v=0.42) == "負債比率 42.0%，門檻 60%"
    assert tr("Not a catalog entry") == "Not a catalog entry"
    assert i18n.translator("en")("{n} stocks", n=3) == "3 stocks"


@pytest.mark.parametrize("raw,lang", [("zh-TW", "zh-TW"), ("zh_tw", "zh-TW"), ("zh-Hant", "zh-TW"),
                                      ("en-US", "en"), ("fr", "en"), (None, "en")])
def test_normalize(raw, lang):
    assert i18n.normalize(raw) == lang


def test_presets_have_both_languages():
    raw = json.loads((PKG / "themes.json").read_text(encoding="utf-8"))
    for key, t in raw.items():
        texts = [t["title"], t["description"]]
        for seg in t["segments"]:
            texts += [seg["name"], seg["description"], *seg["stocks"].values()]
        for v in texts:
            assert set(v) == {"en", "zh-TW"} and all(v.values()), (key, v)
    zh = theme.load_presets("zh-TW")["CPO"]
    assert zh.title.startswith("CPO 共同封裝光學") and zh.roles["3363"]["segment"] == "光學元件與光纖連接"
    assert theme.make_theme("cpo", lang="zh-TW").segments[0]["name"] == "雷射與 III-V 族晶片"


def test_checks_and_signals_in_chinese():
    q = pd.DataFrame({"revenue": [1.0], "roe_ttm": [0.2], "revenue_yoy": [0.1], "ocf_to_net_income_ttm": [1.2],
                      "debt_ratio": [0.3], "current_ratio": [2.0], "gross_margin": [None], "fcf": [None]})
    texts = [t for _, t in checks(q, pd.DataFrame(), pd.DataFrame(), lang="zh-TW")]
    assert "ROE（近四季）20.0%，門檻 15%" in texts
    p = StockProfile("1", metrics={"roe_ttm": 0.2, "debt_ratio": 0.3})
    assert "ROE 高（近四季）20.0%" in data_signals(p, {}, 3, lang="zh-TW")["strengths"]


def test_claude_writes_in_the_chosen_language():
    assert "Traditional Chinese" in llm._system("zh-TW") and "Write in English" in llm._system("en")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web, "list_stocks", lambda: [{"id": "2330", "name": "台積電", "market": "twse",
                                                       "industry": "半導體業"}])
    web.app.config["TESTING"] = True
    c = web.app.test_client()
    sign_in(c)
    return c


def test_web_follows_accept_language(client):
    body = client.get("/", headers={"Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}).get_data(as_text=True)
    assert "台股基本面分析" in body and 'lang="zh-Hant-TW"' in body and "English</a>" in body
    body = client.get("/", headers={"Accept-Language": "en-US,en"}).get_data(as_text=True)
    assert "Taiwan stock fundamentals" in body and "繁體中文</a>" in body


def test_web_lang_param_is_remembered(client):
    r = client.get("/?lang=zh-TW", headers={"Accept-Language": "en"})
    assert "台股基本面分析" in r.get_data(as_text=True)
    assert "lang=zh-TW" in r.headers["Set-Cookie"]
    # The cookie now wins over the browser's preference.
    assert "台股基本面分析" in client.get("/", headers={"Accept-Language": "en"}).get_data(as_text=True)


def test_lang_switch_keeps_query_and_drops_refresh(client):
    with web.app.test_request_context("/report/2330?years=8&refresh=1"):
        html = web._lang_switch()
    assert "/report/2330?years=8&amp;lang=zh-TW" in html and "refresh" not in html
