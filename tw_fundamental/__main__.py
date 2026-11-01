"""CLI: python -m tw_fundamental 2330 [--years 5] [--open]"""
from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from .finmind import FinMindError
from .i18n import LANGS
from .pipeline import build_report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fundamental analysis report for a Taiwan-listed stock.")
    ap.add_argument("stock_id", help="e.g. 2330 (TSMC), 2317, 6488")
    ap.add_argument("--years", type=int, default=5, help="years of history to show (default 5)")
    ap.add_argument("--out", default="reports", help="output directory (default reports/)")
    ap.add_argument("--refresh", action="store_true", help="ignore the local cache")
    ap.add_argument("--open", action="store_true", help="open the report in a browser")
    ap.add_argument("--lang", choices=list(LANGS), default="en", help="report language (default en)")
    args = ap.parse_args(argv)

    try:
        r = build_report(args.stock_id, args.years, refresh=args.refresh, lang=args.lang)
    except FinMindError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    d, val = r.data, r.valuation
    print(f"{d.stock_id} {d.name}  ({d.industry})")
    print(f"  close {val.get('close', float('nan')):,.1f}  P/E {val.get('PER', float('nan')):.1f}  "
          f"P/B {val.get('PBR', float('nan')):.2f}  yield {val.get('dividend_yield', float('nan')):.2f}%")
    if d.is_financial:
        print("  note: financial company; leverage, liquidity and cash-flow checks skipped")
    for ok, text in r.checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {text}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / (f"{d.stock_id}.html" if args.lang == "en" else f"{d.stock_id}.{args.lang}.html")
    path.write_text(r.html, encoding="utf-8")
    print(f"report: {path.resolve()}")
    if args.open:
        webbrowser.open(path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
