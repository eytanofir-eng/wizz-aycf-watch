#!/usr/bin/env python3
"""Booking-window logic for the AYCF watcher (standard library only).

Kept free of the Playwright dependency so it can be used as a fast pre-flight
"gate": the GitHub Actions workflow runs this first and only installs the
browser + runs the full watcher when at least one route is inside its window.

Run directly:
    python aycf_window.py            # exits 0 if any route is in-window, else 1
"""

import json
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def booking_window(route: dict, cfg: dict):
    """Return (open_at, close_at) in UTC for a route's date.

    AYCF flights on a date depart at various times, and each flight is bookable
    from `open_hours_before` to `close_hours_before` before its departure.
    Across a whole day the union of those windows is
    [earliest_flight - open, latest_flight - close]; we approximate the earliest
    flight as 00:00 and the latest as 23:59 local time so we never skip a check
    while something on that date could still be bookable.
    """
    tz = ZoneInfo(route["tz"])
    date = datetime.strptime(route["date"], "%Y-%m-%d").date()
    day_start = datetime.combine(date, dtime(0, 0), tzinfo=tz)
    day_end = datetime.combine(date, dtime(23, 59), tzinfo=tz)

    win = cfg.get("booking_window", {})
    open_h = win.get("open_hours_before", 72)
    close_h = win.get("close_hours_before", 3)

    open_at = (day_start - timedelta(hours=open_h)).astimezone(timezone.utc)
    close_at = (day_end - timedelta(hours=close_h)).astimezone(timezone.utc)
    return open_at, close_at


def in_window(route: dict, cfg: dict):
    """Return (is_open: bool, reason: str)."""
    open_at, close_at = booking_window(route, cfg)
    now = datetime.now(timezone.utc)
    if now < open_at:
        return False, f"too early — window opens {open_at.isoformat()} (in {open_at - now})"
    if now > close_at:
        return False, f"too late — window closed {close_at.isoformat()}"
    return True, f"open until {close_at.isoformat()}"


def _main() -> int:
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config.json")
    cfg = json.loads(cfg_path.read_text())
    any_open = False
    for route in cfg.get("routes", []):
        is_open, reason = in_window(route, cfg)
        label = f"{route['origin']}->{route['destination']} {route['date']}"
        print(("OPEN   " if is_open else "skip   ") + f"{label}: {reason}")
        any_open = any_open or is_open
    print("ANY_OPEN=" + ("true" if any_open else "false"))
    return 0 if any_open else 1


if __name__ == "__main__":
    sys.exit(_main())
