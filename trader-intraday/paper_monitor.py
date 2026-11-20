#!/usr/bin/env python3
"""Poll open NSE paper positions and update their local journal.

This monitor reads market data and writes simulated exits only.  It never
submits, cancels, or reconciles a broker order.
"""

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

from india_market import IST, NSE_FLAT_BY
from angelone_client import AngelOneClient, AngelOneCredentials, AngelOneError
from intraday import IntradayError, load_angelone_ltp, load_source
import alerts
import paper_journal


def _time(value):
    text = str(value).strip()
    try:
        hour, minute = [int(part) for part in text.split(":", 1)]
        return dt.time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise IntradayError("time must be HH:MM") from exc


def check_once(state_path, day, source="yahoo", offline=None,
               credentials_file=None, universe_path=None, interval="5m",
               range_name="1d", now=None):
    current = now or dt.datetime.now(IST)
    state = paper_journal.load_state(state_path, day, 100000.0)
    results = []
    angel_client = None
    if source == "angelone" and state.get("open_positions"):
        try:
            credentials = (AngelOneCredentials.from_json(credentials_file)
                           if credentials_file else AngelOneCredentials.from_environment())
            angel_client = AngelOneClient(credentials)
        except AngelOneError as exc:
            raise IntradayError("Angel One live price unavailable") from exc
    for position in list(state.get("open_positions", [])):
        symbol = position["symbol"]
        plan = dict(position)
        plan.update({
            "status": "PAPER_SETUP", "signal_date": day,
            "signal_timestamp": position.get("entered_at"),
            "flat_by": NSE_FLAT_BY.strftime("%H:%M"),
        })
        try:
            if source == "angelone":
                quote = load_angelone_ltp(
                    symbol, credentials_file=credentials_file,
                    universe_path=universe_path, client=angel_client)
                last_price = float(quote["price"])
                result = alerts.check_live_price(
                    plan, last_price, day, current.isoformat())
                result.update({
                    "symbol": symbol,
                    "source": "angelone-ltp",
                    "quote_timestamp": quote.get("timestamp") or current.isoformat(),
                    "last_price": last_price,
                    "unrealized_pnl_inr": round(
                        (last_price - float(position["entry"])) * int(position["qty"]), 2),
                })
            else:
                bars, actual_source = load_source(
                    symbol, offline=offline, yahoo=source == "yahoo",
                    credentials_file=credentials_file, universe_path=universe_path,
                    interval=interval, range_name=range_name, day=day,
                )
                result = alerts.check_plan(plan, bars, day, current.isoformat(),
                                           int(str(interval).rstrip("m")))
                # Keep the dashboard and journal view useful between exits: the
                # alert result is event-oriented, while the last completed close
                # provides a deterministic mark-to-market paper value.
                closed = alerts.completed_bars(
                    alerts.session_bars(bars, day), current.isoformat(),
                    int(str(interval).rstrip("m")))
                if closed:
                    last_bar = closed[-1]
                    last_price = float(last_bar["close"])
                    result.update({
                        "last_completed_bar": last_bar["timestamp"],
                        "last_price": last_price,
                        "unrealized_pnl_inr": round(
                            (last_price - float(position["entry"])) * int(position["qty"]), 2),
                    })
                result.update({"symbol": symbol, "source": actual_source})
            if result.get("status") in ("TRIGGERED", "EXPIRED"):
                price = result.get("event_price")
                if price is not None and float(price) > 0:
                    paper_journal.exit_position(
                        state, symbol, price, result.get("event") or "flat",
                        result.get("event_timestamp") or current.isoformat(),
                    )
                    result["journal_action"] = "paper_exit_recorded"
        except (IntradayError, OSError, ValueError, TypeError) as exc:
            result = {"symbol": symbol, "status": "ERROR", "reason": str(exc),
                      "execution_enabled": False}
        results.append(result)
    paper_journal.write_state(state_path, state)
    return {"checked_at": current.isoformat(), "results": results,
            "open_positions": len(state.get("open_positions", [])),
            "realized_pnl_inr": state.get("realized_pnl_inr", 0.0),
            "unrealized_pnl_inr": round(sum(
                float(item.get("unrealized_pnl_inr", 0.0)) for item in results), 2),
            "cash_inr": state.get("cash_inr", 0.0),
            "execution_enabled": False}


def _parser():
    parser = argparse.ArgumentParser(description="Monitor NSE paper positions")
    parser.add_argument("--state", default=os.path.join("trader", "state", "nse_paper.json"))
    parser.add_argument("--day", default=dt.datetime.now(IST).date().isoformat())
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--yahoo", action="store_true", help="use Yahoo 5-minute charts")
    source.add_argument("--angelone", action="store_true", help="use Angel One live LTP")
    source.add_argument("--offline", help="directory containing SYMBOL.csv or SYMBOL.json")
    parser.add_argument("--credentials-file")
    parser.add_argument("--universe", default=os.path.join("trader", "nse_universe.json"))
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--range", dest="range_name", default="1d")
    parser.add_argument("--every", type=float, default=30.0)
    parser.add_argument("--until", default=NSE_FLAT_BY.strftime("%H:%M"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        source = "offline" if args.offline else "angelone" if args.angelone else "yahoo"
        until = _time(args.until)
        while True:
            report = check_once(
                args.state, args.day, source, args.offline, args.credentials_file,
                args.universe, args.interval, args.range_name,
            )
            if args.json:
                print(json.dumps(report, sort_keys=True), flush=True)
            else:
                statuses = ", ".join(
                    "%s:%s" % (item.get("symbol"), item.get("status"))
                    for item in report["results"]
                ) or "no open positions"
                print("NSE PAPER MONITOR  %s  cash INR %.2f  realized INR %.2f  %s" %
                      (report["checked_at"], report["cash_inr"],
                       report["realized_pnl_inr"], statuses), flush=True)
            now = dt.datetime.now(IST)
            if args.once or report["open_positions"] == 0 or now.time().replace(tzinfo=None) >= until:
                return 0
            time.sleep(min(max(args.every, 1.0), 300.0))
    except (IntradayError, OSError, ValueError, TypeError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
