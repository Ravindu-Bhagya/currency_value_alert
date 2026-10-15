# Binance Price Alert

Track cryptocurrency prices and receive email alerts.

A personal price-alert app. You create alerts in a web page (for example "XRP/USDT **above** 2.50").
A scheduled job checks Binance prices about every 5 minutes and sends you an email
when a target is reached. Every component runs on a free tier, and none of them asks for a credit card.

> **This is not a real-time monitor.** Prices are checked about every 5 minutes, and GitHub may
> start scheduled runs late. If the price touches your target and moves away between two checks,
> the alert does **not** fire. See [The checking interval](#the-checking-interval).

The app only reads public market data. It never trades and never asks for a Binance API key.

---

## Contents

1. [Architecture](#architecture)
2. [Prerequisites](#prerequisites)
3. [Setup, step by step](#setup-step-by-step)
4. [Using the app](#using-the-app)
5. [Running locally](#running-locally)
6. [Running the tests](#running-the-tests)
7. [Alert states and delivery guarantees](#alert-states-and-delivery-guarantees)
8. [Troubleshooting](#troubleshooting)
9. [Free-tier limits and costs](#free-tier-limits-and-costs)
10. [Security notes](#security-notes)
11. [Project structure](#project-structure)

---

## Architecture

```
 ┌─────────────────────────────┐   HTTPS + your access key    ┌──────────────────────────────┐
 │ Website (GitHub Pages)      │ ───────────────────────────▶ │ Alert API (Cloudflare Worker) │
 │ frontend/ : HTML, CSS, JS   │   create/list/edit/cancel/   │ worker/src/index.js           │
 │                             │   delete alerts              │        │                      │
 │  └─ live prices & symbols ──┼──▶ Binance public API        │        ▼                      │
 └─────────────────────────────┘                              │ Cloudflare D1 (SQLite) table  │
                                                              └──────────────▲───────────────┘
                                                                             │ CHECKER_TOKEN
 ┌──────────────────────────────────────────┐                                │ (/internal/*)
 │ GitHub Actions, every 5 min (Python)     │ ───────────────────────────────┘
 │ backend/check_prices.py                  │ ──▶ Binance public API  (latest prices)
 │                                          │ ──▶ Your mail server (SMTP)          
 └──────────────────────────────────────────┘
```

**Why a small backend is needed.** GitHub Pages only serves static files, so it can't save data.
Putting a GitHub token in browser JavaScript would let anyone who views the page take over your repository.
That's why alerts live in a tiny **Cloudflare Worker + D1 database**, both on Cloudflare's free plan with no credit card:

| Concern | How it is handled |
|---|---|
| Creating alerts from the website | The browser calls the Worker with an **access key** (`ADMIN_TOKEN`). You type it into the app once. It isn't in the source code and is kept only in your browser. |
| Scheduled worker reads/updates alerts | GitHub Actions calls the Worker's `/internal/*` endpoints with a **separate** `CHECKER_TOKEN`. This token cannot create or delete alerts, and the admin key cannot call the checker endpoints. |
| Email (SMTP) credentials | Exist only as GitHub Actions secrets and are used only by the Python job. They never reach the browser or the Worker. |
| Concurrency / duplicates | Every state change is one atomic conditional SQL update (`UPDATE … WHERE status='active' AND version=? RETURNING *`). Only one run can claim a trigger. The workflow also uses a `concurrency` group so runs never overlap. |

Alternatives that were considered and rejected:
- **Alerts as JSON committed to the repository.** The browser cannot commit without an exposed token.
- **GitHub Issues as the write channel.** Every alert would need a manual "submit issue" click, and on a free plan the alerts would be public.
- **localStorage only.** GitHub Actions cannot read your browser's storage.

---

## Prerequisites

- A **GitHub** account (free).
- A **Cloudflare** account (free, no credit card): <https://dash.cloudflare.com/sign-up>
- An **email account** that allows SMTP sending (a free Gmail account works).
- On your computer, used once for setup:
  - **Git**
  - **Node.js 20 or newer** (for the `wrangler` deploy tool; `node --version`). The Worker tests need Node 22.13+.
  - **Python 3.10 or newer** (`python3 --version`). Optional: only for local runs and tests.

---

## Setup, step by step

Throughout this guide, replace:
- `YOUR-USER` with your GitHub username
- `binance-price-alert` with your repository name, if you chose a different one

### Step 1: Create the GitHub repository

1. Go to <https://github.com/new>.
2. Repository name: `binance-price-alert`. Visibility: **Public**. (On the free plan, GitHub Pages
   needs a public repository. Your alerts are **not** stored in the repo, so they stay private.)
3. Do **not** add a README or .gitignore (this project already has them). Click **Create repository**.
4. Push this project from your computer:

```bash
cd /path/to/BinanceAlert
git init
git add .
git commit -m "Initial commit: Binance Price Alert"
git branch -M main
git remote add origin https://github.com/YOUR-USER/binance-price-alert.git
git push -u origin main
```

Before committing, run `git status` and confirm that no `.env` or `.dev.vars` file is listed.

### Step 2: Set up email delivery (SMTP)

Alerts are sent as emails over SMTP. A free Gmail account works:

1. Turn on **2-Step Verification** for the Google account: <https://myaccount.google.com/security>.
2. Create an **App Password** at <https://myaccount.google.com/apppasswords> (name it e.g. `Price Alert`).
   Google shows a 16-character password once. This is your `SMTP_PASSWORD`; treat it like a password and never commit it.
   Your normal Gmail password will **not** work.
3. Note these values:

   | Value | Gmail | Other providers |
   |---|---|---|
   | `SMTP_HOST` | `smtp.gmail.com` | Your provider's SMTP server (e.g. `smtp.office365.com`) |
   | `SMTP_PORT` | `587` | `587` (STARTTLS) or `465` (TLS) |
   | `SMTP_USERNAME` | Your Gmail address | Your mailbox login |
   | `SMTP_PASSWORD` | The App Password | Your SMTP password |
   | `EMAIL_TO` | Where alerts go; several addresses separated by commas | same |
   | `EMAIL_FROM` | Optional; defaults to `SMTP_USERNAME` | Must be an address the server lets you send as |

### Step 3: Generate two access keys

Generate two long random values. They protect your alert storage:

```bash
openssl rand -hex 32    # → ADMIN_TOKEN   (you will type this into the website)
openssl rand -hex 32    # → CHECKER_TOKEN (used only by GitHub Actions)
```

(Without `openssl`: `python3 -c "import secrets; print(secrets.token_hex(32))"`.)
Keep them in a password manager. Use different values for the two keys.

### Step 4: Deploy the alert storage API (Cloudflare Worker + D1)

All commands run from the `worker/` folder. `npx` downloads the official `wrangler` tool on first use.

```bash
cd /path/to/BinanceAlert/worker

# 4.1 Log in to Cloudflare (opens your browser)
npx wrangler@4 login

# 4.2 Create the database
npx wrangler@4 d1 create binance-alerts
```

The output includes a `database_id = "xxxxxxxx-xxxx-..."` line. Open `worker/wrangler.toml` and:
- replace `REPLACE-WITH-YOUR-D1-DATABASE-ID` with that id;
- set `ALLOWED_ORIGINS` to your GitHub Pages **origin**, which is `https://YOUR-USER.github.io`
  (scheme and host only, **no** `/binance-price-alert` path, no trailing slash).

The database id and origin are not secrets, so it's fine to commit them.

```bash
# 4.3 Create the table
npx wrangler@4 d1 execute binance-alerts --remote --file=schema.sql

# 4.4 Store the two keys as encrypted Worker secrets (paste each value when prompted)
npx wrangler@4 secret put ADMIN_TOKEN
npx wrangler@4 secret put CHECKER_TOKEN

# 4.5 Deploy
npx wrangler@4 deploy
```

`deploy` prints your Worker URL, e.g. `https://binance-price-alert.YOUR-SUBDOMAIN.workers.dev`.
(The first time, Cloudflare may ask you to choose a free `workers.dev` subdomain.)

Check that it works:

```bash
curl https://binance-price-alert.YOUR-SUBDOMAIN.workers.dev/api/health
# {"ok":true,"time":"..."}
```

### Step 5: Point the website at your API

Edit `frontend/config.js` and set `apiBaseUrl` to the Worker URL from Step 4.5 (no trailing slash).
This URL is not a secret. Then commit:

```bash
cd /path/to/BinanceAlert
git add worker/wrangler.toml frontend/config.js
git commit -m "Configure Worker and API URL"
git push
```

### Step 6: Add the GitHub Actions secrets

In your repository on GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
Create these secrets:

| Name | Value |
|---|---|
| `ALERTS_API_URL` | Your Worker URL, e.g. `https://binance-price-alert.YOUR-SUBDOMAIN.workers.dev` |
| `CHECKER_TOKEN` | The CHECKER_TOKEN from Step 3 (same value as the Worker secret) |
| `SMTP_HOST` | From Step 2, e.g. `smtp.gmail.com` |
| `SMTP_PORT` | From Step 2, e.g. `587` |
| `SMTP_USERNAME` | From Step 2 |
| `SMTP_PASSWORD` | From Step 2 (Gmail: the App Password) |
| `EMAIL_TO` | Recipient address(es) |
| `EMAIL_FROM` | Optional |

`ADMIN_TOKEN` is **not** needed in GitHub.

### Step 7: Enable GitHub Actions and test email

1. Open the **Actions** tab of your repository. If GitHub asks, click **"I understand my workflows, go ahead and enable them"**.
2. Select **Price alert checker → Run workflow**, choose mode **`test-email`**, and click **Run workflow**.
3. Within about a minute you should receive "✅ Binance Price Alert, Test message…" by email (check the spam folder the first time).
   If the run fails, open it and read the log (see [Troubleshooting](#troubleshooting)).

After this, the checker runs automatically every 5 minutes. You can also run it any time with mode `check`.

### Step 8: Deploy the website to GitHub Pages

1. Repository **Settings → Pages**. Under **Build and deployment → Source**, choose **GitHub Actions**.
2. Go to **Actions → Deploy website to GitHub Pages → Run workflow** (it also runs automatically
   whenever `frontend/` changes on `main`).
3. When it finishes, your site is at `https://YOUR-USER.github.io/binance-price-alert/`.

### Step 9: Connect the website

1. Open the site and click **Connect** (top right).
2. Paste your **ADMIN_TOKEN**. Tick **Remember on this device** only on a personal device.
3. The indicator turns green ("Connected") and your alert list loads.

---

## Using the app

**Create an alert**
1. In **Trading pair**, type a coin or symbol (`xrp`, `bitcoin`, `ETH/USDT`) and pick a pair from the list.
   The list holds every pair Binance currently marks as actively trading on Spot.
2. The **Current price** panel shows the live Binance price, which host it came from, and when it was updated.
3. Enter a **Target price**. Use a dot for decimals, up to 8 decimal places.
4. Choose **Above** (fires when price ≥ target) or **Below** (fires when price ≤ target).
   If the current price already satisfies the condition, the form warns you that the alert will fire on the next check.
5. Click **Create alert**. You'll see "Alert saved". Identical active alerts are rejected.

**Manage alerts.** Each alert card shows the pair, target, condition, last checked or trigger price, status,
created and triggered times, and notification status.
- **Edit** (active alerts only): change the target or condition.
- **Cancel alert**: stops checking it and keeps it in the list as *Cancelled*.
- **Delete**: removes it permanently, after you confirm.
- **Retry notification**: shown when email delivery failed and the retries ran out.

**Receiving notifications.** When an alert fires, you get:

```
🔔 Binance Price Alert

Pair: XRP/USDT
Target Price: 2.50 USDT
Condition: Above
Current Price: 2.503 USDT
Status: TARGET REACHED

Your target price has been reached!
```

---

## Running locally

### Run the checker against your deployed API

```bash
cd /path/to/BinanceAlert
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit .env and fill in the values (.env is git-ignored)
set -a; source .env; set +a

.venv/bin/python -m backend.check_prices --dry-run         # reads alerts and prices; writes nothing, sends nothing
.venv/bin/python -m backend.check_prices --test-email      # sends one test email
.venv/bin/python -m backend.check_prices                   # a real run (same as GitHub Actions)
```

Exit codes: `0` ok, `1` configuration error, `2` alert storage unreachable/rejected,
`3` Binance unavailable, `4` an email delivery failed.

### Run everything locally (no Cloudflare deploy)

```bash
# Terminal 1: local Worker with a local D1 database
cd worker
printf 'ADMIN_TOKEN=local-admin-token-123456\nCHECKER_TOKEN=local-checker-token-123456\nALLOWED_ORIGINS=http://localhost:8000\n' > .dev.vars   # git-ignored
npx wrangler@4 d1 execute binance-alerts --local --file=schema.sql
npx wrangler@4 dev --port 8787

# Terminal 2: the website. On localhost it uses http://127.0.0.1:8787 automatically
# (another Worker port: open http://localhost:8000/?api=http://127.0.0.1:PORT)
cd frontend && python3 -m http.server 8000
# open http://localhost:8000 and connect with local-admin-token-123456

# Terminal 3: the checker
ALERTS_API_URL=http://127.0.0.1:8787 CHECKER_TOKEN=local-checker-token-123456 \
  .venv/bin/python -m backend.check_prices --dry-run
```

No tracked files need editing: `.dev.vars` overrides `ALLOWED_ORIGINS` for `wrangler dev` only.

---

## Running the tests

The tests use mocks only. They never send emails, never call Binance, and never touch your real data.

```bash
# Python (alert logic, Binance client, email notifier, storage client, concurrency)
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q

# Worker API (real SQL against an in-memory SQLite; needs Node.js 22.13+; no npm install)
node --test tests/worker.test.mjs
```

The **Tests** workflow runs both suites on every push.

What is covered: Above and Below triggers, prices exactly equal to the target, conditions not met, invalid prices
and symbols, cancelled alerts ignored, no duplicate notification for triggered alerts, Binance errors
(5xx, 429 Retry-After, 451 host fallback, 418 ban), SMTP errors (retryable vs permanent, password
redaction), failed-notification retries and the attempt limit, persistent create/list/edit/cancel/delete,
auth separation, CORS, and concurrent runs (atomic claim, stale versions, expired leases).

---

## Alert states and delivery guarantees

| Status shown | Meaning |
|---|---|
| **Active** | Checked on every run. |
| **Triggered** | The condition was met; the trigger price and time are recorded. Never checked or re-notified again. |
| **Notification Failed** | Triggered, but email delivery failed (the error is shown). |
| **Cancelled** | You stopped it. Never checked. |

Notification status: *Waiting for trigger → Sending → Delivered*, or *Failed (attempt n/5), will retry*.

**What happens when the price is reached but email fails:**
1. The alert is still marked **Triggered**, with the price and time recorded. The trigger itself is never lost.
2. The notification is marked **failed** with the error message, and the run exits with code 4 (red in Actions).
3. Each following run retries it (using the original trigger price), up to `MAX_NOTIFICATION_ATTEMPTS` (5) in total.
   Within a single run, network errors, 5xx responses and temporary (4xx) SMTP errors are also retried with backoff.
4. After 5 failed attempts it stays **Notification Failed** until you fix the cause and press **Retry notification**.

**Duplicate protection.** Only the run that atomically claims an alert sends its message, and a
delivered notification is never claimed again. One edge case remains: if a run dies *after*
the mail server accepted the message but *before* "delivered" was saved, the 10-minute lease expires and a
later run re-sends it. So delivery is **at-least-once**. A rare duplicate is possible; a silently lost alert is not.

---

## The checking interval

- **Primary timer: a Cloudflare Cron Trigger** (`[triggers]` in `worker/wrangler.toml`, every 2 minutes) starts the
  checker workflow through GitHub's API whenever there are active alerts. It needs the Worker secret
  `GITHUB_DISPATCH_TOKEN`: a fine-grained token for this repository with **Actions: Read and write** only
  (`npx wrangler@4 secret put GITHUB_DISPATCH_TOKEN`). Without it the cron does nothing.
- **Backup timer:** the workflow's own `*/5 * * * *` schedule. GitHub doesn't guarantee it: runs are often 5–15 minutes
  late, and new repositories can go hours without a scheduled run.
- Only the price **at the moment of each check** is compared. A spike that crosses your target and comes back
  between two checks is **missed**. This is a convenience tool, not a trading or risk-management system.
- You can change the interval in `.github/workflows/price-alert.yml`. Keep the repository **public**:
  at 5 minutes a private repository would use far more than the free Actions minutes (below).
- **GitHub disables scheduled workflows in public repositories after 60 days without repository activity.**
  GitHub emails you first. To keep it running, re-enable the workflow in the Actions tab, or push any commit.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Website banner "Setup needed" | `apiBaseUrl` in `frontend/config.js` is still the placeholder (Step 5). |
| "Could not reach the storage API" | Check that the URL is correct and that `ALLOWED_ORIGINS` in `wrangler.toml` is exactly `https://YOUR-USER.github.io` (no path). Run `npx wrangler@4 deploy` again after changing it. |
| "Invalid or missing access key" | The key typed into the site must equal the Worker's `ADMIN_TOKEN`. To reset it: `npx wrangler@4 secret put ADMIN_TOKEN`. |
| "Server is not configured: missing access token secret" | A Worker secret is missing or shorter than 16 characters; run Step 4.4 again. |
| "Could not verify the symbol with Binance" | Binance was briefly unreachable from Cloudflare; try again. |
| "Could not load trading pairs from Binance" | Your network or region blocks Binance; click **Try again**. Both `data-api.binance.vision` and `api.binance.com` are tried. |
| Actions log: `Alert API rejected the CHECKER_TOKEN` | The GitHub secret `CHECKER_TOKEN` doesn't match the Worker secret. |
| Actions log: `Environment variable … is not set` | A GitHub secret is missing or misspelled (Step 6). |
| Email error: `SMTP login failed (535)` | Wrong `SMTP_USERNAME` / `SMTP_PASSWORD`. Gmail needs an App Password, not your normal password. |
| Email error: `The mail server refused EMAIL_TO` | Check the `EMAIL_TO` address. |
| Test email never arrives | Look in the spam folder and mark it "Not spam". |
| Actions log: `Binance API unavailable (… HTTP 451 …)` | Both Binance hosts refused the runner's region. Re-run later; if it persists, set a `BINANCE_BASE_URLS` env value in the workflow. |
| Scheduled runs stopped | See the 60-day note above. Also check **Actions → Price alert checker** isn't disabled. |
| Alert shows "… is not currently listed on Binance Spot" | The pair was delisted or suspended. Cancel or delete the alert. |

---

## Free-tier limits and costs

| Service | Free allowance (at the time of writing; check the providers' pages) | This app's usage |
|---|---|---|
| GitHub Actions | Unlimited minutes for **public** repos; 2,000 min/month for private repos (Free plan) | ~288 runs/day, under 1 minute each. Private repos would use about 8,640 billed minutes/month (over the free allowance). |
| GitHub Pages | Free for public repos (soft limit 100 GB bandwidth/month) | Tiny |
| Cloudflare Workers (Free) | 100,000 requests/day | A few hundred per day |
| Cloudflare D1 (Free) | 5 GB storage, 5M row reads/day, 100k row writes/day | Kilobytes; hundreds of rows touched per day |
| Binance public API | Request-weight limit (6,000/min per IP) | 1 price request (weight 4) per run |
| Gmail SMTP | Free; about 500 emails/day for a personal account | One email per triggered alert |

**What could ever cost money:** nothing, at personal-use scale. You'd only need to pay if you ran the repository
as **private** with a much shorter interval (exceeding 2,000 Actions minutes/month), or if you went far past
Cloudflare's daily request limits. In that case Cloudflare's free plan rejects excess requests rather than
charging you. The app caps itself at 200 active alerts.

---

## Security notes

- **No secrets in the frontend.** `config.js` contains only the public Worker URL. You enter the access key at
  runtime, and it's stored in `sessionStorage` (or `localStorage` if you tick *Remember*) in your browser only.
- **Two least-privilege keys.** `ADMIN_TOKEN` (UI) and `CHECKER_TOKEN` (Actions) are checked in constant time.
  Neither can use the other's endpoints.
- **SMTP credentials** live only in GitHub Actions secrets. They're redacted from error messages and logs, and
  `urllib3` debug logging (which would print request URLs) is suppressed.
- **Validation everywhere:** symbols (`^[A-Z0-9]{2,20}$` plus a live Binance check), prices (positive, at most 8 decimals,
  stored as exact decimal strings), conditions, ids (UUID), body size (64 KB) and alert count (200 active).
- **CORS** is limited to your Pages origin. The page sets a strict Content-Security-Policy (no inline scripts) and
  renders all data with `textContent`, so there's no HTML injection.
- **Workflows** use `permissions: contents: read` (Pages: only what deployment requires). The checker never writes to the repository.
- **Limitations, stated plainly:**
  - Anyone holding `ADMIN_TOKEN` can manage your alerts, so keep it secret and rotate it with `wrangler secret put` if it leaks.
  - Anyone can *see* the website itself (it's public), but they can't see or change alerts without the key.
  - The Worker doesn't rate-limit failed logins; a 64-hex-character key makes guessing impractical.
  - If you tick *Remember*, the key stays in that browser's storage. Don't tick it on shared computers.

---

## Project structure

```
BinanceAlert/
├── frontend/                 # Static website (GitHub Pages)
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   └── config.js             # public settings: Worker URL
├── worker/                   # Alert storage API (Cloudflare Worker + D1)
│   ├── src/index.js
│   ├── schema.sql
│   ├── wrangler.toml
│   └── package.json
├── backend/                  # Scheduled checker (Python, run by GitHub Actions)
│   ├── check_prices.py       # CLI entry point
│   ├── alert_service.py      # evaluation + delivery logic
│   ├── alert_repository.py   # client for the Worker's /internal API
│   ├── binance_client.py
│   ├── email_notifier.py
│   └── models.py
├── tests/
│   ├── conftest.py
│   ├── test_alert_logic.py
│   ├── test_alert_repository.py
│   ├── test_binance_client.py
│   ├── test_email_notifier.py
│   └── worker.test.mjs
├── .github/workflows/
│   ├── price-alert.yml       # every 5 min + manual
│   ├── pages.yml             # deploys frontend/ to GitHub Pages
│   └── tests.yml
├── requirements.txt          # runtime: requests
├── requirements-dev.txt      # + pytest
├── pytest.ini
├── .env.example
├── .gitignore
└── README.md
```
