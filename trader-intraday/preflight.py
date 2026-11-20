#!/usr/bin/env python3
"""Production-readiness checks for the NSE paper workflow.

The command is deliberately a gate, not an order runner.  It separates
calendar, universe, credential, configuration and candle-data checks and
reports exactly which checks passed or failed.
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

from angelone_client import AngelOneCredentials
from india_market import IST, NSE_CLOSE, NSE_OPEN
from intraday import IntradayError, load_source, normalize_bars, session_bars
from nse_calendar import calendar_status


def _now(value=None):
    if value is None:
        return dt.datetime.now(IST)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _check(name, ok, detail):
    return {"name": name, "status": "PASS" if ok else "FAIL", "detail": detail}


def _universe_check(path, now, max_age_hours):
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        rows = document.get("instruments", []) if isinstance(document, dict) else []
        fetched = document.get("fetched_at") if isinstance(document, dict) else None
        if not rows or not fetched:
            return _check("universe", False, "universe is empty or missing fetched_at")
        raw_stamp = str(fetched).strip()
        stamp = None
        for candidate in (raw_stamp.replace("Z", "+00:00"),
                          raw_stamp.replace(" IST", "")):
            try:
                stamp = dt.datetime.fromisoformat(candidate)
                break
            except ValueError:
                pass
        if stamp is None:
            for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    stamp = dt.datetime.strptime(raw_stamp, fmt)
                    break
                except ValueError:
                    pass
        if stamp is None:
            raise ValueError("unsupported fetched_at timestamp: %s" % raw_stamp)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        age = (now.astimezone(dt.timezone.utc) - stamp.astimezone(dt.timezone.utc)).total_seconds() / 3600.0
        return _check("universe", age <= max_age_hours,
                      "%d instruments; age %.1f hours (limit %.1f)" % (len(rows), age, max_age_hours))
    except (OSError, ValueError, TypeError) as exc:
        return _check("universe", False, str(exc))


def _data_check(bars, day, now, interval_minutes=5):
    try:
        normalized = normalize_bars(bars)
        session = session_bars(normalized, day)
        if not session:
            return _check("candles", False, "no regular-session bars for %s" % day)
        first = session[0]["timestamp"]
        last = session[-1]["timestamp"]
        first_stamp = dt.datetime.fromisoformat(first)
        last_stamp = dt.datetime.fromisoformat(last)
        if first_stamp.time().replace(tzinfo=None) != NSE_OPEN:
            return _check("candles", False, "session starts at %s, expected 09:15" % first_stamp.strftime("%H:%M"))
        if last_stamp + dt.timedelta(minutes=interval_minutes) > now:
            return _check("candles", False, "last bar is still forming: %s" % last)
        gaps = []
        for before, after in zip(session, session[1:]):
            left = dt.datetime.fromisoformat(before["timestamp"])
            right = dt.datetime.fromisoformat(after["timestamp"])
            if (right - left).total_seconds() > interval_minutes * 60:
                gaps.append((left, right))
        detail = "%d completed bars; last %s; gaps %d" % (len(session), last, len(gaps))
        return _check("candles", not gaps, detail if not gaps else detail + "; incomplete timeline")
    except (IntradayError, ValueError, TypeError) as exc:
        return _check("candles", False, str(exc))


def run_preflight(day, universe_path, capital=100000.0, max_loss=1000.0,
                  max_trade_value=10000.0, max_daily_loss=3000.0,
                  max_age_hours=48.0, symbol=None, source=None, offline=None,
                  credentials_file=None, interval="5m", range_name="1d", now=None,
                  monitor_source=None):
    current = _now(now)
    checks = []
    market = calendar_status(day)
    checks.append(_check("calendar", market["trading_day"],
                         "%s; known=%s" % (market["reason"], market["calendar_known"])))
    checks.append(_universe_check(universe_path, current, float(max_age_hours)))
    values = (("capital", capital), ("max_loss", max_loss),
              ("max_daily_loss", max_daily_loss))
    for name, value in values:
        try:
            ok = float(value) > 0
        except (TypeError, ValueError):
            ok = False
        checks.append(_check(name, ok, str(value)))
    try:
        cap = float(max_trade_value)
        checks.append(_check("trade_cap", 0 < cap <= 10000.0,
                             "INR %.2f; absolute limit INR 10000" % cap))
    except (TypeError, ValueError):
        checks.append(_check("trade_cap", False, str(max_trade_value)))

    if credentials_file or source == "angelone" or monitor_source == "angelone":
        try:
            if credentials_file:
                AngelOneCredentials.from_json(credentials_file)
            else:
                AngelOneCredentials.from_environment()
            checks.append(_check("angelone_credentials", True, "fields validated locally; no login/order sent"))
        except Exception as exc:
            checks.append(_check("angelone_credentials", False, str(exc)))

    if source and symbol and market["trading_day"]:
        try:
            bars, actual_source = load_source(
                symbol, offline=offline, yahoo=source == "yahoo", angelone=source == "angelone",
                credentials_file=credentials_file, universe_path=universe_path,
                interval=interval, range_name=range_name, day=day,
            )
            minutes = int(str(interval).rstrip("m"))
            checks.append(_data_check(bars, day, current, minutes))
            checks[-1]["detail"] += "; source=%s" % actual_source
        except Exception as exc:
            checks.append(_check("candles", False, str(exc)))
    elif symbol and not source:
        checks.append(_check("candles", False, "choose --offline, --yahoo or --angelone for a data check"))

    passed = sum(item["status"] == "PASS" for item in checks)
    failed = len(checks) - passed
    return {
        "market": "NSE", "currency": "INR", "day": str(day),
        "status": "READY_FOR_PAPER" if failed == 0 else "NOT_READY",
        "paper_only": True, "execution_enabled": False,
        "checks": checks, "passed": passed, "failed": failed,
        "checked_at": current.isoformat(),
        "note": "A paper preflight is not authorization to place a live order.",
    }


def _parser():
    parser = argparse.ArgumentParser(description="NSE paper workflow preflight")
    parser.add_argument("--day", default=dt.datetime.now(IST).date().isoformat())
    parser.add_argument("--universe", default=os.path.join("trader", "nse_universe.json"))
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--max-loss", type=float, default=1000.0)
    parser.add_argument("--max-trade-value", type=float, default=10000.0)
    parser.add_argument("--max-daily-loss", type=float, default=3000.0)
    parser.add_argument("--max-age-hours", type=float, default=48.0)
    parser.add_argument("--symbol")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--offline")
    source.add_argument("--yahoo", action="store_true")
    source.add_argument("--angelone", action="store_true")
    parser.add_argument("--credentials-file")
    parser.add_argument("--monitor-source", choices=("angelone", "yahoo"))
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--range", dest="range_name", default="1d")
    parser.add_argument("--now")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        source = ("offline" if args.offline else
                  "angelone" if args.angelone else "yahoo" if args.yahoo else None)
        document = run_preflight(
            args.day, args.universe, args.capital, args.max_loss, args.max_trade_value,
            args.max_daily_loss, args.max_age_hours, args.symbol, source, args.offline,
            args.credentials_file, args.interval, args.range_name, args.now,
            args.monitor_source,
        )
        if args.json:
            print(json.dumps(document, indent=2, sort_keys=True))
        else:
            print("NSE PREFLIGHT  %s  %s  (%d pass, %d fail)" %
                  (document["day"], document["status"], document["passed"], document["failed"]))
            for check in document["checks"]:
                print("  %-22s %-4s %s" % (check["name"], check["status"], check["detail"]))
        return 0 if document["status"] == "READY_FOR_PAPER" else 1
    except (OSError, ValueError, TypeError, IntradayError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
