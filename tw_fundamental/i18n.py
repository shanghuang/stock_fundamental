"""English / Traditional Chinese user-facing text.

Code keeps the English text inline, wrapped in `tr("...")`; locales/<lang>.json maps
that English text to the translation (gettext style). Templates use str.format
placeholders, and a translation must use the same placeholders (tests check this).

    tr = translator("zh-TW")
    tr("Gross margin {v:.1%}", v=0.42)  ->  "毛利率 42.0%"
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Callable

LANGS = {"en": "English", "zh-TW": "繁體中文"}
DEFAULT_LANG = "en"
HTML_LANG = {"en": "en", "zh-TW": "zh-Hant-TW"}
LOCALE_DIR = Path(__file__).with_name("locales")

Translator = Callable[..., str]


def normalize(lang: str | None) -> str:
    """Map anything Chinese (zh, zh-TW, zh-Hant, zh_tw) to zh-TW; unknown values to the default."""
    v = (lang or "").strip().replace("_", "-").lower()
    if v.startswith("zh"):
        return "zh-TW"
    return "en" if v.startswith("en") else DEFAULT_LANG


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    if lang == "en":
        return {}
    return json.loads((LOCALE_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def translator(lang: str) -> Translator:
    cat = catalog(normalize(lang))

    def tr(msg: str, **kw) -> str:
        text = cat.get(msg, msg)
        return text.format(**kw) if kw else text
    return tr


def N_(msg: str) -> str:
    """Mark a constant for translation where `tr` is applied later (e.g. in a table definition)."""
    return msg


def pick(value, lang: str) -> str:
    """A preset field that is either plain text or {"en": ..., "zh-TW": ...}."""
    if isinstance(value, dict):
        return value.get(normalize(lang)) or value.get(DEFAULT_LANG) or next(iter(value.values()), "")
    return value or ""
