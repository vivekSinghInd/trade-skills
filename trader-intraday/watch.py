#!/usr/bin/env python3
"""Poll completed NSE bars until the long-only ORB signal triggers or expires."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from india_market import IST, NSE_CLOSE
from nse_calendar import calendar_status
from intraday import IntradayError, load_source, signal_for


def completed_bars(bars, now=None, interval_minutes=5):
    current = now or dt.datetime.now(IST)
    result = []
    for bar in bars:
        stamp = dt.datetime.fromisoformat(bar["timestamp"])
        if stamp + dt.timedelta(minutes=interval_minutes) <= current:
            result.append(bar)
    return result


def watch_once(symbol, loader, day, now=None, interval_minutes=5, range_minutes=None):
    bars, source = loader(symbol)
    closed = completed_bars(bars, now, interval_minutes)
    signal = signal_for(closed, range_minutes or interval_minutes * 3, day)
    signal.update({"symbol": symbol.upper(), "source": source, "execution_enabled": False})
    return signal


def _parser():
    parser = argparse.ArgumentParser(description="Watch one NSE intraday ORB setup")
    parser.add_argument("--symbol", required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--yahoo", action="store_true")
    source.add_argument("--angelone", action="store_true")
    source.add_argument("--offline")
    parser.add_argument("--credentials-file")
    parser.add_argument("--universe", default=os.path.join("trader", "nse_universe.json"))
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--range", dest="range_name", default="1d")
    parser.add_argument("--range-minutes", type=int, default=None)
    parser.add_argument("--day", default=dt.datetime.now(IST).date().isoformat())
    parser.add_argument("--every", type=float, default=30.0)
    parser.add_argument("--until", help="IST time HH:MM; default NSE close")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--out", default=os.path.join("trader", "intraday_entry.json"))
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        market_day = calendar_status(args.day)
        if not market_day["trading_day"]:
            document = {"market": "NSE", "currency": "INR", "symbol": args.symbol.upper(),
                        "mode": "intraday_watch", "execution_enabled": False,
                        "status": "BLOCKED", "reason": market_day["reason"],
                        "calendar_known": market_day["calendar_known"]}
            if args.json:
                print(json.dumps(document, sort_keys=True))
            else:
                print("BLOCKED  %s" % market_day["reason"])
            return 1
        interval_minutes = int(str(args.interval).rstrip("m"))
        if interval_minutes <= 0:
            raise IntradayError("interval must be a positive minute interval")

        def loader(symbol):
            return load_source(
                symbol, offline=args.offline,
                yahoo=args.yahoo or (not args.offline and not args.angelone),
                angelone=args.angelone, credentials_file=args.credentials_file,
                universe_path=args.universe, interval=args.interval,
                range_name=args.range_name, day=args.day,
            )

        until = dt.datetime.strptime(args.until, "%H:%M").time() if args.until else NSE_CLOSE
        while True:
            now = dt.datetime.now(IST)
            signal = watch_once(args.symbol, loader, args.day, now, interval_minutes,
                                args.range_minutes)
            signal["calendar_known"] = market_day["calendar_known"]
            if args.json:
                print(json.dumps(signal, sort_keys=True))
            else:
                print("%s  %s" % (signal.get("status"), signal.get("reason", "")))
            if signal.get("status") == "HIT" or now.time().replace(tzinfo=None) >= until:
                if args.write:
                    target = Path(args.out)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "w", encoding="utf-8", newline="\n") as handle:
                        json.dump(signal, handle, indent=2, sort_keys=True)
                        handle.write("\n")
                return 0 if signal.get("status") == "HIT" else 1
            time.sleep(min(max(args.every, 1.0), 60.0))
    except (IntradayError, OSError, ValueError, TypeError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
