"""Thin FinMind REST client with an on-disk JSON cache.

Works without a token (rate-limited to roughly 300 requests/hour). Set the
FINMIND_TOKEN environment variable to use a registered account's higher limit.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pandas as pd
import requests

API_URL = "https://api.finmindtrade.com/api/v4/data"
CACHE_DIR = Path(os.environ.get("TWF_CACHE_DIR", ".cache"))


class FinMindError(RuntimeError):
    pass


def fetch(dataset: str, stock_id: str | None = None, start_date: str | None = None,
          *, max_age_hours: float = 12, refresh: bool = False) -> pd.DataFrame:
    """Fetch one FinMind dataset as a DataFrame, reusing a cached copy if fresh."""
    if stock_id and not re.fullmatch(r"[0-9A-Za-z]+", stock_id):
        raise FinMindError(f"Invalid stock id: {stock_id!r}")  # also keeps cache paths safe
    params = {"dataset": dataset}
    if stock_id:
        params["data_id"] = stock_id
    if start_date:
        params["start_date"] = start_date

    cache_file = CACHE_DIR / f"{dataset}_{stock_id or 'all'}_{start_date or 'all'}.json"
    if not refresh and cache_file.exists():
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_hours < max_age_hours:
            return pd.DataFrame(json.loads(cache_file.read_text()))

    headers = {}
    if token := os.environ.get("FINMIND_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(API_URL, params=params, headers=headers, timeout=30)
    try:
        payload = resp.json()
    except ValueError:
        raise FinMindError(f"{dataset}: HTTP {resp.status_code}, non-JSON response") from None
    if payload.get("status") != 200:
        raise FinMindError(f"{dataset}: {payload.get('msg', 'unknown error')} (status {payload.get('status')})")

    data = payload.get("data", [])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, ensure_ascii=False))
    return pd.DataFrame(data)
