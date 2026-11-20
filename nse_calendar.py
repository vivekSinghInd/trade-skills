"""NSE equity-session calendar helpers.

The exchange publishes a new calendar and occasional special-session notices.
This module ships the official 2026 equity holidays currently needed by this
workspace, exposes the source metadata, and refuses to call an unknown future
year production-ready.  A dated JSON calendar can extend or override it.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path


NSE_HOLIDAY_SOURCE = (
    "https://www.nseindia.com/resources/exchange-communication-holidays?apiversion=v2"
)

# Official NSE equity holidays for 2026, including the January 15 Maharashtra
# municipal-election holiday shown on the exchange calendar page.  Sunday and
# Saturday holidays are retained for audit but are non-trading weekends anyway.
NSE_EQUITY_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
    "2026-02-15": "Mahashivratri (Sunday)",
    "2026-03-21": "Id-Ul-Fitr (Saturday)",
    "2026-08-15": "Independence Day (Saturday)",
    "2026-11-08": "Diwali Laxmi Pujan / Muhurat Trading (Sunday)",
}

KNOWN_YEARS = {2026}


def _date(value):
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(text).date()
    except ValueError:
        return _dt.date.fromisoformat(text[:10])


def load_calendar(path=None):
    """Return a normalized calendar document, optionally merged with JSON."""
    holidays = dict(NSE_EQUITY_HOLIDAYS)
    special_sessions = {}
    source = NSE_HOLIDAY_SOURCE
    retrieved_at = "2026-11-13"
    if path:
        target = Path(path)
        try:
            with open(target, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ValueError("cannot read NSE calendar: %s" % path) from exc
        if not isinstance(document, dict):
            raise ValueError("NSE calendar must be a JSON object")
        for key, value in (document.get("holidays") or {}).items():
            holidays[_date(key).isoformat()] = str(value)
        special_sessions.update(document.get("special_sessions") or {})
        source = document.get("source") or source
        retrieved_at = document.get("retrieved_at") or retrieved_at
    return {"source": source, "retrieved_at": retrieved_at,
            "known_years": sorted(KNOWN_YEARS | {int(k[:4]) for k in holidays}),
            "holidays": holidays, "special_sessions": special_sessions}


def calendar_status(day, calendar=None):
    d = _date(day)
    document = calendar or load_calendar()
    holiday = document.get("holidays", {}).get(d.isoformat())
    known = d.year in set(document.get("known_years", ()))
    if d.weekday() >= 5:
        return {"date": d.isoformat(), "calendar_known": known, "trading_day": False,
                "reason": "weekend", "holiday": holiday}
    if holiday:
        return {"date": d.isoformat(), "calendar_known": known, "trading_day": False,
                "reason": "NSE holiday: %s" % holiday, "holiday": holiday}
    if not known:
        return {"date": d.isoformat(), "calendar_known": False, "trading_day": False,
                "reason": "NSE calendar is unknown for %d" % d.year, "holiday": None}
    special = document.get("special_sessions", {}).get(d.isoformat())
    return {"date": d.isoformat(), "calendar_known": True, "trading_day": True,
            "reason": "regular session", "holiday": None, "special_session": special}


def is_trading_day(day, calendar=None):
    return bool(calendar_status(day, calendar).get("trading_day"))


def next_trading_day(day, calendar=None):
    result = _date(day) + _dt.timedelta(days=1)
    while not is_trading_day(result, calendar):
        result += _dt.timedelta(days=1)
    return result

