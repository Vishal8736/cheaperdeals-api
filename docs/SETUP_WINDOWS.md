# Setup on Windows

## A0. One click (easiest)

Double-click `run_local.bat` in the project folder, or in cmd:

```bat
cd /d "C:\Users\Lenovo\OneDrive\Desktop\companey\cheapedeal"
run_local.bat
```

First run creates a private Python environment, installs packages (1 to 3 minutes), creates a password for you, starts the server and opens the browser. The username and password are printed in the window. Next runs start in a few seconds. Press Ctrl+C in that window to stop.

Everything it creates (environment, database, password) is stored in `C:\Users\<you>\CheapeDeal-data`, not in OneDrive. To get a new password: `run_local.bat --reset-password`. It needs Python 3.11 or newer on PATH.

## A. Manual version of the same thing (5 minutes, no accounts, no Docker)

Uses a simulated store and supplier, SQLite,. Python 3.11+ required.

```powershell
cd cheapedeal
copy .env.example .env
python scripts\gen_secrets.py          # copy SECRET_KEY, INTERNAL_API_KEY, ADMIN_PASSWORD into .env
```

Edit `.env` for local use:

```
APP_ENV=development
PUBLIC_URL=http://localhost:8000
ENABLE_DEV_TOOLS=true
SUPPLIER_BACKEND=mock
STORE_BACKEND=mock
DATABASE_URL=sqlite:///./cheapedeal.db
```

Then:

```powershell
cd api
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 and sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

Try this loop:
1. **Products** > Add demo products.
2. **Orders** > Create test order. In rehearsal mode it completes with a "Rehearsal" badge and nothing is sent anywhere.
3. **Settings**: type any shipping method name, save, press **Go live** (still simulated), create more orders. Try "Supplier says 429" (watch it retry) and "Supplier reply lost" (the order stops and asks you).
4. **Orders** > Move supplier forward: tracking sync runs, orders become Shipped, then Delivered.
5. Set approval threshold to 20 in Settings and create a 3-item order: it waits on **Approvals**.

Run the tests: `pip install -r requirements-dev.txt` then `python -m pytest` from the project folder.

## B. Full stack with Docker Desktop

```powershell
copy .env.example .env      # fill secrets; for local use set N8N_PROTOCOL=http, N8N_HOST=localhost,
                            # N8N_WEBHOOK_URL=http://localhost:5678/, N8N_SECURE_COOKIE=false, PUBLIC_URL=http://localhost:8000
docker compose up -d --build
docker compose ps           # postgres, api, n8n should be running/healthy
```

- Dashboard: http://localhost:8000   (set `ENABLE_DEV_TOOLS=true` and the mock backends)
- n8n: http://localhost:5678 (create the owner account on first visit)

### Import and start the n8n workflows

Either in the n8n UI (Workflows > Import from file, choose the six files in `n8n/workflows/`), or:

```powershell
docker compose exec n8n n8n import:workflow --separate --input=/workflows
```

Then, in n8n:
1. Open **Cheape Deal 05 - Error handler** and switch it to Active.
2. For each of the other five: Settings (three dots) > **Error Workflow** = "Cheape Deal 05 - Error handler", then switch it Active.
3. Click **Execute workflow** once on each to see it succeed. The dashboard's "Automation heartbeat" turns green.

If a workflow fails with an authorization error, `CHEAPEDEAL_INTERNAL_KEY` is not reaching n8n: check that `INTERNAL_API_KEY` is in `.env` and recreate the container (`docker compose up -d`).

### Telegram alerts

1. In Telegram, talk to @BotFather, send `/newbot`, copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` into `TELEGRAM_CHAT_ID`.
3. Restart the API, then Settings > Send test.

## Updating to a newer version
1. Close the running window (or press Ctrl+C).
2. Extract the new zip over the same folder (`...\companey\cheapedeal`), choosing "Replace".
3. Double-click `run_local.bat`. Packages are reinstalled automatically and the database gets any new columns on its own. Your data lives in `C:\Users\<you>\CheapeDeal-data` and is not touched.
