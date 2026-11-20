#!/usr/bin/env python3
"""Persistent paper-entry/exit journal and daily risk gate for NSE.

This file records simulated fills only.  It is intentionally not connected to
Angel One and cannot submit, cancel or reconcile a live order.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path


class JournalError(Exception):
    """Expected paper-journal error."""


def _positive(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise JournalError("%s must be numeric" % name) from exc
    if not math.isfinite(result) or result <= 0:
        raise JournalError("%s must be positive" % name)
    return result


def new_state(day, capital):
    capital = _positive(capital, "capital")
    return {"market": "NSE", "currency": "INR", "mode": "paper_journal",
            "execution_enabled": False, "day": str(day),
            "starting_capital_inr": capital, "cash_inr": capital,
            "realized_pnl_inr": 0.0, "open_positions": [], "trades": []}


def load_state(path, day=None, capital=100000.0):
    target = Path(path)
    if not target.is_file():
        if day is None:
            day = dt.datetime.now().date().isoformat()
        return new_state(day, capital)
    try:
        with open(target, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError) as exc:
        raise JournalError("cannot read paper journal") from exc
    if not isinstance(state, dict) or state.get("mode") != "paper_journal":
        raise JournalError("paper journal has an invalid shape")
    state.setdefault("open_positions", [])
    state.setdefault("trades", [])
    state.setdefault("realized_pnl_inr", 0.0)
    if day and str(state.get("day")) != str(day):
        # A new trading day resets daily limits but carries forward cash.
        cash = float(state.get("cash_inr", state.get("starting_capital_inr", capital)))
        fresh = new_state(day, cash)
        fresh["prior_day"] = state.get("day")
        return fresh
    return state


def write_state(path, state):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(target) + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, target)
    return str(target)


def _limits(max_loss, max_trades, max_daily_loss, max_trade_value):
    max_loss = _positive(max_loss, "max_loss")
    max_daily_loss = _positive(max_daily_loss, "max_daily_loss")
    max_trade_value = _positive(max_trade_value, "max_trade_value")
    if max_trade_value > 10000.0:
        raise JournalError("max_trade_value cannot exceed INR 10000")
    max_trades = int(max_trades)
    if max_trades <= 0:
        raise JournalError("max_trades must be positive")
    return max_loss, max_trades, max_daily_loss, max_trade_value


def open_risk_inr(state):
    """Return the stop-loss risk of all currently open paper positions."""
    return round(sum(
        max(0.0, (float(item.get("entry", 0.0)) - float(item.get("stop", 0.0)))
                 * int(item.get("qty", 0)))
        for item in state.get("open_positions", [])
    ), 2)


def enter(state, plan, max_loss=1000.0, max_trades=3, max_daily_loss=3000.0,
          max_trade_value=10000.0, timestamp=None, max_open_positions=None,
          max_open_risk=None):
    max_loss, max_trades, max_daily_loss, max_trade_value = _limits(
        max_loss, max_trades, max_daily_loss, max_trade_value)
    if str(plan.get("status", "PAPER_SETUP")) != "PAPER_SETUP":
        raise JournalError("plan is not a PAPER_SETUP")
    symbol = str(plan.get("symbol") or "").upper()
    if not symbol:
        raise JournalError("plan needs a symbol")
    try:
        entry = _positive(plan["entry"], "entry")
        stop = _positive(plan["stop"], "stop")
        target = _positive(plan["target"], "target")
        qty = int(plan["qty"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JournalError("plan needs entry, stop, target and whole-share qty") from exc
    if qty <= 0 or stop >= entry or target <= entry:
        raise JournalError("invalid long-only paper plan")
    if any(item.get("symbol") == symbol for item in state["open_positions"]):
        raise JournalError("position already open for %s" % symbol)
    if max_open_positions is not None:
        max_open_positions = int(max_open_positions)
        if max_open_positions <= 0:
            raise JournalError("max_open_positions must be positive")
        if len(state["open_positions"]) >= max_open_positions:
            raise JournalError("max open positions reached")
    if len(state["trades"]) + len(state["open_positions"]) >= max_trades:
        raise JournalError("max trades per day reached")
    if float(state.get("realized_pnl_inr", 0.0)) <= -max_daily_loss:
        raise JournalError("daily loss limit reached")
    notional = round(entry * qty, 2)
    if notional > max_trade_value:
        raise JournalError("trade notional INR %.2f exceeds cap INR %.2f" % (notional, max_trade_value))
    risk = (entry - stop) * qty
    planned = float(plan.get("planned_loss_inr") or risk)
    if planned > max_loss:
        raise JournalError("planned loss INR %.2f exceeds max loss INR %.2f" % (planned, max_loss))
    if max_open_risk is not None:
        max_open_risk = _positive(max_open_risk, "max_open_risk")
        projected_risk = open_risk_inr(state) + risk
        if projected_risk > max_open_risk + 1e-9:
            raise JournalError(
                "open stop-risk INR %.2f exceeds cap INR %.2f" %
                (projected_risk, max_open_risk)
            )
    if float(state.get("cash_inr", 0.0)) < notional:
        raise JournalError("insufficient paper cash")
    state["cash_inr"] = round(float(state["cash_inr"]) - notional, 2)
    state["open_positions"].append({
        "symbol": symbol, "entry": entry, "stop": stop, "target": target,
        "qty": qty, "entry_value": notional, "risk_inr": round(risk, 2),
        "entered_at": timestamp or plan.get("signal_timestamp"),
    })
    return state


def exit_position(state, symbol, exit_price, outcome="manual", timestamp=None):
    symbol = str(symbol).upper()
    matches = [item for item in state["open_positions"] if item.get("symbol") == symbol]
    if not matches:
        raise JournalError("no open paper position for %s" % symbol)
    position = matches[0]
    exit_price = _positive(exit_price, "exit_price")
    state["open_positions"].remove(position)
    exit_value = round(exit_price * int(position["qty"]), 2)
    gross = round((exit_price - float(position["entry"])) * int(position["qty"]), 2)
    state["cash_inr"] = round(float(state["cash_inr"]) + exit_value, 2)
    state["realized_pnl_inr"] = round(float(state.get("realized_pnl_inr", 0.0)) + gross, 2)
    state["trades"].append({"symbol": symbol, "entry": position["entry"],
                            "exit": exit_price, "qty": position["qty"],
                            "gross_pnl_inr": gross, "net_pnl_inr": gross,
                            "outcome": outcome, "entered_at": position.get("entered_at"),
                            "exited_at": timestamp})
    return state


def _parser():
    parser = argparse.ArgumentParser(description="NSE persistent paper journal")
    sub = parser.add_subparsers(dest="command")
    status = sub.add_parser("status")
    status.add_argument("--state", default=os.path.join("trader", "state", "nse_paper.json"))
    status.add_argument("--day")
    status.add_argument("--capital", type=float, default=100000.0)
    status.add_argument("--json", action="store_true")
    enter_cmd = sub.add_parser("enter")
    enter_cmd.add_argument("--state", default=os.path.join("trader", "state", "nse_paper.json"))
    enter_cmd.add_argument("--plan", required=True)
    enter_cmd.add_argument("--day")
    enter_cmd.add_argument("--capital", type=float, default=100000.0)
    enter_cmd.add_argument("--max-loss", type=float, default=1000.0)
    enter_cmd.add_argument("--max-trades", type=int, default=3)
    enter_cmd.add_argument("--max-daily-loss", type=float, default=3000.0)
    enter_cmd.add_argument("--max-trade-value", type=float, default=10000.0)
    enter_cmd.add_argument("--max-open-positions", type=int)
    enter_cmd.add_argument("--max-open-risk", type=float)
    exit_cmd = sub.add_parser("exit")
    exit_cmd.add_argument("--state", default=os.path.join("trader", "state", "nse_paper.json"))
    exit_cmd.add_argument("--symbol", required=True)
    exit_cmd.add_argument("--price", required=True, type=float)
    exit_cmd.add_argument("--outcome", default="manual")
    exit_cmd.add_argument("--timestamp")
    exit_cmd.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            state = load_state(args.state, args.day, args.capital)
        elif args.command == "enter":
            with open(args.plan, encoding="utf-8") as handle:
                plan = json.load(handle)
            state = load_state(args.state, args.day or plan.get("signal_date"), args.capital)
            enter(state, plan, args.max_loss, args.max_trades, args.max_daily_loss,
                  args.max_trade_value, max_open_positions=args.max_open_positions,
                  max_open_risk=args.max_open_risk)
            write_state(args.state, state)
        elif args.command == "exit":
            state = load_state(args.state)
            exit_position(state, args.symbol, args.price, args.outcome, args.timestamp)
            write_state(args.state, state)
        else:
            _parser().print_help()
            return 0
        print(json.dumps(state, indent=2, sort_keys=True) if getattr(args, "json", False)
              else "NSE PAPER JOURNAL  %s  cash INR %.2f  realized INR %.2f  open %d" %
              (state["day"], state["cash_inr"], state["realized_pnl_inr"],
               len(state["open_positions"])))
        return 0
    except (OSError, ValueError, TypeError, JournalError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
