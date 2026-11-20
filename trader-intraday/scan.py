#!/usr/bin/env python3
"""Batch-scan an NSE universe for the intraday opening-range setup."""

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

from india_market import IST
from nse_calendar import calendar_status
from intraday import IntradayError, _parse_timestamp, load_source, session_bars, signal_for


def universe_symbols(path, symbols=None, limit=None):
    wanted = [str(item).strip().upper() for item in (symbols or ()) if str(item).strip()]
    if wanted:
        result = wanted
    else:
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as exc:
            raise IntradayError("cannot read NSE universe") from exc
        result = []
        for row in document.get("instruments", []) if isinstance(document, dict) else []:
            symbol = str(row.get("base_symbol") or "").upper()
            if symbol and symbol not in result:
                result.append(symbol)
    return result[:limit] if limit else result


def completed_session_bars(bars, day, now=None, interval_minutes=5):
    current = now or dt.datetime.now(IST)
    result = []
    for bar in session_bars(bars, day):
        stamp = _parse_timestamp(bar["timestamp"])
        if stamp + dt.timedelta(minutes=interval_minutes) <= current:
            result.append(bar)
    return result


def scan_symbols(symbols, loader, day, min_price=0.0, min_volume=0.0, pause=0.0,
                 interval_minutes=5, now=None):
    hits = []
    no_setup = []
    failed = []
    for index, symbol in enumerate(symbols):
        try:
            bars, source = loader(symbol)
            bars_for_day = completed_session_bars(bars, day, now, interval_minutes)
            if not bars_for_day:
                no_setup.append({"symbol": symbol, "status": "INSUFFICIENT_DATA"})
            else:
                last_close = bars_for_day[-1]["close"]
                average_volume = sum(bar["volume"] for bar in bars_for_day) / len(bars_for_day)
                if last_close < min_price or average_volume < min_volume:
                    no_setup.append({"symbol": symbol, "status": "FILTERED",
                                     "close": last_close, "average_volume": average_volume})
                else:
                    # The signal must be calculated from the same completed-bar
                    # set used by the gate above.  Passing the raw provider
                    # response here could let a currently forming candle
                    # create a false breakout.
                    signal = signal_for(bars_for_day, day=day)
                    signal.update({"symbol": symbol, "source": source})
                    if signal.get("status") == "HIT":
                        hits.append(signal)
                    else:
                        no_setup.append(signal)
        except (IntradayError, OSError, ValueError, TypeError) as exc:
            failed.append({"symbol": symbol, "error": str(exc)})
        if pause and index + 1 < len(symbols):
            time.sleep(max(0.0, pause))
    hits.sort(key=lambda item: item.get("signal_timestamp", ""))
    return {"hits": hits, "no_setup": no_setup, "failed": failed}


def _write(path, document):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return str(target)


def _parser():
    parser = argparse.ArgumentParser(description="NSE intraday universe scanner")
    parser.add_argument("--universe", default=os.path.join("trader", "nse_universe.json"))
    parser.add_argument("--symbols", nargs="*", help="override the universe with explicit symbols")
    parser.add_argument("--limit", type=int, help="scan only the first N universe symbols")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--yahoo", action="store_true", help="use Yahoo 5-minute charts")
    source.add_argument("--angelone", action="store_true", help="use Angel One historical candles")
    source.add_argument("--offline", help="directory containing SYMBOL.csv or SYMBOL.json")
    parser.add_argument("--credentials-file")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--range", dest="range_name", default="1d")
    parser.add_argument("--day", default=dt.datetime.now(IST).date().isoformat())
    parser.add_argument("--min-price", type=float, default=0.0)
    parser.add_argument("--min-volume", type=float, default=0.0)
    parser.add_argument("--pause", type=float, default=0.2)
    parser.add_argument("--out", default=os.path.join("trader", "intraday_watchlist.json"))
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        market_day = calendar_status(args.day)
        if not market_day["trading_day"]:
            document = {"market": "NSE", "currency": "INR", "mode": "intraday_universe_scan",
                        "execution_enabled": False, "day": args.day,
                        "status": "BLOCKED", "reason": market_day["reason"],
                        "calendar_known": market_day["calendar_known"], "hits": [],
                        "no_setup": [], "failed": []}
            if args.json:
                print(json.dumps(document, indent=2, sort_keys=True))
            else:
                print("NSE INTRADAY SCAN  %s  BLOCKED: %s" % (args.day, market_day["reason"]))
            return 1
        symbols = universe_symbols(args.universe, args.symbols, args.limit)
        if not symbols:
            raise IntradayError("NSE universe has no symbols to scan")

        def loader(symbol):
            return load_source(
                symbol,
                offline=args.offline,
                yahoo=args.yahoo or (not args.offline and not args.angelone),
                angelone=args.angelone,
                credentials_file=args.credentials_file,
                universe_path=args.universe,
                interval=args.interval,
                range_name=args.range_name,
                day=args.day,
            )

        interval_minutes = int(str(args.interval).rstrip("m"))
        if interval_minutes <= 0:
            raise IntradayError("interval must be a positive minute interval")
        result = scan_symbols(symbols, loader, args.day, args.min_price,
                              args.min_volume, args.pause, interval_minutes)
        document = {
            "market": "NSE", "currency": "INR", "mode": "intraday_universe_scan",
            "execution_enabled": False, "day": args.day,
            "calendar_known": market_day["calendar_known"],
            "universe": args.universe, "universe_count": len(symbols),
            "scanned": len(symbols), "hits": result["hits"],
            "no_setup": result["no_setup"], "failed": result["failed"],
        }
        if args.write:
            print("Wrote %s" % _write(args.out, document))
        if args.json:
            print(json.dumps(document, indent=2, sort_keys=True))
        else:
            print("NSE INTRADAY SCAN  %s  scanned %d  hits %d  failed %d" %
                  (args.day, len(symbols), len(result["hits"]), len(result["failed"])))
            for hit in result["hits"]:
                print("  %s  %s  close %.2f" %
                      (hit["symbol"], hit["signal_timestamp"], hit["close"]))
        return 0 if result["hits"] else 1
    except (IntradayError, OSError, ValueError, TypeError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
