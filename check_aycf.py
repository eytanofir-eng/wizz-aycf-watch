#!/usr/bin/env python3
"""
Wizz Air AYCF (All You Can Fly / MultiPass) availability watcher.

For each configured route+date it:
  1. Skips the route if "now" is outside the AYCF booking window
     (default: 72h to 3h before departure).
  2. Logs into https://multipass.wizzair.com with your AYCF account
     (via the Wizz/Keycloak SSO login).
  3. Searches the route/date and looks for a flight card with a SELECT button.
  4. If found, sends you a Telegram message with the flight details.
     You book manually — this only detects and notifies.

Secrets and routes live in config.json (see config.example.json).
Nothing is hardcoded in this file.

Usage:
    python3 check_aycf.py                 # check all in-window routes (headless)
    python3 check_aycf.py --debug         # save screenshots/HTML to debug/ at each step
    python3 check_aycf.py --config other.json
    python3 check_aycf.py --no-window     # ignore the 72h-3h gate (force a check)

Designed to be run on a schedule (cron / launchd) every few minutes once a
route's window is open.

The selectors below were verified against the live multipass.wizzair.com UI
(a Caravelo-powered, Keycloak-authenticated app). If Wizz changes the markup,
run with --debug and update the SELECTORS dict from the saved debug/ HTML.
"""

import argparse
import json
import logging
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from aycf_window import booking_window, in_window  # noqa: F401 (booking_window re-exported)

try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # certifi missing — fall back to system defaults
    _SSL_CTX = ssl.create_default_context()

# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #
BASE_URL = "https://multipass.wizzair.com/"
# Logged-in landing page that hosts the flight search form.
SEARCH_URL = "https://multipass.wizzair.com/w6/subscriptions/spa/private-page/wallets"

# --------------------------------------------------------------------------- #
# Selectors (verified live). Each entry is a list of fallbacks tried in order.
# --------------------------------------------------------------------------- #
SELECTORS = {
    "cookie_accept": [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
    ],
    # Header button that kicks off the Wizz/Keycloak SSO login.
    "login_open_panel": [
        "button.CvoHeader-loginButton",
        "button:has-text('Log in')",
        "button:has-text('Login')",
    ],
    # Keycloak login form (#kc-form-login).
    "email": ["#username", "input[name='username']", "input[type='email']"],
    "password": ["#password", "input[name='password']", "input[type='password']"],
    "login_submit": ["#kc-login", "input[type='submit'][name='login']", "#kc-form-login button[type='submit']"],
    # "Payment complete / wallet" success modal that pops up after activation.
    "modal_dismiss": [
        ".WalletModal-actionWrapper button",
        ".CvoModal button:has-text('Search for a flight')",
        ".vm--top-right-slot button",
    ],
    # Search form on the private page.
    "origin_input": ["input[id^='autocomplete-origin']"],
    "origin_options": ["#autocomplete-result-list-1 [role='option']", "#autocomplete-result-list-1 li"],
    "destination_input": ["input[id^='autocomplete-destination']"],
    "destination_options": ["#autocomplete-result-list-2 [role='option']", "#autocomplete-result-list-2 li"],
    "date_input": ["#Departure-date"],
    "date_next_month": ["button.Datepicker-btn-icon-right"],
    "search_submit": ["button.SearchCombo-submit"],
    # Results: a flight card's SELECT button.
    "select_button": [
        "button.CvoCollapsibleDirectFlightRow-action",
        "button:has-text('Select')",
        "a:has-text('Select')",
    ],
    "flight_row": ["article.CvoCollapsibleDirectFlightRow"],
    # Explicit "no flights" empty-state on the availability page.
    "no_flights": [
        ".AvailabilityPage--no-result",
        ".AvailabilityPage-noResultMessage",
        ".BookingAvailabilityView-noResults",
    ],
}

