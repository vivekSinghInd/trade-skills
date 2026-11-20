#!/usr/bin/env python3
"""Evaluate paper stop, target and flat-by events from completed NSE bars.

This is the NSE equivalent of a price-alert checker for research and paper
tracking.  It reads a saved plan and completed OHLCV bars, reports the first
deterministic event, and never sends a notification or an order.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from india_market import IST, NSE_FLAT_BY
from intraday import IntradayError, _parse_timestamp, load_source, session_bars
from nse_calendar import calendar_status


def _now(value=None):
    if value is None:
        return dt.datetime.now(IST)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise IntradayError("--now must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def completed_bars(bars, now=None, interval_minutes=5):
    """Return only bars whose interval has closed by ``now``."""
    current = _now(now)
    result = []
    for bar in bars:
        stamp = _parse_timestamp(bar["timestamp"])
        if stamp + dt.timedelta(minutes=interval_minutes) <= current:
            result.append(bar)
    return result


def _flat_time(plan):
    text = str(plan.get("flat_by") or "").strip()
    if not text:
        return NSE_FLAT_BY
    try:
        hour, minute = [int(part) for part in text.split(":", 1)]
        result = dt.time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise IntradayError("plan flat_by must be HH:MM") from exc
    return result


def check_plan(plan, bars, day=None, now=None, interval_minutes=5):
    """Return the first stop, target or flat-by event visible in completed bars."""
    if not isinstance(plan, dict):
        raise IntradayError("plan must be a JSON object")
    if str(plan.get("status", "PAPER_SETUP")) == "BLOCKED":
        return {"status": "BLOCKED", "reason": plan.get("reason", "plan is blocked"),
                "execution_enabled": False}
    try:
        entry = float(plan["entry"])
        stop = float(plan["stop"])
        target = float(plan["target"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IntradayError("plan needs numeric entry, stop and target") from exc
    if entry <= 0 or stop <= 0 or target <= 0 or stop >= entry or target <= entry:
        raise IntradayError("plan prices must satisfy stop < entry < target")

    signal_ref = plan.get("signal_timestamp")
    entry_ref = plan.get("entry_timestamp")
    reference = entry_ref or signal_ref
    entry_stamp = _parse_timestamp(reference) if reference else None
    selected_day = day or plan.get("signal_date")
    if not selected_day and entry_stamp:
        selected_day = entry_stamp.date().isoformat()
    if selected_day:
        market_day = calendar_status(selected_day)
        if not market_day["trading_day"]:
            return {"status": "BLOCKED", "reason": market_day["reason"],
                    "calendar_known": market_day["calendar_known"],
                    "execution_enabled": False}
    session = session_bars(bars, selected_day)
    closed = completed_bars(session, now, interval_minutes)
    if entry_stamp:
        if entry_ref:
            closed = [bar for bar in closed if _parse_timestamp(bar["timestamp"]) >= entry_stamp]
        else:
            closed = [bar for bar in closed if _parse_timestamp(bar["timestamp"]) > entry_stamp]

    flat_by = _flat_time(plan)
    current = _now(now)
    for bar in closed:
        stamp = _parse_timestamp(bar["timestamp"])
        if stamp.time().replace(tzinfo=None) >= flat_by:
            return {
                "status": "TRIGGERED", "event": "flat", "event_timestamp": bar["timestamp"],
                "event_price": bar["open"], "reason": "flat-by time reached",
                "execution_enabled": False,
            }
        # Stop-first is the same conservative convention used by backtest_session.
        if bar["low"] <= stop:
            return {
                "status": "TRIGGERED", "event": "stop", "event_timestamp": bar["timestamp"],
                "event_price": min(bar["open"], stop),
                "reason": "completed bar touched the stop; stop is checked first",
                "execution_enabled": False,
            }
        if bar["high"] >= target:
            return {
                "status": "TRIGGERED", "event": "target", "event_timestamp": bar["timestamp"],
                "event_price": max(bar["open"], target),
                "reason": "completed bar touched the target",
                "execution_enabled": False,
            }

    if entry_stamp and current.date() == entry_stamp.date() and current.time().replace(tzinfo=None) >= flat_by:
        last = closed[-1] if closed else None
        flat_stamp = dt.datetime.combine(current.date(), flat_by).replace(tzinfo=IST)
        return {
            "status": "EXPIRED", "event": "flat", "event_timestamp": flat_stamp.isoformat(),
            "event_price": last["close"] if last else None,
            "reason": "flat-by time passed without a stop or target bar",
            "execution_enabled": False,
        }
    return {
        "status": "WAITING", "event": None, "observed_bars": len(closed),
        "reason": "no completed bar has touched stop, target or flat-by",
        "execution_enabled": False,
    }


def check_live_price(plan, price, day=None, now=None):
    """Evaluate one broker-side live price for a paper position.

    This path is for monitoring after Yahoo has selected a setup. It does not
    submit an order; it only returns the paper event that the journal should
    record. The polling loop supplies the timestamp because SmartAPI's LTP
    response does not reliably include one for every instrument.
    """
    if not isinstance(plan, dict):
        raise IntradayError("plan must be a JSON object")
    try:
        entry = float(plan["entry"])
        stop = float(plan["stop"])
        target = float(plan["target"])
        last = float(price)
    except (KeyError, TypeError, ValueError) as exc:
        raise IntradayError("plan and live price need numeric values") from exc
    if (entry <= 0 or stop <= 0 or target <= 0 or last <= 0 or
            stop >= entry or target <= entry):
        raise IntradayError("plan prices must satisfy stop < entry < target")

    current = _now(now)
    reference = plan.get("entry_timestamp") or plan.get("signal_timestamp")
    entry_stamp = _parse_timestamp(reference) if reference else None
    selected_day = str(day or plan.get("signal_date") or
                       (entry_stamp.date().isoformat() if entry_stamp else current.date().isoformat()))
    market_day = calendar_status(selected_day)
    if not market_day["trading_day"]:
        return {"status": "BLOCKED", "reason": market_day["reason"],
                "calendar_known": market_day["calendar_known"],
                "execution_enabled": False}

    flat_by = _flat_time(plan)
    if current.time().replace(tzinfo=None) >= flat_by:
        return {
            "status": "TRIGGERED", "event": "flat",
            "event_timestamp": current.isoformat(), "event_price": last,
            "reason": "flat-by time reached using Angel One LTP",
            "execution_enabled": False,
        }
    if last <= stop:
        return {
            "status": "TRIGGERED", "event": "stop",
            "event_timestamp": current.isoformat(), "event_price": last,
            "reason": "Angel One LTP is at or below the stop",
            "execution_enabled": False,
        }
    if last >= target:
        return {
            "status": "TRIGGERED", "event": "target",
            "event_timestamp": current.isoformat(), "event_price": last,
            "reason": "Angel One LTP is at or above the target",
            "execution_enabled": False,
        }
    return {
        "status": "WAITING", "event": None, "last_price": last,
        "reason": "Angel One LTP has not touched stop, target or flat-by",
        "execution_enabled": False,
    }


def _write(path, document):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return str(target)


def _parser():
    parser = argparse.ArgumentParser(description="Check a paper NSE intraday plan")
    parser.add_argument("--plan", default=os.path.join("trader", "intraday_plan.json"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv")
    source.add_argument("--offline")
    source.add_argument("--yahoo", action="store_true")
    source.add_argument("--angelone", action="store_true")
    parser.add_argument("--symbol", help="override the plan symbol")
    parser.add_argument("--credentials-file")
    parser.add_argument("--universe", default=os.path.join("trader", "nse_universe.json"))
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--range", dest="range_name", default="1d")
    parser.add_argument("--day")
    parser.add_argument("--now")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--out", default=os.path.join("trader", "intraday_alert.json"))
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        with open(args.plan, encoding="utf-8") as handle:
            plan = json.load(handle)
        symbol = (args.symbol or plan.get("symbol") or "").upper()
        if not symbol:
            raise IntradayError("plan or --symbol must provide a symbol")
        bars, source = load_source(
            symbol, args.csv, args.offline, args.yahoo, args.angelone,
            args.credentials_file, args.universe, args.interval, args.range_name,
            args.day,
        )
        try:
            interval_minutes = int(str(args.interval).rstrip("m"))
        except ValueError as exc:
            raise IntradayError("interval must be a minute interval such as 5m") from exc
        document = check_plan(plan, bars, args.day, args.now, interval_minutes)
        document.update({"market": "NSE", "currency": "INR", "symbol": symbol,
                         "source": source, "mode": "paper_alert_check"})
        if args.write:
            print("Wrote %s" % _write(args.out, document))
        if args.json:
            print(json.dumps(document, indent=2, sort_keys=True))
        else:
            print("NSE ALERT  %s  %s" % (document["status"], document.get("reason", "")))
        return 0 if document["status"] == "TRIGGERED" else 1
    except (IntradayError, OSError, ValueError, TypeError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
