"""Saved theme analyses, so a run can be reopened without fetching data or calling Claude again.

Each run is a ThemeResult serialized to .cache/themes/<id>.json, with a small <id>.meta.json
beside it for listing. A request with the same parameters reuses the newest saved run for
MAX_AGE_HOURS; after that, or when the user asks to run again, a new run is saved.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timedelta

import numpy as np

from . import finmind
from .theme import StockProfile, Theme, ThemeResult

VERSION = 1
MAX_AGE_HOURS = 12
OUTLOOK_MAX_DAYS = 30  # a single-stock report borrows a theme run's outlook for this long
KEEP = 200
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


def store_dir():
    return finmind.CACHE_DIR / "themes"


def params_key(theme: Theme, news_days: int, use_llm: bool, lang: str) -> str:
    """Identifies a run's inputs: the resolved theme (after presets and overrides) and the options."""
    payload = {"theme": theme.key, "keywords": theme.keywords, "stocks": theme.stock_ids,
               "days": news_days, "llm": bool(use_llm), "lang": lang}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8]


def _json_default(v):
    if isinstance(v, np.generic):
        return v.item()
    raise TypeError(f"not JSON serializable: {type(v).__name__}")


def _write(path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=_json_default), encoding="utf-8")
    os.replace(tmp, path)


def _summary(r: ThemeResult) -> dict:
    outlook = r.market_forecast or r.projection or {}
    return {"title": r.theme.title, "theme": r.theme.key, "lang": r.lang, "news_days": r.news_days,
            "claude": r.research is not None, "stocks": sum(1 for p in r.profiles if p.metrics),
            "keywords": r.theme.keywords, "cagr": outlook.get("cagr"), "cagr_source": outlook.get("source"),
            "per_fwd": r.medians.get("per_fwd"), "warnings": bool(r.llm_error or r.forecast_error),
            "stock_ids": [p.stock_id for p in r.profiles if p.metrics]}


def save(r: ThemeResult, key: str, query: dict, owner: str = "") -> str:
    """Store a run; `query` holds the request parameters needed to run it again, `owner` the username."""
    created = datetime.now()
    rid = f"{created:%Y%m%d-%H%M%S}-{key}"
    meta = {"version": VERSION, "id": rid, "key": key, "created": created.isoformat(timespec="seconds"),
            "query": query, "owner": owner, **_summary(r)}
    d = store_dir()
    d.mkdir(parents=True, exist_ok=True)
    _write(d / f"{rid}.json", {"version": VERSION, "result": asdict(r)})
    _write(d / f"{rid}.meta.json", meta)
    prune()
    return rid


def list_saved() -> list[dict]:
    """Metadata of every saved run, newest first."""
    d = store_dir()
    out = []
    for f in d.glob("*.meta.json") if d.exists() else []:
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("version") == VERSION and ID_RE.match(meta.get("id", "")):
            out.append(meta)
    return sorted(out, key=lambda m: m["created"], reverse=True)


def find_recent(key: str, max_age_hours: float = MAX_AGE_HOURS) -> dict | None:
    now = datetime.now()
    for meta in list_saved():
        age = (now - datetime.fromisoformat(meta["created"])).total_seconds() / 3600
        if meta["key"] == key and age < max_age_hours:
            return meta
    return None


def stock_outlook(stock_id: str, max_age_days: float = OUTLOOK_MAX_DAYS) -> dict | None:
    """What the newest theme run that priced this stock assumed about it, for its own report.

    Prefers a run with Claude research (it may have analysts' EPS estimates). Returns the run's id,
    title and time, the stock's segment, its outlook growth and any EPS estimates; None if no run
    within `max_age_days` priced the stock.
    """
    cutoff = datetime.now() - timedelta(days=max_age_days)
    found = None
    for meta in list_saved():
        if datetime.fromisoformat(meta["created"]) < cutoff:
            break
        if stock_id not in meta.get("stock_ids", [stock_id]) or (found and not meta.get("claude")):
            continue
        loaded = load(meta["id"])
        p = next((p for p in loaded[1].profiles if p.stock_id == stock_id), None) if loaded else None
        if p is None or not p.price.get("years"):
            continue
        mf = loaded[1].market_forecast or {}
        found = {"id": meta["id"], "title": meta["title"], "created": meta["created"],
                 "claude": bool(meta.get("claude")), "segment": p.segment,
                 "end_growth": p.price["end_growth"],
                 "eps_by_year": mf.get("eps_by_year", {}).get(stock_id),
                 "eps_estimate": mf.get("eps_estimates", {}).get(stock_id)}
        if found["claude"]:
            break
    return found


def get_meta(rid: str) -> dict | None:
    if not ID_RE.match(rid):
        return None
    try:
        return json.loads((store_dir() / f"{rid}.meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load(rid: str) -> tuple[dict, ThemeResult] | None:
    if not ID_RE.match(rid):
        return None
    d = store_dir()
    try:
        meta = json.loads((d / f"{rid}.meta.json").read_text(encoding="utf-8"))
        doc = json.loads((d / f"{rid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if doc.get("version") != VERSION:
        return None
    raw = dict(doc["result"])
    raw["theme"] = Theme(**raw["theme"])
    raw["profiles"] = [StockProfile(**p) for p in raw["profiles"]]
    return meta, ThemeResult(**raw)


def delete(rid: str) -> bool:
    if not ID_RE.match(rid):
        return False
    found = False
    for suffix in (".json", ".meta.json"):
        path = store_dir() / f"{rid}{suffix}"
        if path.exists():
            path.unlink()
            found = True
    return found


def prune(keep: int = KEEP) -> None:
    for meta in list_saved()[keep:]:
        delete(meta["id"])