log = logging.getLogger("aycf")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines from a local .env into os.environ (no overrides).

    Used for local runs. In the cloud (GitHub Actions) the same variables are
    provided as real environment variables from encrypted Secrets, so there is
    no .env file there.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _placeholder(v: str) -> bool:
    return (not v) or "YOUR_" in v or "PASTE_" in v


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Config not found: {path}. Copy config.example.json -> {path.name} and fill it in.")
    load_dotenv()
    cfg = json.loads(path.read_text())

    if "routes" not in cfg or not cfg["routes"]:
        sys.exit("config.json has no routes.")

    # Secrets come from environment variables first (GitHub Secrets / .env),
    # falling back to a local config.json that still carries them.
    cfg.setdefault("wizz", {})
    cfg.setdefault("telegram", {})
    cfg["wizz"]["email"] = os.environ.get("WIZZ_EMAIL") or cfg["wizz"].get("email", "")
    cfg["wizz"]["password"] = os.environ.get("WIZZ_PASSWORD") or cfg["wizz"].get("password", "")
    cfg["telegram"]["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg["telegram"].get("bot_token", "")
    cfg["telegram"]["chat_id"] = str(
        os.environ.get("TELEGRAM_CHAT_ID") or cfg["telegram"].get("chat_id", "")
    )

    missing = []
    if _placeholder(cfg["wizz"]["email"]):
        missing.append("WIZZ_EMAIL")
    if _placeholder(cfg["wizz"]["password"]):
        missing.append("WIZZ_PASSWORD")
    if _placeholder(cfg["telegram"]["bot_token"]):
        missing.append("TELEGRAM_BOT_TOKEN")
    if _placeholder(cfg["telegram"]["chat_id"]):
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        sys.exit(
            "Missing secrets: " + ", ".join(missing) + ".\n"
            "Set them as environment variables (GitHub Secrets), or in a local .env file, "
            "or in config.json."
        )
    return cfg


# --------------------------------------------------------------------------- #
# Booking window: booking_window() / in_window() are imported from aycf_window
# (stdlib-only module, also used as the GitHub Actions pre-flight gate).
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def send_telegram(cfg: dict, text: str) -> bool:
    token = cfg["telegram"]["bot_token"]
    chat_id = str(cfg["telegram"]["chat_id"])
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20, context=_SSL_CTX) as resp:
            body = json.loads(resp.read().decode())
            if not body.get("ok"):
                log.error("Telegram API error: %s", body)
                return False
            return True
    except urllib.error.HTTPError as e:
        log.error("Telegram send failed: %s — %s", e, e.read().decode(errors="replace"))
        return False
    except urllib.error.URLError as e:
        log.error("Telegram send failed: %s", e)
        return False


# --------------------------------------------------------------------------- #
# Notify-once state (avoid spamming on every cron run)
# --------------------------------------------------------------------------- #
def route_key(route: dict) -> str:
    return f"{route['origin']}-{route['destination']}-{route['date']}"


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Playwright helpers
# --------------------------------------------------------------------------- #
def first_visible(page, selector_list, timeout=8000):
    """Return the first locator from the list that becomes visible, else None."""
    per = max(1000, timeout // max(1, len(selector_list)))
    for sel in selector_list:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=per)
            return loc
        except PWTimeout:
            continue
    return None


def any_count(page, selector_list) -> int:
    total = 0
    for sel in selector_list:
        try:
            total += page.locator(sel).count()
        except Exception:  # noqa: BLE001
            pass
    return total


def dump_debug(page, name: str):
    debug_dir = Path("debug")
    debug_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    png = debug_dir / f"{ts}-{name}.png"
    html = debug_dir / f"{ts}-{name}.html"
    try:
        page.screenshot(path=str(png), full_page=True)
        html.write_text(page.content())
        log.info("Saved debug artifacts: %s , %s", png, html)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not save debug artifacts: %s", e)


def dismiss_modal(page):
    """Close the 'Payment complete' / wallet success modal if it's up."""
    btn = first_visible(page, SELECTORS["modal_dismiss"], timeout=3000)
    if btn:
        try:
            btn.click()
            page.wait_for_timeout(2000)
            log.info("Dismissed post-login modal")
        except Exception:  # noqa: BLE001
            pass


