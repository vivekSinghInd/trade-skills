"""Shared Indian-market conventions for the India extensions.

The original repository is U.S.-market code.  New Indian skills import this
module instead of duplicating timezone, symbol, session, and tick logic.
Holiday data is intentionally supplied by the caller or configuration so it
can be refreshed from the exchange without changing the strategy code.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from nse_calendar import calendar_status as _calendar_status
from nse_calendar import is_trading_day as _calendar_is_trading_day
from nse_calendar import next_trading_day as _calendar_next_trading_day


IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
NSE_OPEN = _dt.time(9, 15)
NSE_CLOSE = _dt.time(15, 30)
NSE_FLAT_BY = _dt.time(15, 15)

# These are the standard price bands used by the extension.  A broker or
# dated security master can override the tick for a particular instrument.
TICK_BANDS = (
    (250.0, 0.01),
    (1000.0, 0.05),
    (5000.0, 0.10),
    (10000.0, 0.50),
    (20000.0, 1.00),
    (float("inf"), 5.00),
)


def parse_date(value):
    """Parse an ISO date or an ISO timestamp into a date."""
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    text = str(value or "").strip()
    if not text:
        raise ValueError("date is required")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(text).date()
    except ValueError:
        return _dt.date.fromisoformat(text[:10])


def to_ist(value=None):
    """Return an aware datetime in IST.

    Naive values are treated as UTC, which matches timestamps returned by the
    public chart endpoints and keeps the conversion explicit.
    """
    value = value or _dt.datetime.now(_dt.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(IST)


def nse_symbol(symbol, exchange="NSE"):
    """Return the Yahoo-style symbol for an NSE or BSE instrument."""
    value = str(symbol or "").strip().upper()
    if not value:
        raise ValueError("symbol is required")
    if value.endswith((".NS", ".BO")):
        return value
    suffix = ".BO" if str(exchange).upper() == "BSE" else ".NS"
    return value + suffix


def is_trading_day(day, holidays=()):
    """Return true only for a known NSE equity trading day."""
    calendar = None
    if holidays:
        calendar = {"holidays": {parse_date(item).isoformat(): "configured holiday"
                                  for item in holidays},
                    "known_years": {parse_date(item).year for item in holidays}}
    return _calendar_is_trading_day(parse_date(day), calendar)


def session_status(now=None, holidays=()):
    """Describe the NSE regular session without making network calls."""
    local = to_ist(now)
    calendar = None
    if holidays:
        calendar = {"holidays": {parse_date(item).isoformat(): "configured holiday"
                                  for item in holidays},
                    "known_years": {parse_date(item).year for item in holidays}}
    status = _calendar_status(local.date(), calendar)
    if not status["trading_day"]:
        return {"status": "CLOSED", "phase": status["reason"],
                "calendar_known": status["calendar_known"], "holiday": status.get("holiday"),
                "now": local.isoformat()}
    current = local.time().replace(tzinfo=None)
    if current < NSE_OPEN:
        phase = "PRE_OPEN"
    elif current >= NSE_CLOSE:
        phase = "POST_CLOSE"
    else:
        phase = "OPEN"
    return {
        "status": "OPEN" if phase == "OPEN" else "CLOSED",
        "phase": phase,
        "calendar_known": status["calendar_known"],
        "now": local.isoformat(),
        "open": NSE_OPEN.strftime("%H:%M"),
        "close": NSE_CLOSE.strftime("%H:%M"),
        "flat_by": NSE_FLAT_BY.strftime("%H:%M"),
    }


def cash_tick(price, override=None):
    """Return a configurable/default INR tick size for a cash price."""
    if override is not None:
        tick = float(override)
        if tick <= 0:
            raise ValueError("tick size must be positive")
        return tick
    value = abs(float(price))
    for ceiling, tick in TICK_BANDS:
        if value < ceiling:
            return tick
    return TICK_BANDS[-1][1]


def tick_round(price, tick=None, up=False):
    """Round an INR price onto a tick, down for stops and up for targets."""
    value = Decimal(str(price))
    step = Decimal(str(tick if tick is not None else cash_tick(float(price))))
    mode = ROUND_CEILING if up else ROUND_FLOOR
    return float((value / step).to_integral_value(rounding=mode) * step)


def next_trading_day(day, holidays=()):
    """Return the next configured NSE trading day."""
    if holidays:
        calendar = {"holidays": {parse_date(item).isoformat(): "configured holiday"
                                  for item in holidays},
                    "known_years": {parse_date(item).year for item in holidays}}
        return _calendar_next_trading_day(parse_date(day), calendar)
    return _calendar_next_trading_day(parse_date(day))
