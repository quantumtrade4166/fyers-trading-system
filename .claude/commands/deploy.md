# /deploy — Push code to GitHub and deploy to VPS

Deploys the latest local code to the VPS dashboard server. Run this after making any code changes.

## What it does
1. Commits and pushes local changes to GitHub
2. SSHes into VPS → git pull
3. Kills old uvicorn → restarts via Task Scheduler
4. Confirms server is responding (HTTP 200)

---

## Step 1 — Check for local changes

Run:
```powershell
cd G:\fyers_data_pipeline
git status
git diff --stat
```

If there are changes, ask the user for a one-line commit message (or use a sensible default based on what changed). Then:
```powershell
git add .
git commit -m "<message>"
git push
```

If nothing to commit, print "Nothing to push — already up to date." and proceed to Step 2 anyway (VPS may be behind).

---

## Step 2 — Pull on VPS

```powershell
ssh Administrator@144.79.166.103 "cd C:/Users/Administrator/Desktop/fyers_data_pipeline_git && git pull"
```

Print the git pull output so the user can see what changed on VPS.

---

## Step 3 — Restart server on VPS

```powershell
ssh Administrator@144.79.166.103 "C:\Users\Administrator\Desktop\restart_server.bat"
```

---

## Step 4 — Confirm server is up

Wait 8 seconds, then:
```powershell
Start-Sleep -Seconds 8
ssh Administrator@144.79.166.103 "powershell -Command `"(Invoke-WebRequest -Uri http://localhost:8000/api/signals -UseBasicParsing).StatusCode`""
```

- If `200`: print "Deployed successfully. Server is live on VPS."
- If anything else or error: print the error and tell user to manually run `restart_server.bat` on VPS Desktop.

---

## Notes
- `.env`, `access_token.txt`, `positions.json`, `trades.json`, `equity.json` are in `.gitignore` — never touched
- Cloudflare tunnel does NOT need restart — stays running independently
- `restart_server.bat` is on VPS Desktop — user can double-click it manually too
- SSH key must be set up for passwordless auth (already done as of Session 12)
