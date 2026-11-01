# tw_fundamental

Single-stock fundamental analysis reports for Taiwan-listed (TWSE 上市 / TPEx 上櫃) companies,
built on the free [FinMind](https://finmindtrade.com/) API.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Optional: register at FinMind and `export FINMIND_TOKEN=...` for higher rate limits.
Without a token you get roughly 300 requests/hour, and one report uses 8.

## Web UI

```bash
.venv/bin/python -m tw_fundamental.web            # http://127.0.0.1:8000
.venv/bin/python -m tw_fundamental.web --port 9000
```

Search by code or name (e.g. `2330` or `台積電`), filter by market or industry, and press Enter or
click a row to open its report. The first load of a stock takes a few seconds. After that it is
served from the 12-hour cache. "Refresh data" on a report re-downloads from FinMind.

## Themes and SWOT

Open **Themes** in the web UI, or use the command line:

```bash
.venv/bin/python -m tw_fundamental.theme CPO --open              # preset: CPO, AI-server, cooling
.venv/bin/python -m tw_fundamental.theme CPO --days 7            # scan a week of news
.venv/bin/python -m tw_fundamental.theme 機器人 --stocks 2049,1590 --keywords 機器人,robot
ANTHROPIC_API_KEY=... .venv/bin/python -m tw_fundamental.theme CPO --llm
```

For every stock in the theme you get:

- **Comparison**: 3- and 12-month revenue growth, margins and margin trend, ROE, P/E (and where it
  sits in its own 5-year range), yield, and the group median for each.
- **Theme headlines**: FinMind news from the last N days, filtered to the theme's keywords.
- **SWOT**: without Claude, rule-based signals that each cite the number behind them. With `--llm`,
  Claude (`claude-opus-5-5`) researches the theme with web search, adds candidate stocks it finds
  (validated against the listed-company list), and writes a qualitative SWOT for each company,
  grounded in the same metrics and headlines. Claude results are cached for a day in `.cache/llm/`.

Presets live in `tw_fundamental/themes.json`, which you can edit to add your own themes.

Each report includes a **supply-chain map**: segments from upstream to downstream, each company's
role, and per-segment medians (revenue growth, margins, P/E) so you can see which part of the chain
is growing or earning the most. Roles come from Claude's research when it is on, otherwise from the
preset (`segments` in `themes.json`, each mapping stock codes to a role). Stocks that neither covers are
grouped by FinMind industry. The SWOT also flags the highest and lowest gross margin and revenue
growth within a segment once it has at least 3 companies.

Rate limits: FinMind news is one request per stock per day scanned. Past days are cached for good,
but a first run of a 16-stock theme with 3 days of news uses about 180 of the roughly 300
requests per hour allowed without a token.

## Languages

The web UI, reports and Claude's research are available in English and Traditional Chinese (繁體中文).
The web UI follows the browser's language; the switch at the top of each page changes it and is
remembered in a cookie (`?lang=zh-TW` or `?lang=en` works too). On the command line pass
`--lang zh-TW`; the file is then written as `reports/2330.zh-TW.html`.

UI text stays in English in the code, wrapped in `tr("...")`, with translations in
`tw_fundamental/locales/zh-TW.json`. Preset text in `themes.json` has an `en` and a `zh-TW` value.
`tests/test_i18n.py` fails if a wrapped string has no translation or a translation drops a placeholder.

## Command line

```bash
.venv/bin/python -m tw_fundamental 2330 --open      # TSMC, open in browser
.venv/bin/python -m tw_fundamental 6488 --years 8   # longer history
.venv/bin/python -m tw_fundamental 2317 --refresh   # bypass the 12-hour cache
```

This prints a summary to the terminal and writes `reports/<id>.html`, which includes:

- **Valuation tiles**: price, P/E, P/B and dividend yield, each with where it sits in its own history
- **Quick checks**: pass/fail heuristics (ROE ≥ 15%, OCF ≥ net income, debt ratio, …)
- **Charts**: monthly revenue (月營收) and YoY, margins, EPS, ROE/ROA, cash flow, P/E band, dividends
- **Tables**: the last 12 quarters of metrics, plus dividends and payout ratio by fiscal year

## Layout

| File | Role |
|---|---|
| `tw_fundamental/finmind.py` | API client with a JSON cache in `.cache/` |
| `tw_fundamental/statements.py` | Long-to-wide statement reshaping; de-cumulates YTD cash flow |
| `tw_fundamental/analysis.py` | Data loading, metrics, valuation percentiles, checks |
| `tw_fundamental/report.py` | HTML/Plotly report |
| `tw_fundamental/pipeline.py` | `build_report()` and `list_stocks()`, shared by the CLI and the web UI |
| `tw_fundamental/web.py` | Flask app: stock search, `/report/<id>`, `/themes`, `/theme` |
| `tw_fundamental/theme.py` | Theme analysis: profiles, headline filtering, rule-based SWOT, CLI |
| `tw_fundamental/llm.py` | Optional Claude layer: web research and per-stock SWOT |
| `tw_fundamental/theme_report.py` | Theme HTML report |
| `tw_fundamental/i18n.py`, `locales/zh-TW.json` | Language selection and the Traditional Chinese translations |
| `tw_fundamental/themes.json` | Theme presets: keywords, supply-chain segments and each stock's role |

## Data notes

- FinMind's income statement is single-quarter. Its **cash-flow statement is year-to-date cumulative**
  and is converted to single quarters here.
- Quarterly filings arrive 45 days or more after quarter end (Q4 by end of March). Monthly revenue
  comes out by the 10th of each month and is the most timely signal.
- Financial companies (金融保險) use a different statement layout. Leverage, liquidity and cash-flow
  checks are skipped for them.
- The checks are prompts for further reading, not investment advice.

## Tests

```bash
.venv/bin/python -m pytest
```