def login(page, cfg: dict, debug: bool) -> bool:
    log.info("Opening %s", BASE_URL)
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)

    cookie = first_visible(page, SELECTORS["cookie_accept"], timeout=4000)
    if cookie:
        try:
            cookie.click()
            log.info("Dismissed cookie banner")
        except Exception:  # noqa: BLE001
            pass

    # Already logged in (persisted session)?
    if "/private-page" in page.url:
        log.info("Already logged in")
        return True

    opener = first_visible(page, SELECTORS["login_open_panel"], timeout=8000)
    if opener:
        try:
            opener.click()
            log.info("Clicked login -> redirecting to SSO")
        except Exception:  # noqa: BLE001
            pass

    email = first_visible(page, SELECTORS["email"], timeout=15000)
    pwd = first_visible(page, SELECTORS["password"], timeout=10000)
    if not email or not pwd:
        log.error("Could not find the login form.")
        if debug:
            dump_debug(page, "login-form-missing")
        return False

    email.fill(cfg["wizz"]["email"])
    pwd.fill(cfg["wizz"]["password"])
    submit = first_visible(page, SELECTORS["login_submit"], timeout=5000)
    if submit:
        submit.click()
    else:
        pwd.press("Enter")

    # Wait for the SSO round-trip back to the logged-in private page.
    try:
        page.wait_for_url("**/private-page/**", timeout=25000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2000)

    if "/private-page" in page.url:
        log.info("Logged in")
        if debug:
            dump_debug(page, "logged-in")
        return True

    log.error("Login did not complete; url=%s", page.url)
    if debug:
        dump_debug(page, "login-failed")
    return False


def select_autocomplete(page, input_selectors, option_selectors, code: str) -> bool:
    """Type an IATA code into an autocomplete and click the first matching option."""
    field = first_visible(page, input_selectors, timeout=8000)
    if not field:
        return False
    field.click()
    field.fill("")
    field.type(code, delay=80)
    page.wait_for_timeout(1500)
    option = first_visible(page, option_selectors, timeout=5000)
    if not option:
        return False
    option.click()
    page.wait_for_timeout(600)
    return True


def pick_date(page, date_str: str, max_months_forward: int = 4) -> str:
    """Open the datepicker and click the day cell for date_str (YYYY-MM-DD).

    Returns: 'ok' (selected), 'disabled' (date exists but not bookable per Wizz),
    or 'missing' (couldn't reach/locate the date).
    """
    di = first_visible(page, SELECTORS["date_input"], timeout=6000)
    if not di:
        return "missing"
    di.click()
    page.wait_for_timeout(1000)

    for _ in range(max_months_forward + 1):
        enabled = page.locator(f"td.cell[title='{date_str}']:not(.disabled):not(.not-current-month)")
        if enabled.count():
            enabled.first.click()
            page.wait_for_timeout(600)
            return "ok"
        # Is the date present in the current view but disabled (not bookable yet)?
        any_cell = page.locator(f"td.cell[title='{date_str}']:not(.not-current-month)")
        if any_cell.count():
            return "disabled"
        nxt = first_visible(page, SELECTORS["date_next_month"], timeout=2000)
        if not nxt:
            break
        nxt.click()
        page.wait_for_timeout(800)
    return "missing"


def extract_flights(page) -> list:
    """Return a list of short descriptions for each flight card with a SELECT button."""
    flights = []
    rows = page.locator(SELECTORS["flight_row"][0])
    for i in range(rows.count()):
        row = rows.nth(i)
        if row.locator(SELECTORS["select_button"][0]).count() == 0:
            continue
        txt = " ".join(row.inner_text().split())
        # The hour element is e.g. "21:55\nUTC+2"; keep just the HH:MM.
        dep = _first_line(_first_text(row, ".CvoCollapsibleDirectFlightRow-departure .CvoCollapsibleDirectFlightRow-hour"))
        arr = _first_line(_first_text(row, ".CvoCollapsibleDirectFlightRow-arrival .CvoCollapsibleDirectFlightRow-hour"))
        code = _first_text(row, ".CvoCollapsibleDirectFlightRow-flightCode")
        price = _first_text(row, ".CvoCollapsibleDirectFlightRow-price")
        parts = [p for p in (f"{dep}→{arr}" if dep or arr else "", code) if p]
        flights.append(" · ".join(parts) if parts else txt[:80])
    return flights


def _first_line(text: str) -> str:
    return text.splitlines()[0].strip() if text else ""


