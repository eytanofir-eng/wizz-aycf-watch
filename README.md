# Wizz AYCF availability watcher

Logs into [multipass.wizzair.com](https://multipass.wizzair.com) with your **All You Can Fly**
account, checks whether a bookable flight card (one with a **SELECT** button) exists for a
given route + date, and pings you on **Telegram** when it does. It only detects and notifies —
you book manually.

It respects the AYCF booking window: a route is only checked when "now" is between
**72h and 3h before departure**. Outside that window the route is skipped.

## Setup

Python 3.12 is already on this machine; Node is not, so this uses **Playwright for Python**.

```bash
cd ~/Claude/wizz-aycf-watch

# 1. Isolated environment + dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

# 2. Fill in your secrets and routes
#    config.json already has your two routes (BUD->TLV 30 Jun, TLV->BUD 3 Jul).
#    Open it and replace the placeholder Wizz email/password and Telegram token/chat_id.
$EDITOR config.json
```

### Getting your Telegram bot token + chat_id
1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts → it gives you the
   **bot token** (looks like `123456:ABC...`). Put it in `config.json` → `telegram.bot_token`.
2. Send any message to your new bot (so it's allowed to message you back).
3. Get your numeric **chat_id**: open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read
   `result[].message.chat.id`. Put it in `config.json` → `telegram.chat_id`.

## Run

```bash
source .venv/bin/activate

python3 check_aycf.py            # check all in-window routes, headless
python3 check_aycf.py --debug    # also dump screenshots/HTML to debug/ at each step
python3 check_aycf.py --no-window   # ignore the 72h-3h gate (force a check now)
```

Today (24 Jun) both routes are still **outside** their windows, so a normal run skips both.
The BUD→TLV window opens ~26 Jun 22:00 UTC, TLV→BUD ~29 Jun 21:00 UTC. Wizz's own date
picker also only enables dates inside the booking window, so `--no-window` runs of the two
target dates correctly report "not yet bookable" until then.

**Status: verified end-to-end against the live site** — login (Wizz/Keycloak SSO), the
post-activation "Payment complete" modal, origin/destination autocomplete, the date picker,
the search, flight-card/SELECT-button detection, flight-detail extraction, and the Telegram
send have all been confirmed working.

## Run it on a schedule

The script is built to run repeatedly. Once a window is open, run it every few minutes.
With `notify_once: true` (default) you get **one** Telegram alert per route until the flight
stops appearing, so a tight cron won't spam you.

Example crontab (every 5 minutes):

```cron
*/5 * * * * cd ~/Claude/wizz-aycf-watch && .venv/bin/python check_aycf.py >> aycf.log 2>&1
```

## How the booking window is computed

AYCF flights on a date depart at various times, and each flight is bookable from 72h to 3h
before *its* departure. Since we only have the date, the script uses the widest safe window:
opens 72h before **00:00** local and closes 3h before **23:59** local on the date — so it
never skips a check while something on that date could still be bookable. Timezone is per
route (`Europe/Budapest`, `Asia/Jerusalem`).

## If Wizz changes the page

The selectors in `check_aycf.py` (the `SELECTORS` dict near the top) were verified against the
live site, but Wizz can change the markup. If a run starts logging "could not select…" or
"search button not found", run with `--debug`: it saves screenshots + page HTML to `debug/` at
each step. Open the saved HTML, copy the real selector, and add it to the front of the matching
list in `SELECTORS` (each entry is a list of fallbacks tried in order).

Key verified selectors, for reference:
- Login: header `button.CvoHeader-loginButton` → Keycloak `#username` / `#password` / `#kc-login`
- Post-login modal: `.WalletModal-actionWrapper button` ("Search for a flight")
- Search: `input[id^='autocomplete-origin']` / `input[id^='autocomplete-destination']`
  (type the IATA code, pick the first option), date `#Departure-date` →
  `td.cell[title='YYYY-MM-DD']` (disabled days carry `.disabled`)
- Results: flight card `article.CvoCollapsibleDirectFlightRow`,
  SELECT button `button.CvoCollapsibleDirectFlightRow-action`,
  empty-state `.AvailabilityPage--no-result`

## Files

| File | Purpose |
|------|---------|
| `check_aycf.py` | The watcher script |
| `config.json` | **Your secrets + routes** (git-ignored, never commit) |
| `config.example.json` | Template to copy |
| `state.json` | Auto-created; tracks notify-once state |
| `debug/` | Auto-created by `--debug`; screenshots + HTML |

`config.json` and `state.json` are in `.gitignore` so your credentials never get committed.
