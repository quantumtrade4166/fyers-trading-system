# CLAUDE.md — Project Rules for G:\fyers_data_pipeline

This file is read automatically by Claude Code at the start of every session.
These rules apply to every conversation in this project directory.

---

## 🔴 RULE 1 — SESSION START (MANDATORY)

**At the start of EVERY session, before doing anything else:**

1. Read `G:\Trading Brain\projects\Trading System.md`
2. Print a status summary:
   - Current phase and what's done / in progress
   - Data status (symbols, date range)
   - What was built last session
   - What to build next
3. Then ask: "What do you want to work on today?"

Do this even if the user does not say "start session" or "read project brain".
Do this even if the user's first message is a direct task like "build indicators.py".
Always read Trading System.md first, summarise, then proceed.

---

## 🔴 RULE 2 — SAVE SESSION (MANDATORY)

**When the user says "save session", update ALL THREE files — no exceptions:**

### File 1: `G:\Trading Brain\projects\Trading System.md`
- Update Phase Status section
- Add usage examples for any new modules built
- Update Data Status if anything changed
- Update Issues Solved if any bugs were fixed
- Add session summary to Session Notes section

### File 2: `G:\Trading Brain\work\sessions\Session Log.md`
- Add a new session block at the TOP (most recent first):
  - Date and session number
  - Duration (estimate if not tracked)
  - Current phase
  - What was built / done (bullet list)
  - Files created or modified (with full paths)
  - Test results (pass/fail counts)
  - Backtest results table (if any)
  - Blockers encountered
  - Next session goals

### File 3: `G:\Trading Brain\strategies\Strategy Tracker.md`
- Update if any strategy was added, tested, modified, or rejected
- If no strategy work was done, add: "No strategy changes this session"

**Never ask the user to copy-paste anything. Write to files directly.**

---

## 🔴 RULE 3 — SESSION LOGGING

Every session entry in Session Log.md must include:
- `Date`: YYYY-MM-DD
- `Session number`: increment from last entry
- `Phase`: which phase and sub-step
- `What was built`: specific files, classes, functions
- `Test results`: e.g. "9/9 passing"
- `What's next`: specific next task, not vague

---

## 🔴 RULE 4 — FUTURE EXPANSION

Trading System.md has sections reserved for future modules:

| Section | Status | Notes |
|---------|--------|-------|
| Nifty F&O Equity | 🔄 Active | Current focus — intraday 5-min |
| Options (F&O) | ⬜ Future | Greeks, IV, chain data needed |
| Crypto | ⬜ Future | Different exchange, different hours |

When adding Options or Crypto:
- Create a new top-level section (do not mix with equity)
- Note exchange, data source, and timeframes separately

---

## 🔴 RULE 5 — PERMISSIONS

