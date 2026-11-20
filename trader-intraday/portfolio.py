#!/usr/bin/env python3
"""Portfolio-level cash, trade-count and daily-loss gate for paper reports."""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intraday import ABSOLUTE_MAX_TRADE_VALUE_INR


class PortfolioError(Exception):
    """Expected portfolio allocation error."""


def _time(value):
    text = str(value or "").strip()
    if not text:
        return dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        result = dt.datetime.fromisoformat(text)
    except ValueError:
        return dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=dt.timezone.utc)
    return result


def _scale_costs(costs, ratio):
    result = {}
    for key, value in (costs or {}).items():
        try:
            result[key] = round(float(value) * ratio, 2)
        except (TypeError, ValueError):
            result[key] = value
    return result


def allocate_trades(trades, capital=100000.0, max_trades=3,
                    max_daily_loss=3000.0, max_trade_value=10000.0):
    """Apply shared cash and daily limits to independently generated trades.

    This function never sends an order.  It accepts the deterministic trades
    produced by the intraday backtester and returns the trades that could fit
    one paper portfolio in chronological order.
    """
    capital = float(capital)
    if not math.isfinite(capital) or capital <= 0:
        raise PortfolioError("capital must be positive")
    max_trades = int(max_trades)
    if max_trades <= 0:
        raise PortfolioError("max_trades must be positive")
    max_daily_loss = float(max_daily_loss)
    if max_daily_loss <= 0:
        raise PortfolioError("max_daily_loss must be positive")
    trade_cap = min(float(max_trade_value), ABSOLUTE_MAX_TRADE_VALUE_INR)
    if trade_cap <= 0:
        raise PortfolioError("max_trade_value must be positive")

    cash = capital
    realized = 0.0
    selected = []
    rejected = []
    open_positions = []
    ordered = sorted([copy.deepcopy(item) for item in (trades or [])],
                     key=lambda item: (_time(item.get("entry_timestamp")),
                                       str(item.get("symbol", ""))))

    for candidate in ordered:
        entry_time = _time(candidate.get("entry_timestamp"))
        still_open = []
        for position in open_positions:
            if _time(position["exit_timestamp"]) <= entry_time:
                cash += position["exit_value"] - position["costs"]
                realized += position["net_pnl"]
            else:
                still_open.append(position)
        open_positions = still_open

        if len(selected) >= max_trades:
            rejected.append({"symbol": candidate.get("symbol"), "reason": "max trades per day"})
            continue
        if realized <= -max_daily_loss:
            rejected.append({"symbol": candidate.get("symbol"), "reason": "daily loss limit"})
            continue

        original_qty = int(candidate.get("qty") or 0)
        entry = float(candidate.get("entry") or 0)
        exit_price = float(candidate.get("exit") or 0)
        if original_qty <= 0 or entry <= 0 or exit_price <= 0:
            rejected.append({"symbol": candidate.get("symbol"), "reason": "invalid trade"})
            continue
        qty = min(original_qty, int(trade_cap // entry), int(cash // entry))
        if qty <= 0:
            rejected.append({"symbol": candidate.get("symbol"), "reason": "insufficient cash"})
            continue
        ratio = float(qty) / float(original_qty)
        gross = round(float(candidate.get("gross_pnl") or 0.0) * ratio, 2)
        costs = round(float((candidate.get("costs") or {}).get("total") or 0.0) * ratio, 2)
        net = round(gross - costs, 2)
        entry_value = round(entry * qty, 2)
        exit_value = round(exit_price * qty, 2)
        accepted = copy.deepcopy(candidate)
        accepted.update({
            "qty": qty, "entry_value": entry_value, "gross_pnl": gross,
            "costs": _scale_costs(candidate.get("costs"), ratio),
            "net_pnl": net, "portfolio_status": "ACCEPTED",
        })
        selected.append(accepted)
        cash -= entry_value
        open_positions.append({"exit_timestamp": candidate.get("exit_timestamp"),
                               "exit_value": exit_value, "costs": costs,
                               "net_pnl": net})

    for position in open_positions:
        cash += position["exit_value"] - position["costs"]
        realized += position["net_pnl"]

    return {
        "capital_inr": capital,
        "cash_after_inr": round(cash, 2),
        "selected": selected,
        "rejected": rejected,
        "summary": {
            "trades": len(selected),
            "wins": sum(1 for item in selected if float(item.get("net_pnl", 0)) > 0),
            "losses": sum(1 for item in selected if float(item.get("net_pnl", 0)) < 0),
            "net_pnl_inr": round(realized, 2),
            "return_pct": round(100.0 * realized / capital, 4),
            "max_trade_value_inr": round(trade_cap, 2),
        },
        "execution_enabled": False,
        "mode": "paper_portfolio_gate",
    }


def _parser():
    parser = argparse.ArgumentParser(description="Apply NSE paper portfolio limits to trade JSON")
    parser.add_argument("--input", required=True, help="JSON containing a trades list")
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--max-trades", type=int, default=3)
    parser.add_argument("--max-daily-loss", type=float, default=3000.0)
    parser.add_argument("--max-trade-value", type=float, default=10000.0)
    parser.add_argument("--out")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        with open(args.input, encoding="utf-8") as handle:
            document = json.load(handle)
        trades = document.get("trades", []) if isinstance(document, dict) else document
        result = allocate_trades(trades, args.capital, args.max_trades,
                                 args.max_daily_loss, args.max_trade_value)
        if args.out:
            target = Path(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(result, handle, indent=2, sort_keys=True)
                handle.write("\n")
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print("NSE PORTFOLIO GATE  trades %d  net INR %.2f" %
                  (result["summary"]["trades"], result["summary"]["net_pnl_inr"]))
        return 0
    except (OSError, ValueError, TypeError, PortfolioError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
