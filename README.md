# Wizz AYCF availability watcher

Logs into [multipass.wizzair.com](https://multipass.wizzair.com) with your **All You Can Fly**
account, checks whether a bookable flight card (one with a **SELECT** button) exists for a
given route + date, and pings you on **Telegram** when it does. It only detects and notifies —
you book manually.

It respects the AYCF booking window: a route is only checked when "now" is between
**72h and 3h before departure** (configurable). Outside that window the route is skipped, and
Wizz's own date picker only enables in-window dates anyway.

## Secrets & config

- **Secrets** (Wizz login, Telegram bot) come from environment variables:
  `WIZZ_EMAIL`, `WIZZ_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
  - Locally: put them in a `.env` file (git-ignored — see `.env.example`).
  - In the cloud: store them as **GitHub Actions Secrets**.
- **Routes & settings** live in `config.json` (committed, safe to edit):
  the list of routes, plus `booking_window`, `headless`, `notify_once`.

`config.json` carries no secrets, so it is committed; `.env` and `state.json` are git-ignored.

## Run in the cloud (recommended) — GitHub Actions

Runs every ~5 minutes on GitHub's servers, independent of your Mac.
`.github/workflows/aycf.yml` does it; a stdlib-only pre-flight gate
(`aycf_window.py`) means off-window runs finish in seconds without installing a browser.

One-time setup:
1. Create a new GitHub repo (Public = unlimited free Actions minutes; the code has no secrets).
2. Push this folder to it:
   ```bash
   cd ~/Claude/wizz-aycf-watch
   git remote add origin https://github.com/<YOUR_USERNAME>/wizz-aycf-watch.git
   git push -u origin main
   ```
3. Repo → **Settings → Secrets and variables → Actions → New repository secret**, add four
   (values are in your local `.env`): `WIZZ_EMAIL`, `WIZZ_PASSWORD`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`.
4. Repo → **Actions** tab → enable workflows. Use **Run workflow** to trigger a test run.

Notify-once state is persisted between runs via the Actions cache, so you get one alert per
route until it stops appearing — no every-5-minute spam.

Each availability alert carries an **"✅ I've booked this — stop alerts"** button. Tapping it
flags the route as booked in `state.json` so it's no longer checked (handy when a flight you
already grabbed disappears and later reopens). Because the bot only wakes every ~5 min, the tap
is applied on the next scheduled run, which then edits the message to confirm. To re-enable a
route later, clear its `booked` flag from `state.json` (or just re-add it).

## Run locally

Python 3.12 + Playwright (Node is not required):

```bash
cd ~/Claude/wizz-aycf-watch
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env && $EDITOR .env     # fill in your four secrets

python3 check_aycf.py            # check all in-window routes, headless
python3 check_aycf.py --debug    # also dump screenshots/HTML to debug/ at each step
python3 check_aycf.py --no-window   # ignore the 72h-3h gate (force a check now)
```

For a local cron instead of the cloud, `install-cron.sh` adds an every-5-min entry — but that
needs the Mac awake during windows, which is why GitHub Actions is preferred.

### Getting your Telegram bot token + chat_id
1. Message **@BotFather**, `/newbot`, follow prompts → **bot token** (`123456:ABC...`).
2. Message your new bot once (so it can message you back).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[].message.chat.id`.

## How the booking window is computed

AYCF flights on a date depart at various times, each bookable 72h–3h before *its* departure.
Since we only have the date, the script uses the widest safe window: opens 72h before **00:00**
local and closes 3h before **23:59** local on the date, per-route timezone (`Europe/Budapest`,
`Asia/Jerusalem`). So it never skips a check while something that day could still be bookable.

## If Wizz changes the page

Selectors in `check_aycf.py` (`SELECTORS` dict) were verified against the live site. If runs
start logging "could not select…", run with `--debug` (saves screenshots + HTML to `debug/`),
find the new selector, and add it to the front of the matching list. Verified anchors:
- Login: `button.CvoHeader-loginButton` → Keycloak `#username`/`#password`/`#kc-login`
- Post-login modal: `.WalletModal-actionWrapper button`
- Search: `input[id^='autocomplete-origin']` / `input[id^='autocomplete-destination']`
  (type IATA code, pick first option), date `#Departure-date` → `td.cell[title='YYYY-MM-DD']`
- Results: card `article.CvoCollapsibleDirectFlightRow`, SELECT `button.CvoCollapsibleDirectFlightRow-action`,
  empty-state `.AvailabilityPage--no-result`

## Files

| File | Purpose |
|------|---------|
| `check_aycf.py` | The watcher (login, search, detect, notify) |
| `aycf_window.py` | Stdlib-only booking-window logic + Actions pre-flight gate |
| `config.json` | Routes + window/notify settings (committed, editable) |
| `.env` | **Your secrets** (git-ignored; see `.env.example`) |
| `.github/workflows/aycf.yml` | GitHub Actions cloud schedule |
| `state.json` | Auto-created notify-once state (git-ignored) |
| `install-cron.sh` | Optional local cron installer |