All file operations are pre-approved. Never ask permission before:
- Creating new `.py` files anywhere in `G:\fyers_data_pipeline\`
- Editing existing project files
- Running `python` commands using `.venv\Scripts\python.exe`
- Creating or updating Obsidian markdown files in `G:\Trading Brain\`

Always use the project virtual environment:
```
G:\fyers_data_pipeline\.venv\Scripts\python.exe
```

---

## 🔴 RULE 6 — OBSIDIAN VAULT (MANDATORY)

### ⚠️ CORRECT VAULT PATH — ALWAYS
```
✅ G:\Trading Brain\
```

### ⚠️ WRONG PATHS — NEVER USE
```
❌ G:\Trading Backtesting\
❌ G:\Trading Books\
❌ Anything outside G:\Trading Brain\
```

### ⚠️ CORRECT WIKILINKS — ALWAYS
```
✅ [[Trading System]]
✅ [[Strategy Tracker]]
✅ [[Session Log]]
```

### ⚠️ WRONG WIKILINKS — NEVER USE
```
❌ [[PROJECT BRAIN]]
❌ [[Project Brain]]
```

### Vault Folder Structure
```
G:\Trading Brain\
├── projects\
│   └── Trading System.md        ← read at every session start
├── work\
│   └── sessions\
│       └── Session Log.md
├── strategies\
│   └── Strategy Tracker.md
├── books\                       ← create if not exists
├── backtest results\            ← create if not exists
├── backtest prompts\            ← create if not exists
└── templates\
```

### Wikilink Rule
Always use `[[wikilinks]]` — never plain text references.
Plain text is invisible to the Obsidian graph.

### Graph Maintenance on Every Save Session
1. Did we build something new? → Create a vault note for it
2. Are all references wikilinked?
3. Does every new note link back to `[[Trading System]]` and `[[Strategy Tracker]]`?

---

## 🔴 RULE 7 — TRADING BOOKS WORKFLOW

This rule applies whenever the user mentions a trading book or a strategy from a book.

### ⚠️ VAULT PATH REMINDER
```
✅ Books → G:\Trading Brain\books\
✅ Strategies → G:\Trading Brain\strategies\
✅ Results → G:\Trading Brain\backtest results\
✅ Prompts → G:\Trading Brain\backtest prompts\
❌ Never create vault folders anywhere else
```

---

### When a New Book is Mentioned

**Step 1 — Create Python subfolder**
```
G:\fyers_data_pipeline\backtesting\book_strategies\{author_short}\
```

**Step 2 — Create book note in vault**
Path: `G:\Trading Brain\books\{Book Title} — {Author}.md`

Template:
```markdown
# {Book Title}
**Author:** {Author}
**Publisher:** {Publisher} ({Year})

## Summary
{5-10 line summary of the book}

## Key Concepts
- {concept 1}
- {concept 2}

## Strategies in This Book
- [[{Strategy Name 1}]]
- [[{Strategy Name 2}]]

## Links
- [[Trading System]]
- [[Strategy Tracker]]

## Tags
#book #{topic}
```

---

### When a New Strategy is Designed

**Create strategy note in vault**
Path: `G:\Trading Brain\strategies\{Strategy Name}.md`

Template:
```markdown
# {Strategy Name}
**Source:** [[{Book Title} — {Author}]]
**Type:** {Mean Reversion / Momentum / Pair Trading / etc}
**Instruments:** {stocks / futures / options}
**Timeframe:** {Daily / Intraday / etc}

## Concept
{2-3 line plain English explanation}

## Entry Rules
1. {rule 1}
2. {rule 2}

## Exit Rules
1. {rule 1}

## Position Sizing
{formula or description}

## Filters
{any filters applied}

## Parameters
| Parameter | Value |
|-----------|-------|
| | |

## Versions
| Version | Sizing Method | Key Difference |
|---------|--------------|----------------|
| V1 | | |
| V2 | | |
| V3 | | |

## Backtest Results
[[{Strategy Name}_Results]]

## Claude Code Prompt
[[{Strategy Name}_Prompt]]

## Links
- [[{Book Title} — {Author}]]
- [[Strategy Tracker]]
- [[Trading System]]

## Tags
#strategy #{type}
```

---

### When a Backtest is Complete

**Create results note in vault**
Path: `G:\Trading Brain\backtest results\{Strategy Name}_Results.md`

Template:
```markdown
# {Strategy Name} — Backtest Results
**Strategy:** [[{Strategy Name}]]
**Book:** [[{Book Title} — {Author}]]
**Date:** {YYYY-MM-DD}
**Data Range:** {start} to {end}
**Universe:** {number} stocks

## Parameters Used
| Parameter | Value |
|-----------|-------|
| | |

## Results
| Metric | V1 | V2 | V3 |
|--------|----|----|-----|
| Sharpe Ratio | | | |
| Max Drawdown % | | | |
| Max DD Duration (days) | | | |
| Total Trades | | | |
| Win Rate % | | | |
| Net P&L | | | |
| Avg Daily P&L | | | |