def _first_text(scope, selector: str) -> str:
    loc = scope.locator(selector).first
    try:
        if loc.count():
            return loc.inner_text().strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def check_route(page, route: dict, debug: bool):
    """Return (available: bool, flights: list[str])."""
    origin, dest, date = route["origin"], route["destination"], route["date"]
    log.info("Searching %s -> %s on %s", origin, dest, date)

    page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    dismiss_modal(page)

    if not select_autocomplete(page, SELECTORS["origin_input"], SELECTORS["origin_options"], origin):
        log.warning("Could not select origin %s", origin)
        if debug:
            dump_debug(page, f"origin-{origin}")
        return False, []

    if not select_autocomplete(page, SELECTORS["destination_input"], SELECTORS["destination_options"], dest):
        # No destination option usually means this route isn't offered / has nothing.
        log.info("Destination %s not selectable from %s (route not offered right now)", dest, origin)
        if debug:
            dump_debug(page, f"dest-{dest}")
        return False, []

    state = pick_date(page, date)
    if state == "disabled":
        log.info("Date %s not yet bookable per Wizz (outside their window).", date)
        return False, []
    if state == "missing":
        log.warning("Could not locate date %s in the picker.", date)
        if debug:
            dump_debug(page, f"date-{date}")
        return False, []

    submit = first_visible(page, SELECTORS["search_submit"], timeout=6000)
    if not submit:
        log.warning("Search button not found / not enabled.")
        if debug:
            dump_debug(page, "search-button")
        return False, []
    submit.click()

    # Wait for the availability page to resolve to either results or no-result.
    try:
        page.wait_for_url("**/availability/**", timeout=15000)
    except PWTimeout:
        pass
    # Poll briefly for a select button or the no-result state.
    for _ in range(10):
        if any_count(page, SELECTORS["select_button"]) > 0:
            break
        if any_count(page, SELECTORS["no_flights"]) > 0:
            break
        page.wait_for_timeout(700)

    if any_count(page, SELECTORS["select_button"]) > 0:
        flights = extract_flights(page)
        log.info("FOUND availability %s -> %s on %s: %s", origin, dest, date, flights or "(card present)")
        if debug:
            dump_debug(page, f"found-{route_key(route)}")
        return True, flights

    if any_count(page, SELECTORS["no_flights"]) > 0:
        log.info("No flights for %s -> %s on %s", origin, dest, date)
    else:
        log.info("No SELECT button and no explicit empty-state for %s -> %s on %s", origin, dest, date)
        if debug:
            dump_debug(page, f"notfound-{route_key(route)}")
    return False, []


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Wizz AYCF availability watcher")
    parser.add_argument("--config", default="config.json", help="Path to config file")
    parser.add_argument("--debug", action="store_true", help="Save screenshots/HTML to debug/ at each step")
    parser.add_argument("--no-window", action="store_true", help="Ignore the 72h-3h window gate and check anyway")
    parser.add_argument("--state", default="state.json", help="Path to notify-once state file")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_config(Path(args.config))
    state_path = Path(args.state)
    state = load_state(state_path)
    notify_once = cfg.get("notify_once", True)
    headless = cfg.get("headless", True)

    to_check = []
    for route in cfg["routes"]:
        is_open, reason = in_window(route, cfg)
        label = f"{route['origin']}->{route['destination']} {route['date']}"
        if is_open or args.no_window:
            log.info("Route %s: WILL CHECK (%s)", label, reason)
            to_check.append(route)
        else:
            log.info("Route %s: skipping — %s", label, reason)

    if not to_check:
        log.info("No routes in window. Nothing to do.")
        return 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            if not login(page, cfg, args.debug):
                log.error("Aborting: login failed.")
                return 2

            for route in to_check:
                key = route_key(route)
                try:
                    available, flights = check_route(page, route, args.debug)
                except Exception as e:  # noqa: BLE001
                    log.exception("Error checking %s: %s", key, e)
                    if args.debug:
                        dump_debug(page, f"error-{key}")
                    continue

                if available:
                    already = state.get(key, {}).get("notified", False)
                    if notify_once and already:
                        log.info("Already notified for %s; not sending again.", key)
                    else:
                        detail = ("\n" + "\n".join(f"• {f}" for f in flights)) if flights else ""
                        d = route['date']  # YYYY-MM-DD
                        display_date = f"{d[8:10]}-{d[5:7]}-{d[:4]}"
                        msg = (
                            f"✈️ <b>Wizz AYCF flight available!</b>\n"
                            f"<b>{route['origin']} → {route['destination']}</b>  ({display_date})"
                            f"{detail}\n"
                            f'<a href="{SEARCH_URL}">Book it here now</a>'
                        )
                        if send_telegram(cfg, msg):
                            log.info("Telegram notification sent for %s", key)
                            state[key] = {"notified": True, "at": datetime.now(timezone.utc).isoformat()}
                        else:
                            log.error("Failed to send Telegram for %s", key)
                else:
                    if state.get(key, {}).get("notified"):
                        state[key] = {"notified": False, "at": datetime.now(timezone.utc).isoformat()}
        finally:
            save_state(state_path, state)
            context.close()
            browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
