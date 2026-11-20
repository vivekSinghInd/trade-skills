#!/usr/bin/env python3
"""Run the NSE intraday rule over many saved symbols and gate one portfolio."""

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

from india_market import IST
from nse_calendar import calendar_status
from intraday import DEFAULTS, IntradayError, backtest_session, load_csv
from portfolio import allocate_trades


def run_batch(folder, day, symbols=None, capital=100000.0, max_loss=1000.0,
              max_trades=3, max_daily_loss=3000.0):
    market_day = calendar_status(day)
    if not market_day["trading_day"]:
        return {"market": "NSE", "currency": "INR", "mode": "intraday_batch_backtest",
                "day": day, "execution_enabled": False, "status": "BLOCKED",
                "reason": market_day["reason"], "calendar_known": market_day["calendar_known"],
                "symbols": [], "independent_trades": [], "portfolio": None}
    selected = {str(item).upper() for item in (symbols or ()) if str(item).strip()}
    files = sorted(Path(folder).glob("*.csv"))
    all_trades = []
    per_symbol = []
    for path in files:
        symbol = path.stem.upper()
        if selected and symbol not in selected:
            continue
        bars = load_csv(str(path))
        result = backtest_session(bars, symbol, DEFAULTS, capital, max_loss, day)
        per_symbol.append(result)
        all_trades.extend(result.get("trades", []))
    portfolio = allocate_trades(all_trades, capital, max_trades,
                                max_daily_loss, DEFAULTS["max_trade_value_inr"])
    return {
        "market": "NSE", "currency": "INR", "mode": "intraday_batch_backtest",
        "day": day, "folder": str(folder), "execution_enabled": False,
        "symbols": per_symbol, "independent_trades": all_trades,
        "portfolio": portfolio,
    }


def _parser():
    parser = argparse.ArgumentParser(description="NSE multi-symbol intraday backtest")
    parser.add_argument("--csv-dir", required=True)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--day", default=dt.datetime.now(IST).date().isoformat())
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--max-loss", type=float, default=1000.0)
    parser.add_argument("--max-trades", type=int, default=3)
    parser.add_argument("--max-daily-loss", type=float, default=3000.0)
    parser.add_argument("--out")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        document = run_batch(args.csv_dir, args.day, args.symbols, args.capital,
                             args.max_loss, args.max_trades, args.max_daily_loss)
        if args.out:
            target = Path(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
        if args.json:
            print(json.dumps(document, indent=2, sort_keys=True))
        else:
            if document.get("status") == "BLOCKED":
                print("NSE BATCH BACKTEST  BLOCKED: %s" % document["reason"])
            else:
                summary = document["portfolio"]["summary"]
                print("NSE BATCH BACKTEST  symbols %d  trades %d  net INR %.2f" %
                      (len(document["symbols"]), summary["trades"], summary["net_pnl_inr"]))
        return 0
    except (IntradayError, OSError, ValueError, TypeError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
