#!/usr/bin/env python3
"""Replay the NSE intraday rule over saved sessions and bootstrap outcomes.

The historical section uses real saved bars.  The optional Monte Carlo section
resamples observed daily portfolio results with a fixed seed.  It is a
research diagnostic, not a forecast and not a probability of future profit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import statistics
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from india_market import NSE_CLOSE, NSE_FLAT_BY, cash_tick, tick_round
from nse_calendar import calendar_status
from intraday import (DEFAULTS, IntradayError, _parse_timestamp, backtest_session,
                      load_csv, normalize_bars, session_bars, signal_for)
from portfolio import allocate_trades


def _percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * float(percent) / 100.0
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return round(ordered[low] + (ordered[high] - ordered[low]) * fraction, 2)


def _dates(bars):
    return sorted({bar["date"] for bar in normalize_bars(bars)
                   if calendar_status(bar["date"])["trading_day"]})


def _summary(trades, days):
    nets = [float(item.get("net_pnl", 0.0)) for item in trades]
    outcomes = {}
    for item in trades:
        outcome = str(item.get("outcome", "unknown"))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    wins = sum(1 for value in nets if value > 0)
    losses = sum(1 for value in nets if value < 0)
    return {
        "days": len(days), "trades": len(trades), "wins": wins, "losses": losses,
        "win_rate_pct": round(100.0 * wins / len(trades), 2) if trades else 0.0,
        "net_pnl_inr": round(sum(nets), 2),
        "average_trade_net_inr": round(sum(nets) / len(nets), 2) if nets else 0.0,
        "outcomes": outcomes,
    }


def bootstrap_daily(daily_nets, iterations=10000, seed=19):
    """Resample observed daily net results, preserving the observed day count."""
    iterations = int(iterations)
    if iterations <= 0 or not daily_nets:
        return {"iterations": 0, "seed": seed, "note": "no bootstrap requested or no observed days"}
    rng = random.Random(int(seed))
    observed_days = len(daily_nets)
    samples = []
    for _ in range(iterations):
        samples.append(round(sum(rng.choice(daily_nets) for _ in range(observed_days)), 2))
    return {
        "iterations": iterations, "seed": int(seed), "observed_days": observed_days,
        "probability_positive_pct": round(100.0 * sum(value > 0 for value in samples) / iterations, 2),
        "p05_net_pnl_inr": _percentile(samples, 5),
        "p25_net_pnl_inr": _percentile(samples, 25),
        "median_net_pnl_inr": _percentile(samples, 50),
        "p75_net_pnl_inr": _percentile(samples, 75),
        "p95_net_pnl_inr": _percentile(samples, 95),
        "min_net_pnl_inr": min(samples), "max_net_pnl_inr": max(samples),
        "method": "bootstrap observed daily portfolio results with replacement",
        "not_a_forecast": True,
    }


def _clock(value):
    text = str(value or "").strip()
    if not text:
        return NSE_FLAT_BY
    try:
        hour, minute = [int(part) for part in text.split(":", 1)]
        return dt.time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise IntradayError("time must be HH:MM") from exc


def _session_map(bars):
    normalized = normalize_bars(bars)
    dates = sorted({bar["date"] for bar in normalized})
    return {day: session_bars(normalized, day) for day in dates}


def _full_prior_sessions(bars, day, minimum=20):
    sessions = _session_map(bars)
    prior = [(key, rows) for key, rows in sessions.items()
             if key < str(day) and len(rows) >= 70 and calendar_status(key)["trading_day"]]
    if len(prior) < int(minimum):
        raise IntradayError("only %d full NSE sessions before %s; plan replay needs at least %d"
                            % (len(prior), day, minimum))
    return prior


def _entry_index(session, plan):
    reference = plan.get("entry_timestamp") or plan.get("signal_timestamp")
    if not reference:
        raise IntradayError("plan needs signal_timestamp or entry_timestamp")
    stamp = _parse_timestamp(reference)
    candidates = [index for index, bar in enumerate(session)
                  if _parse_timestamp(bar["timestamp"]) >= stamp]
    if not candidates:
        raise IntradayError("plan timestamp is outside the selected NSE session")
    index = candidates[0]
    # intraday.py enters at the next bar open after a signal bar. A plan that
    # already carries entry_timestamp is already anchored to that bar.
    if not plan.get("entry_timestamp"):
        index += 1
    if index >= len(session):
        raise IntradayError("plan entry leaves no completed bars to replay")
    return index


def _plan_prices(session, index, raw_plan):
    try:
        entry = float(raw_plan.get("entry") or session[index]["open"])
        stop = float(raw_plan["stop"])
        target = float(raw_plan["target"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IntradayError("plan needs numeric stop and target, and optional entry") from exc
    if entry <= 0 or stop <= 0 or target <= 0 or stop >= entry or target <= entry:
        raise IntradayError("NSE time machine currently supports stop < entry < target only")
    return entry, stop, target, entry - stop


def _pool(prior, last):
    result = {}
    for _, session in prior:
        for index in range(1, min(last, len(session) - 1) + 1):
            previous = session[index - 1]["close"]
            if previous <= 0:
                continue
            result.setdefault(index, []).append(tuple(
                float(session[index][field]) / previous
                for field in ("open", "high", "low", "close")
            ))
    return result


def _slot(pool, index, rng, window):
    candidates = [key for key in pool if abs(key - index) <= window]
    if not candidates:
        raise IntradayError("historical pool has no completed candle near slot %d" % index)
    return rng.choice(candidates)


def _path_result(entry, stop, target, risk, pool, entry_index, last,
                 replays=10000, seed=19, window=2, keep_paths=20):
    replays = int(replays)
    window = int(window)
    if not 1 <= replays <= 1000000:
        raise IntradayError("replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise IntradayError("window must be between 0 and 10 bars")
    rng = random.Random(int(seed))
    outcomes, closes, rs, paths = [], [], [], []
    for number in range(replays):
        price = entry
        outcome = None
        path = [round(price, 4)]
        for index in range(entry_index, last + 1):
            slot = _slot(pool, index, rng, window)
            opening, high, low, close = rng.choice(pool[slot])
            if min(opening, high, low, close) <= 0:
                continue
            hi, lo, cl = price * high, price * low, price * close
            if lo <= stop:
                outcome = "stop"
                price = min(price * opening, stop)
                break
            if hi >= target:
                outcome = "target"
                price = max(price * opening, target)
                break
            price = cl
            if number < keep_paths:
                path.append(round(price, 4))
        outcomes.append(outcome or "flat")
        closes.append(price)
        rs.append((target - entry) / risk if outcome == "target" else
                  -1.0 if outcome == "stop" else (price - entry) / risk)
        if number < keep_paths:
            paths.append(path)
    counts = {name: outcomes.count(name) for name in ("target", "stop", "flat")}
    sorted_closes = sorted(closes)
    hit_side = counts["target"] + counts["stop"]
    target_share = counts["target"] / hit_side if hit_side else 0.0
    green = sum(1 for outcome, price in zip(outcomes, closes)
                if outcome == "target" or (outcome == "flat" and price > entry))
    return {
        "replays": replays, "seed": int(seed), "window": window,
        "entry_bar_index": entry_index, "bars_left": last - entry_index + 1,
        "target_r": round((target - entry) / risk, 4), "counts": counts,
        "p_target": round(counts["target"] / replays, 4),
        "p_stop": round(counts["stop"] / replays, 4),
        "p_flat": round(counts["flat"] / replays, 4),
        "p_green": round(green / replays, 4),
        "target_share_of_hit_side": round(target_share, 4),
        "median_close": round(_percentile(sorted_closes, 50), 4),
        "p05_close": round(_percentile(sorted_closes, 5), 4),
        "p95_close": round(_percentile(sorted_closes, 95), 4),
        "avg_r": round(statistics.mean(rs), 4), "sample_paths": paths,
        "method": "same-symbol historical candle-return bootstrap by time-of-day",
        "not_a_forecast": True,
    }


def _historical_plan_replay(prior, range_minutes, target_r, flat_by):
    trades = []
    for day, session in prior:
        signal = signal_for(session, range_minutes, day)
        if signal.get("status") != "HIT":
            continue
        entry_index = signal["signal_index"] + 1
        if entry_index >= len(session):
            continue
        entry_bar = session[entry_index]
        entry = entry_bar["open"]
        stop = tick_round(signal["stop_reference"], cash_tick(entry), up=False)
        risk = entry - stop
        if risk <= 0:
            continue
        target = tick_round(entry + target_r * risk, cash_tick(entry), up=True)
        exit_price = session[-1]["close"]
        outcome = "flat"
        for bar in session[entry_index:]:
            if _parse_timestamp(bar["timestamp"]).time().replace(tzinfo=None) >= flat_by:
                break
            if bar["low"] <= stop:
                exit_price, outcome = min(bar["open"], stop), "stop"
                break
            if bar["high"] >= target:
                exit_price, outcome = max(bar["open"], target), "target"
                break
            exit_price = bar["close"]
        trades.append({"day": day, "entry_timestamp": entry_bar["timestamp"],
                       "entry": round(entry, 4), "stop": round(stop, 4),
                       "target": round(target, 4), "exit": round(exit_price, 4),
                       "outcome": outcome, "r_multiple": round((exit_price - entry) / risk, 4)})
    counts = {name: sum(1 for item in trades if item["outcome"] == name)
              for name in ("target", "stop", "flat")}
    return {"sessions": len(prior), "triggered": len(trades), "counts": counts,
            "green": sum(1 for item in trades if item["r_multiple"] > 0),
            "avg_r": round(statistics.mean(item["r_multiple"] for item in trades), 4)
            if trades else None, "trades": trades}


def run_plan_replay(bars, plan, day=None, range_minutes=15, replays=10000,
                    seed=19, window=2, until=None, minimum_sessions=20):
    """Run U.S.-style plan Monte Carlo plus historical ORB replay for NSE."""
    if str(plan.get("side", "buy")).lower() != "buy":
        raise IntradayError("NSE time machine is long-only; short replay is disabled")
    plan_day = day or plan.get("signal_date")
    if plan_day and not calendar_status(plan_day)["trading_day"]:
        raise IntradayError("plan date is not a known NSE trading day: %s" % plan_day)
    session = session_bars(bars, plan_day)
    if not session:
        raise IntradayError("no NSE session bars for the plan date")
    plan_day = plan_day or session[-1]["date"]
    entry_index = _entry_index(session, plan)
    entry, stop, target, risk = _plan_prices(session, entry_index, plan)
    flat_by = _clock(plan.get("flat_by"))
    end_time = _clock(until) if until else flat_by
    if end_time > NSE_CLOSE:
        raise IntradayError("until cannot be after the NSE regular close")
    eligible = [index for index, bar in enumerate(session)
                if _parse_timestamp(bar["timestamp"]).time().replace(tzinfo=None) < end_time]
    if not eligible or max(eligible) < entry_index:
        raise IntradayError("until leaves no bars after the plan entry")
    last = max(eligible)
    prior = _full_prior_sessions(bars, plan_day, minimum_sessions)
    pool = _pool(prior, last)
    mc = _path_result(entry, stop, target, risk, pool, entry_index, last,
                      replays, seed, window)
    history = _historical_plan_replay(prior, range_minutes,
                                      (target - entry) / risk, flat_by)
    share = mc["target_share_of_hit_side"]
    verdict = ("target-lean" if share >= 0.55 else
               "stop-lean" if share <= 0.45 else "coin-flip")
    return {
        "market": "NSE", "currency": "INR", "mode": "nse_intraday_time_machine",
        "execution_enabled": False, "plan": dict(plan, entry=entry, stop=stop, target=target),
        "data": {"day": plan_day, "prior_sessions": len(prior),
                 "until": end_time.strftime("%H:%M"), "range_minutes": range_minutes},
        "mc": mc, "history": history, "verdict": verdict,
        "caveats": [
            "This is a same-symbol historical return simulation, not a prediction.",
            "A new headline, halt, gap, spread or regime can invalidate the historical pool.",
            "Same-bar stop and target ambiguity is resolved conservatively in favor of the stop.",
        ],
    }


def run_replay(folder, symbols=None, capital=100000.0, max_loss=1000.0,
               max_trades=3, max_daily_loss=3000.0, iterations=10000, seed=19,
               config=None):
    selected = {str(item).upper() for item in (symbols or ()) if str(item).strip()}
    files = sorted(Path(folder).glob("*.csv"))
    if not files:
        raise IntradayError("no CSV files found in %s" % folder)
    cfg = dict(DEFAULTS)
    cfg.update(config or {})
    by_day = {}
    per_symbol = []
    for path in files:
        symbol = path.stem.upper()
        if selected and symbol not in selected:
            continue
        bars = load_csv(str(path))
        symbol_days = []
        for day in _dates(bars):
            result = backtest_session(bars, symbol, cfg, capital, max_loss, day)
            symbol_days.append(result)
            by_day.setdefault(day, []).extend(result.get("trades", []))
        per_symbol.append({"symbol": symbol, "days": symbol_days})
    if selected and not per_symbol:
        raise IntradayError("none of the requested symbols has a CSV file")

    daily = []
    selected_trades = []
    for day in sorted(by_day):
        gate = allocate_trades(by_day[day], capital, max_trades, max_daily_loss,
                               cfg["max_trade_value_inr"])
        daily.append({"day": day, "candidate_trades": len(by_day[day]),
                      "portfolio": gate})
        selected_trades.extend(gate["selected"])
    daily_nets = [float(item["portfolio"]["summary"]["net_pnl_inr"]) for item in daily]
    historical = _summary(selected_trades, daily)
    return {
        "market": "NSE", "currency": "INR", "mode": "intraday_replay_research",
        "execution_enabled": False, "folder": str(folder), "config": cfg,
        "symbols": per_symbol, "daily": daily, "historical": historical,
        "bootstrap": bootstrap_daily(daily_nets, iterations, seed),
        "caveats": [
            "Historical results are not predictions or investment advice.",
            "Bootstrap samples observed daily results; it does not model future news, gaps, halts or regime change.",
            "OHLCV bars cannot model queue priority, exact fills or live data delays.",
            "Run the portfolio-gated results, not the independent per-symbol totals, when comparing capital usage.",
        ],
    }


def _parser():
    parser = argparse.ArgumentParser(description="NSE multi-day intraday replay research")
    parser.add_argument("--csv-dir", help="folder of SYMBOL.csv files for multi-day replay")
    parser.add_argument("--csv", help="one symbol CSV for plan-level time machine")
    parser.add_argument("--plan", help="paper plan JSON for U.S.-style plan replay")
    parser.add_argument("--symbol", help="symbol when using --csv-dir or inline plan flags")
    parser.add_argument("--day", help="plan session date YYYY-MM-DD")
    parser.add_argument("--entry", type=float)
    parser.add_argument("--stop", type=float)
    parser.add_argument("--target", type=float)
    parser.add_argument("--entry-timestamp", help="entry bar timestamp; defaults to signal timestamp")
    parser.add_argument("--signal-timestamp", help="signal bar timestamp for next-bar entry")
    parser.add_argument("--flat-by", default="15:15")
    parser.add_argument("--range-minutes", type=int, default=15)
    parser.add_argument("--replays", type=int, default=10000)
    parser.add_argument("--window", type=int, default=2)
    parser.add_argument("--until")
    parser.add_argument("--minimum-sessions", type=int, default=20)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--max-loss", type=float, default=1000.0)
    parser.add_argument("--max-trades", type=int, default=3)
    parser.add_argument("--max-daily-loss", type=float, default=3000.0)
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=19)
    parser.add_argument("--out")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.plan or args.csv or args.entry is not None:
            if args.plan:
                with open(args.plan, encoding="utf-8") as handle:
                    plan = json.load(handle)
                if not isinstance(plan, dict):
                    raise IntradayError("plan must be a JSON object")
            else:
                if not all(value is not None for value in (args.symbol, args.day,
                                                            args.entry, args.stop, args.target)):
                    raise IntradayError("inline plan replay needs --symbol, --day, --entry, --stop and --target")
                plan = {"symbol": args.symbol.upper(), "signal_date": args.day,
                        "entry": args.entry, "stop": args.stop, "target": args.target,
                        "flat_by": args.flat_by}
                if args.entry_timestamp:
                    plan["entry_timestamp"] = args.entry_timestamp
                elif args.signal_timestamp:
                    plan["signal_timestamp"] = args.signal_timestamp
                else:
                    raise IntradayError("inline plan replay needs --entry-timestamp or --signal-timestamp")
            symbol = str(args.symbol or plan.get("symbol") or "").upper()
            if not symbol:
                raise IntradayError("plan or --symbol must provide a symbol")
            csv_path = args.csv
            if not csv_path and args.csv_dir:
                csv_path = str(Path(args.csv_dir) / (symbol + ".csv"))
            if not csv_path:
                raise IntradayError("plan replay needs --csv or --csv-dir")
            bars = load_csv(csv_path)
            document = run_plan_replay(
                bars, plan, args.day, args.range_minutes, args.replays, args.seed,
                args.window, args.until, args.minimum_sessions,
            )
        else:
            if not args.csv_dir:
                raise IntradayError("multi-day replay needs --csv-dir")
            document = run_replay(args.csv_dir, args.symbols, args.capital, args.max_loss,
                                  args.max_trades, args.max_daily_loss, args.iterations,
                                  args.seed)
        if args.out:
            target = Path(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
        if args.json:
            print(json.dumps(document, indent=2, sort_keys=True))
        elif document.get("mode") == "nse_intraday_time_machine":
            print("NSE TIME MACHINE  %s  verdict %s  target %.2f%%  stop %.2f%%" %
                  (document["data"]["day"], document["verdict"],
                   document["mc"]["p_target"] * 100.0,
                   document["mc"]["p_stop"] * 100.0))
        else:
            summary = document["historical"]
            print("NSE REPLAY  days %d  trades %d  net INR %.2f" %
                  (summary["days"], summary["trades"], summary["net_pnl_inr"]))
            mc = document["bootstrap"]
            if mc.get("iterations"):
                print("  bootstrap positive %.2f%%  median INR %.2f" %
                      (mc["probability_positive_pct"], mc["median_net_pnl_inr"]))
        return 0
    except (IntradayError, OSError, ValueError, TypeError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
