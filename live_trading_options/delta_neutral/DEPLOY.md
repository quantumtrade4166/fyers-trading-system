# Delta Neutral — first deploy runbook

**Run this only when the market is CLOSED (after 15:30 IST).** The VWAP strangle
trades real money on the same box and its dashboard restarts as part of this.

The standard `/deploy` command **will not work for this one**, for three reasons
found on 2026-08-24:

1. `/deploy` step 1 runs `git add .`, and the local tree has `ETF data/`,
   `Nifty fno 2021-26/`, `forex/`, `.tmp.driveupload/` and more sitting untracked.
   That would sweep gigabytes into the commit. (Already handled — the work is
   committed with an explicit file list.)
2. The VPS is **19 commits behind** but its *files* already match `81c683a`
   (verified: all 12 modified code files byte-identical by git blob hash). A
   `git pull` onto that dirty tree is refused by git.
3. `live/audit.py` exists on the VPS as an **untracked** file. Even after a clean
   reset, `git pull` refuses to overwrite an untracked file with a tracked one.

After this deploy the VPS is on a normal committed HEAD and plain `/deploy` works
again.

---

## Pre-flight

```bash
ssh Administrator@144.79.166.103 "powershell -NoProfile -Command \"cd C:\Users\Administrator\Desktop\fyers_data_pipeline_git; \$d='live_trading_options\strangle_strategy\data\live_state'; Get-ChildItem \$d -Filter '*LIVE.json' | ForEach-Object { \$j=Get-Content \$_.FullName -Raw|ConvertFrom-Json; Write-Output (\$j.index+': open='+(\$j.open -ne \$null)+' mode='+\$j.mode) }\""
```

**Every index must read `open=False`.** If anything is still open, stop — the
VWAP position has not squared off yet.

---

## Step 1 — Back up the VPS working tree

Nothing below is destructive to anything unique (all 12 code files were verified
identical to `81c683a`), but take the backup anyway — it costs seconds.

```bash
ssh Administrator@144.79.166.103 "powershell -NoProfile -Command \"cd C:\Users\Administrator\Desktop\fyers_data_pipeline_git; \$b='C:\Users\Administrator\Desktop\predeploy_backup_'+(Get-Date -Format 'yyyyMMdd_HHmmss'); New-Item -ItemType Directory \$b | Out-Null; git status --short | ForEach-Object { \$p=\$_.Substring(3).Trim('\\\"'); if (Test-Path \$p -PathType Leaf) { \$dst=Join-Path \$b \$p; New-Item -ItemType Directory (Split-Path \$dst) -Force | Out-Null; Copy-Item \$p \$dst -Force } }; Write-Output ('backed up to ' + \$b)\""
```

---

## Step 2 — Clean ONLY the code paths

Leaves `deployment/dualmom_paper*.json` and `dualmom_signal_log.json` untouched —
those are live DualMom paper state, and the incoming commits never touch them
(verified with `git log --name-only 7e864e0..HEAD -- <those files>` → empty).

```bash
ssh Administrator@144.79.166.103 "powershell -NoProfile -Command \"cd C:\Users\Administrator\Desktop\fyers_data_pipeline_git; \$f=@('deployment/main.py','deployment/static/index.html','live_trading_options/strangle_strategy/config/parameters.json','live_trading_options/strangle_strategy/live/control_flags.py','live_trading_options/strangle_strategy/live/controller.py','live_trading_options/strangle_strategy/live_tick_engine.py','live_trading_options/strangle_strategy/reporting/sheets_logger.py','live_trading_options/strangle_strategy/live/kite_executor.py','live_trading_options/strangle_strategy/live/ledger.py','live_trading_options/strangle_strategy/live/trigger_engine.py','live_trading_options/strangle_strategy/live_capture.py'); git reset -q HEAD \$f 'live_trading_options/strangle_strategy/live/audit.py'; git checkout -- \$f; Remove-Item 'live_trading_options/strangle_strategy/live/audit.py' -Force; git status --short\""
```

