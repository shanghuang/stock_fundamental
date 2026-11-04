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
.venv/bin/python -m tw_fundamental.theme CPO --open              # preset: CPO, AI-server, cooling, memory
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

Presets live in `tw_fundamental/themes.json`, which you can edit to add your own themes. A theme
name opens a preset when it matches the preset's key, its title, or a keyword that only that preset
has, so `DRAM`, `記憶體` and `HBM` all open the memory preset. Other names need stock codes or Claude
research. Without either, the page suggests companies whose name or industry matches the name (for
example `航運` finds the shipping companies) and lets you edit the list before running.

Each report includes a **supply-chain map**: segments from upstream to downstream, each company's
role, and per-segment medians (revenue growth, margins, P/E) so you can see which part of the chain
is growing or earning the most. Roles come from Claude's research when it is on, otherwise from the
preset (`segments` in `themes.json`, each mapping stock codes to a role). Stocks that neither covers are
grouped by FinMind industry. The SWOT also flags the highest and lowest gross margin and revenue
growth within a segment once it has at least 3 companies.

A **market outlook** covers the next six years, and **forward valuation** shows each stock's forward P/E:

- With `--llm`, Claude searches for published market-size forecasts, reconciles them into low, base
  and high paths in one unit, estimates growth for each supply-chain segment, and looks for analysts'
  EPS estimates (法人預估) for the next three years. The report lists the published forecasts and their sources.
- Always shown: a projection of the tracked companies' combined revenue (NT$ 億). Growth starts at the
  last 12 months' rate (capped at −30%…+60%) and fades to 5% a year by year six. This is a mechanical
  projection of those companies, not a market forecast; the report notes when one company (e.g. TSMC
  in CPO) dominates the total.
- Forward P/E = latest close ÷ forward EPS. Forward EPS is the analyst estimate when Claude found
  one, otherwise trailing EPS × (1 + last 3 months' revenue growth, limited to −50%…+100%), which
  assumes a constant net margin. Loss-makers and financial companies get none.
- **Price forecast**: implied share prices for the next three fiscal years.
  - EPS path: analysts' estimates where Claude found them. Otherwise year 1 is the forward EPS, and
    later years grow at the six-year outlook of the company's segment, or the theme's, kept within
    0–15% a year.
  - Implied price = EPS × the stock's own 5-year P/E band: the 25th percentile, median and 75th
    percentile give the low, base and high cases. The high case is capped at twice the median,
    because P/E spikes when earnings briefly collapse.
  - A stock trading below its usual P/E band is marked ⚠. The market may expect its earnings to
    fall, as at a cyclical peak, so its base case keeps today's P/E and its EPS gets no revenue
    momentum.
  - The report shows each year's EPS, the final year's low, base and high prices, and the base
    case's change from the latest close, in total and annualized. Prices exclude dividends. They
    are scenarios, not price targets.

**Saved analyses.** Every theme run (web or command line) is saved in `.cache/themes/`, and the newest
200 are kept. Repeating the same analysis (same theme, keywords, stocks, news window, Claude setting
and language) within 12 hours reopens the saved run instantly instead of fetching data or calling
Claude again; the page says so and links to **Run again with fresh data**. The Themes page shows the
latest runs, and **Saved analyses** (`/themes/saved`) lists them all with their growth outlook and
median forward P/E. On the command line, `python -m tw_fundamental.theme --list` prints them.

Rate limits: FinMind news is one request per stock per day scanned. Past days are cached for good,
but a first run of a 16-stock theme with 3 days of news uses about 180 of the roughly 300
requests per hour allowed without a token.

## Accounts

The web UI needs an account. Anyone who can reach the site can register at `/register`; the
**first account ever created becomes the admin**, so register yourself first after starting a new
installation. Only admins can run Claude research (it bills your Anthropic account). Saved theme
analyses are shared by everyone; each can be deleted by the person who ran it or by an admin.

- **Account** page (click your name): change your password. This signs out your other sessions.
- **Users** page (admins): make or remove admins, disable or enable accounts, reset a password (a
  temporary one is shown once), delete accounts. The last active admin cannot be removed.
- Accounts are stored in `data/users.db` (set `TWF_DATA_DIR` to move it), with salted scrypt password
  hashes. `data/secret_key` signs the session cookie; set `TWF_SECRET_KEY` instead if you prefer.
  Back up `data/`; it is not in git.
- **Topic requests**: users who are not admins can ask for a theme under **Request a topic** on the
  Themes page (up to 5 open requests each, with an optional note). Admins see a count next to
  **Requests** in the top bar. On that page they can run the topic (edit the name, keywords and
  stocks first if needed) or decline it with an optional reason. Any admin run of a requested topic
  counts, including one started from the Themes page: a request for `DRAM` is fulfilled when the
  memory preset runs. The request then leaves the user's list of open requests, and the requester
  gets a pop-up on their next page (or within a minute on an open page) with a link to the analysis.
  It stays until they open it or dismiss it.
- After 5 failed sign-ins for an account from one address, sign-in is blocked for 15 minutes.
- When serving over HTTPS, set `TWF_SECURE_COOKIES=1` so the session cookie is sent only over HTTPS.

From the command line:

```bash
.venv/bin/python -m tw_fundamental.users list
.venv/bin/python -m tw_fundamental.users create alice --admin   # asks for a password
.venv/bin/python -m tw_fundamental.users reset-password alice
.venv/bin/python -m tw_fundamental.users set-admin alice on
```

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
- **Price forecast**: implied prices for the next three fiscal years, worked out as in a theme analysis
  (EPS path × the stock's 5-year P/E band, ⚠ when it trades below its usual P/E). Outlook growth and
  any analysts' EPS estimates come from the newest theme analysis in the last 30 days that included
  the stock, preferring one with Claude research; the report links to it. Without one, EPS grows at
  5% a year after year 1.
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
| `tw_fundamental/llm.py` | Optional Claude layer: web research, market forecast and per-stock SWOT |
| `tw_fundamental/users.py` | Accounts: registration, sign-in, roles, admin actions, CLI |
| `tw_fundamental/wishlist.py` | Topic requests: submit, fulfill or decline, notifications |
| `tw_fundamental/theme_store.py` | Saved theme analyses: save, reuse, list, delete |
| `tw_fundamental/forecast.py` | Six-year market outlook (Claude or extrapolated), forward P/E, implied prices |
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