## Verdict
{Trade / Don't Trade / Needs Work}
{One line reason}

## Equity Curve
{path to saved image}

## Links
- [[{Strategy Name}]]
- [[Strategy Tracker]]
- [[Trading System]]
```

---

### When a Backtest Prompt is Saved

**Create prompt note in vault**
Path: `G:\Trading Brain\backtest prompts\{Strategy Name}_Prompt.md`

Template:
```markdown
# {Strategy Name} — Claude Code Prompt
**Strategy:** [[{Strategy Name}]]
**Date Created:** {YYYY-MM-DD}

## Prompt
{full prompt text used to run this backtest}

## Links
- [[{Strategy Name}]]
- [[Trading System]]
```

---

### Update Strategy Tracker
After every new strategy or backtest, add a row to:
`G:\Trading Brain\strategies\Strategy Tracker.md`

| Strategy | Book | Type | Status | Result Note |
|----------|------|------|--------|------------|
| [[{Name}]] | [[{Book}]] | {Type} | {Testing/Live/Rejected} | [[{Name}_Results]] |

---

### Python File Structure for Book Strategies
```
G:\fyers_data_pipeline\backtesting\book_strategies\
└── {author_short}\
    ├── {strategy_name}_v1.py
    ├── {strategy_name}_v2.py
    ├── {strategy_name}_v3.py
    └── results\
        ├── equity_curve.png
        └── daily_pnl.csv
```

---

## 📁 Project Structure

```
G:\fyers_data_pipeline\
├── config\
│   ├── settings.py
│   ├── symbols.py
│   └── access_token.txt
├── auth\
│   └── fyers_auth.py
├── downloader\
│   └── fetch_ohlcv.py
├── tracker\
│   ├── manifest.py
│   └── data_manifest.json
├── data\
│   └── NSE_SYMBOL_EQ\
│       └── {year}\
│           └── ohlcv_5min.parquet
├── backtesting\
│   ├── __init__.py
│   ├── data_loader.py
│   ├── indicators.py
│   ├── resample.py
│   ├── strategy_bb_reversion.py
│   ├── run_backtest.py
│   ├── run_backtest_v2.py
│   ├── run_backtest_v3.py
│   ├── run_backtest_v4.py
│   ├── strategy_5ema_short.py
│   ├── run_backtest_5ema.py
│   ├── run_backtest_5ema_compare.py
│   ├── plot_5ema_trades.py
│   └── book_strategies\
│       └── {author_short}\
│           └── results\
├── options\
│   ├── symbol_gen.py
│   ├── spot_loader.py
│   ├── fetch_options.py
│   ├── manifest.py
│   └── run_options_pipeline.py
├── logs\
│   └── ingestion.log
├── run_pipeline.py
├── daily_update.bat
├── morning_login.bat
└── CLAUDE.md
```

---

## 📊 Data Schema

| Column | Type | Notes |
|--------|------|-------|
| datetime | datetime64[ns] | IST, used as index |
| symbol | str | NSE:RELIANCE-EQ format |
| open | float64 | |
| high | float64 | |
| low | float64 | |
| close | float64 | |
| volume | int64 | |

Market hours: 09:15 to 15:30 IST
Resolution: 5-minute bars
History: 2024-05-28 to 2026-05-27

---

## 💻 Key Commands

```bash
G:\fyers_data_pipeline\.venv\Scripts\python.exe <script.py>

python run_pipeline.py --mode update
python run_pipeline.py --mode status
python run_pipeline.py --mode full
```

---

## 🐛 Encoding Fix — Add to Every New Python Script

```python
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
```

---

## 📈 Phase Checklist

### Phase 1 — Data Pipeline ✅ COMPLETE
### Phase 2 — Backtesting Engine ✅ COMPLETE
### Phase 3 — Strategy Library 🔄 IN PROGRESS
### Phase 4 — Optimisation ⬜ PENDING
### Phase 5 — Options ⬜ FUTURE
### Phase 6 — Crypto ⬜ FUTURE