`audit.py` is **deleted**, not reverted — it is untracked at the VPS's HEAD, and
the pull will restore it byte-identical. That is the step that makes the pull
possible at all.

Expected after this: only the DualMom state JSONs, logs, and untracked junk
remain listed. **No code file should still show as modified.**

---

## Step 3 — Pull

```bash
ssh Administrator@144.79.166.103 "powershell -NoProfile -Command \"cd C:\Users\Administrator\Desktop\fyers_data_pipeline_git; git pull; Write-Output ('HEAD: ' + (git log -1 --format='%h %s'))\""
```

HEAD must become the Delta Neutral commit. If the pull still refuses, **stop and
report the message** — do not force anything.

---

## Step 4 — Verify the code landed

```bash
ssh Administrator@144.79.166.103 "powershell -NoProfile -Command \"cd C:\Users\Administrator\Desktop\fyers_data_pipeline_git; Write-Output ('dn files: ' + (Get-ChildItem live_trading_options\delta_neutral -Recurse -Filter *.py | Measure-Object).Count); python live_trading_options\delta_neutral\core\test_windows.py | Select-Object -Last 1; python live_trading_options\delta_neutral\core\test_selector.py | Select-Object -Last 1; python live_trading_options\delta_neutral\live\dry_run.py 2>&1 | Select-Object -Last 1\""
```

Expect `31 passed`, `32 passed`, `158 passed` — the same suite, now on the VPS's
own Python. If the dry run fails there but passes locally, something differs in
the environment; investigate before going further.

---

## Step 5 — Create the scheduled task

```bash
ssh Administrator@144.79.166.103 "schtasks /Create /TN DeltaNeutralEngine /TR \"C:\Users\Administrator\Desktop\fyers_data_pipeline_git\live_trading_options\delta_neutral\run_dn_engine.bat\" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:20 /RL HIGHEST /F"
```

Then harden it the same way the other trading tasks were (Session 22): allow
start-when-missed, restart on failure, and **clear the "stop after N hours"
setting** — that setting once killed a live engine mid-session.

---

## Step 6 — Restart the dashboard

```bash
ssh Administrator@144.79.166.103 "C:\Users\Administrator\Desktop\restart_server.bat"
```

## Step 7 — Confirm

```bash
ssh Administrator@144.79.166.103 "powershell -NoProfile -Command \"Start-Sleep 8; (Invoke-WebRequest -Uri http://localhost:8000/api/signals -UseBasicParsing).StatusCode; (Invoke-WebRequest -Uri http://localhost:8000/api/dn/config -UseBasicParsing).Content\""
```

Both must answer: `200`, and the DN config JSON with the entry table. Then open
the dashboard and check the **⚖️ Delta Neutral** tab renders with
`STANDBY — not armed`.

---

## Next morning (Tue 25 Aug — NIFTY 0 DTE)

- 09:20 the engine starts itself. Check the tab shows `DTE 0 · stop 40 · → 30 at 12:00`.
- **Leave it unarmed for the first day** unless you intend to watch it. Unarmed it
  decides everything and places nothing, and the audit log records every decision
  plus a status line every 15 minutes.
- To go live: set qty (65 = 1 lot) and max loss, then **GO LIVE**. Arm *before*
  09:30 or the 09:30 entry is missed and it will instead open at the next
  15-minute window.
- Note the arm flag has **no expiry** — it stays armed until switched back.

## Rollback

```bash
ssh Administrator@144.79.166.103 "powershell -NoProfile -Command \"cd C:\Users\Administrator\Desktop\fyers_data_pipeline_git; git checkout 81c683a -- deployment live_trading_options/strangle_strategy\""
```

then `restart_server.bat`. The delta-neutral engine is a separate process and
task, so simply not starting it removes it entirely — the VWAP strangle never
depended on it.
