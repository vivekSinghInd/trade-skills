#!/usr/bin/env python3
"""Indian NSE intraday opening-range research and paper planning.

The module consumes completed intraday OHLCV bars from CSV or an offline
fixture. It does not place orders. The default example is a long-only opening
range breakout with a midpoint stop and configurable INR sizing.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from india_market import IST, NSE_CLOSE, NSE_FLAT_BY, NSE_OPEN, cash_tick
from india_market import nse_symbol, session_status, tick_round
from nse_calendar import calendar_status
from angelone_client import AngelOneClient, AngelOneCredentials, AngelOneError
from angelone_client import default_day_window


DEFAULTS = {
    "range_minutes": 15,
    "target_r": 2.0,
    "max_position_pct": 75.0,
    "max_trade_value_inr": 10000.0,
    "slippage_bps": 10.0,
    "brokerage_per_order_inr": 20.0,
    "transaction_bps": 1.0,
    "stt_sell_bps": 2.5,
    "gst_bps": 1800.0,
}
ABSOLUTE_MAX_TRADE_VALUE_INR = 10000.0


class IntradayError(Exception):
    """Expected user or data error."""


def _number(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise IntradayError("%s must be numeric" % name)
    if not math.isfinite(result):
        raise IntradayError("%s must be finite" % name)
    return result


def _positive(value, name):
    result = _number(value, name)
    if result <= 0:
        raise IntradayError("%s must be positive" % name)
    return result


def _row_value(row, names):
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for name in names:
        if name in lowered and str(lowered[name]).strip() != "":
            return lowered[name]
    return None


def _parse_timestamp(value):
    text = str(value or "").strip()
    if not text:
        raise IntradayError("bar timestamp is required")
    try:
        numeric = float(text)
        if numeric > 100000000:
            return dt.datetime.fromtimestamp(numeric, dt.timezone.utc).astimezone(IST)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise IntradayError("cannot parse bar timestamp %r" % value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _make_bar(stamp, row):
    values = {}
    for field in ("open", "high", "low", "close"):
        raw = _row_value(row, (field, field[0]))
        if raw is None:
            raise IntradayError("CSV bar is missing %s" % field)
        values[field] = _number(raw, field)
    raw_volume = _row_value(row, ("volume", "vol"))
    values["volume"] = _number(raw_volume or 0, "volume")
    if values["low"] > values["high"]:
        raise IntradayError("bar low is above high at %s" % stamp.isoformat())
    if not values["low"] <= values["open"] <= values["high"]:
        raise IntradayError("bar open is outside its range at %s" % stamp.isoformat())
    if not values["low"] <= values["close"] <= values["high"]:
        raise IntradayError("bar close is outside its range at %s" % stamp.isoformat())
    values["timestamp"] = stamp.isoformat()
    values["date"] = stamp.date().isoformat()
    return values


def normalize_bars(bars):
    """Sort and deduplicate timezone-aware intraday bars."""
    result = {}
    for bar in bars:
        if not isinstance(bar, dict):
            raise IntradayError("each bar must be an object")
        stamp = _parse_timestamp(bar.get("timestamp") or bar.get("datetime"))
        result[stamp.isoformat()] = _make_bar(stamp, bar)
    return [result[key] for key in sorted(result)]


def load_csv(path):
    """Load timestamped OHLCV bars from CSV."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise IntradayError("CSV has no header: %s" % path)
        bars = []
        for row in reader:
            stamp = _row_value(row, ("timestamp", "datetime", "date", "time"))
            if stamp is None:
                raise IntradayError("CSV needs timestamp or datetime column")
            bars.append(_make_bar(_parse_timestamp(stamp), row))
    result = normalize_bars(bars)
    if not result:
        raise IntradayError("CSV contains no bars: %s" % path)
    return result


def _yahoo_url(symbol, range_name="1d", interval="5m", host="query1.finance.yahoo.com"):
    if interval not in ("1m", "2m", "5m", "15m", "30m", "60m", "90m"):
        raise IntradayError("unsupported Yahoo interval: %s" % interval)
    query = urllib.parse.urlencode({"range": range_name, "interval": interval,
                                    "events": "history", "includePrePost": "false"})
    return "https://%s/v8/finance/chart/%s?%s" % (host, nse_symbol(symbol), query)


def _yahoo_get(symbol, range_name="1d", interval="5m"):
    last_error = None
    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        request = urllib.request.Request(
            _yahoo_url(symbol, range_name, interval, host),
            headers={"User-Agent": "Mozilla/5.0 day-trader-skill-india/1.0",
                     "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except (OSError, urllib.error.URLError, ValueError) as exc:
            last_error = exc
    raise IntradayError("Yahoo intraday data unavailable for %s" % symbol) from last_error


def parse_yahoo_chart(document, symbol):
    """Convert Yahoo chart JSON into normalized IST bars."""
    try:
        chart = document["chart"]
        error = chart.get("error")
        result = (chart.get("result") or [])[0]
    except (KeyError, IndexError, TypeError):
        raise IntradayError("Yahoo returned no chart data for %s" % symbol)
    if error or not result:
        raise IntradayError("Yahoo returned no chart data for %s" % symbol)
    timestamps = result.get("timestamp") or []
    quotes = (result.get("indicators") or {}).get("quote") or []
    quote = quotes[0] if quotes else {}
    bars = []
    for index, timestamp in enumerate(timestamps):
        row = {}
        for field in ("open", "high", "low", "close", "volume"):
            values = quote.get(field) or []
            if index < len(values):
                row[field] = values[index]
        if any(row.get(field) is None for field in ("open", "high", "low", "close")):
            continue
        stamp = _parse_timestamp(timestamp)
        bars.append(_make_bar(stamp, row))
    normalized = normalize_bars(bars)
    if not normalized:
        raise IntradayError("Yahoo returned no completed bars for %s" % symbol)
    return normalized


def load_yahoo(symbol, range_name="1d", interval="5m"):
    return parse_yahoo_chart(_yahoo_get(symbol, range_name, interval), symbol)


def _universe_instrument(path, symbol):
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise IntradayError("cannot read NSE universe: %s" % path) from exc
    instruments = document.get("instruments") if isinstance(document, dict) else None
    wanted = str(symbol).upper().replace(".NS", "").replace(".BO", "")
    for instrument in instruments or []:
        base = str(instrument.get("base_symbol", "")).upper()
        trading = str(instrument.get("trading_symbol", "")).upper()
        if base == wanted or trading in (wanted, wanted + "-EQ"):
            if not instrument.get("token"):
                break
            return instrument
    raise IntradayError("%s is not present in the NSE universe" % symbol)


def parse_angel_candles(rows, symbol):
    """Convert SmartAPI candle arrays or objects into normalized IST bars."""
    bars = []
    for row in rows or []:
        if isinstance(row, (list, tuple)) and len(row) >= 6:
            stamp, opening, high, low, close, volume = row[:6]
            values = {"open": opening, "high": high, "low": low,
                      "close": close, "volume": volume}
        elif isinstance(row, dict):
            stamp = _row_value(row, ("timestamp", "datetime", "time", "date"))
            values = {field: _row_value(row, (field, field[0]))
                      for field in ("open", "high", "low", "close", "volume")}
        else:
            continue
        if stamp is None:
            continue
        bars.append(_make_bar(_parse_timestamp(stamp), values))
    normalized = normalize_bars(bars)
    if not normalized:
        raise IntradayError("Angel One returned no completed bars for %s" % symbol)
    return normalized


def parse_angel_ltp(document, symbol):
    """Normalize a SmartAPI LTP response for paper monitoring."""
    if not isinstance(document, dict):
        raise IntradayError("Angel One returned no quote data for %s" % symbol)
    raw_ltp = _row_value(document, ("ltp", "lasttradedprice", "last_traded_price"))
    try:
        ltp = _positive(raw_ltp, "Angel One LTP")
    except IntradayError as exc:
        raise IntradayError("Angel One returned no valid LTP for %s" % symbol) from exc
    quote = {"symbol": str(symbol).upper(), "price": ltp, "ltp": ltp,
             "source": "angelone-ltp"}
    for field in ("open", "high", "low", "close"):
        raw = _row_value(document, (field, field[0]))
        if raw is None:
            continue
        try:
            quote[field] = _positive(raw, "Angel One %s" % field)
        except IntradayError:
            # OHLC reference fields are optional for this endpoint; LTP is not.
            continue
    stamp = _row_value(document, ("timestamp", "time", "datetime", "exchange_timestamp"))
    if stamp:
        quote["timestamp"] = stamp
    return quote


def load_angelone(symbol, credentials_file=None, universe_path=None, interval="5m",
                  day=None, fromdate=None, todate=None):
    universe = universe_path or str(ROOT / "trader" / "nse_universe.json")
    instrument = _universe_instrument(universe, symbol)
    try:
        credentials = (AngelOneCredentials.from_json(credentials_file)
                       if credentials_file else AngelOneCredentials.from_environment())
        client = AngelOneClient(credentials)
        default_from, default_to = default_day_window(day)
        rows = client.historical_candles(
            "NSE", instrument["token"], interval,
            fromdate or default_from, todate or default_to,
        )
    except AngelOneError as exc:
        raise IntradayError("Angel One historical data unavailable for %s" % symbol) from exc
    return parse_angel_candles(rows, symbol)


def load_angelone_ltp(symbol, credentials_file=None, universe_path=None, client=None):
    """Read the current Angel One LTP for an NSE universe instrument.

    ``client`` is optional so a monitor can reuse one authenticated session
    for all open positions in a polling cycle.
    """
    universe = universe_path or str(ROOT / "trader" / "nse_universe.json")
    instrument = _universe_instrument(universe, symbol)
    try:
        if client is None:
            credentials = (AngelOneCredentials.from_json(credentials_file)
                           if credentials_file else AngelOneCredentials.from_environment())
            client = AngelOneClient(credentials)
        trading_symbol = instrument.get("trading_symbol") or (str(symbol).upper() + "-EQ")
        document = client.ltp_data("NSE", trading_symbol, instrument["token"])
    except AngelOneError as exc:
        raise IntradayError("Angel One live price unavailable for %s" % symbol) from exc
    return parse_angel_ltp(document, symbol)


def find_offline_file(directory, symbol):
    folder = Path(directory)
    plain = str(symbol).upper()
    for name in (plain + ".csv", nse_symbol(plain) + ".csv", plain + ".json"):
        path = folder / name
        if path.is_file():
            return path
    raise IntradayError("no offline CSV or JSON found for %s in %s" % (symbol, directory))


def load_source(symbol, csv_path=None, offline=None, yahoo=False, angelone=False,
                credentials_file=None, universe_path=None, interval="5m",
                range_name="1d", day=None, fromdate=None, todate=None):
    selected = sum(bool(value) for value in (csv_path, offline, yahoo, angelone))
    if selected > 1:
        raise IntradayError("choose only one intraday data source")
    if csv_path:
        return load_csv(csv_path), "csv"
    if offline:
        path = find_offline_file(offline, symbol)
        if path.suffix.lower() == ".csv":
            return load_csv(str(path)), "offline-csv"
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
            if not isinstance(document, list):
                raise IntradayError("offline intraday JSON must be a list of bars")
            return normalize_bars(document), "offline-json"
        except (OSError, ValueError, TypeError) as exc:
            raise IntradayError("cannot read offline data for %s: %s" % (symbol, exc))
    if yahoo:
        return load_yahoo(symbol, range_name, interval), "yahoo-chart"
    if angelone:
        return load_angelone(symbol, credentials_file, universe_path, interval,
                             day, fromdate, todate), "angelone-historical"
    raise IntradayError("intraday research requires --csv, --offline, --yahoo or --angelone")


def _config(values=None):
    result = dict(DEFAULTS)
    if values:
        result.update(values)
    return result


def session_bars(bars, day=None):
    """Return regular-session bars for one IST trading date."""
    normalized = normalize_bars(bars)
    if day is None:
        days = sorted({bar["date"] for bar in normalized})
        if not days:
            return []
        day = days[-1]
    wanted = str(day)
    result = []
    for bar in normalized:
        stamp = _parse_timestamp(bar["timestamp"])
        if bar["date"] == wanted and NSE_OPEN <= stamp.time().replace(tzinfo=None) < NSE_CLOSE:
            result.append(bar)
    return result


def latest_expected_completed_start(day, now=None, interval_minutes=5):
    """Return the latest NSE bar start that should be complete by ``now``."""
    current = now or dt.datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    current = current.astimezone(IST)
    if str(day) != current.date().isoformat():
        return None
    interval_seconds = int(interval_minutes) * 60
    if interval_seconds <= 0:
        raise IntradayError("interval_minutes must be positive")
    session_open = dt.datetime.combine(current.date(), NSE_OPEN).replace(tzinfo=IST)
    session_close = dt.datetime.combine(current.date(), NSE_CLOSE).replace(tzinfo=IST)
    elapsed = (current - session_open).total_seconds()
    if elapsed < interval_seconds:
        return None
    completed_count = int(elapsed // interval_seconds)
    latest = session_open + dt.timedelta(seconds=(completed_count - 1) * interval_seconds)
    return min(latest, session_close - dt.timedelta(seconds=interval_seconds))


def yahoo_data_is_stale(bars, day, now=None, interval_minutes=5,
                        grace_seconds=60.0, range_minutes=15):
    """Detect a delayed Yahoo candle after the opening range is scannable.

    Yahoo remains the primary scan provider.  This helper only says when a
    completed candle is overdue long enough to justify a read-only fallback.
    """
    current = now or dt.datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    current = current.astimezone(IST)
    expected = latest_expected_completed_start(day, current, interval_minutes)
    if expected is None:
        return False
    opening_end = dt.datetime.combine(current.date(), NSE_OPEN).replace(tzinfo=IST)
    opening_end += dt.timedelta(minutes=int(range_minutes))
    if expected < opening_end:
        return False
    observed = session_bars(bars, day)
    latest_observed = max(
        (_parse_timestamp(item["timestamp"]) for item in observed),
        default=None,
    )
    if latest_observed is not None and latest_observed >= expected:
        return False
    due = expected + dt.timedelta(minutes=int(interval_minutes))
    return current >= due + dt.timedelta(seconds=float(grace_seconds))


def signal_for(bars, range_minutes=15, day=None, quality_filter=False,
               min_volume_ratio=1.5, min_breakout_strength=0.10,
               min_body_ratio=0.50, max_upper_wick_ratio=0.35):
    """Find a completed opening-range breakout with optional confirmation.

    The legacy signal remains available for replay compatibility.  The live
    paper runners enable ``quality_filter`` so a single wick/low-volume print
    cannot create a trade: the breakout must have strong participation and the
    next completed candle must hold above the range high.
    """
    bars = session_bars(bars, day)
    if not bars:
        return {"status": "INSUFFICIENT_DATA", "bars": 0, "reason": "no NSE session bars"}
    range_minutes = int(range_minutes)
    if range_minutes <= 0:
        raise IntradayError("range_minutes must be positive")
    first_stamp = _parse_timestamp(bars[0]["timestamp"])
    if first_stamp.time().replace(tzinfo=None) != NSE_OPEN:
        return {"status": "INSUFFICIENT_DATA", "bars": len(bars),
                "reason": "session data must start at 09:15 IST"}
    range_end = (dt.datetime.combine(first_stamp.date(), NSE_OPEN) +
                 dt.timedelta(minutes=range_minutes)).time()
    opening = [bar for bar in bars
               if _parse_timestamp(bar["timestamp"]).time().replace(tzinfo=None) < range_end]
    if not opening or len(bars) <= len(opening):
        return {"status": "WAITING", "bars": len(bars),
                "reason": "opening range is not complete"}
    or_high = max(bar["high"] for bar in opening)
    or_low = min(bar["low"] for bar in opening)
    midpoint = (or_high + or_low) / 2.0
    for index in range(len(opening), len(bars)):
        current = bars[index]
        previous = bars[index - 1]
        if current["close"] > or_high and previous["close"] <= or_high:
            candle_range = float(current["high"]) - float(current["low"])
            body_ratio = (abs(float(current["close"]) - float(current["open"])) /
                          candle_range) if candle_range > 0 else 0.0
            upper_wick_ratio = ((float(current["high"]) -
                                 max(float(current["open"]), float(current["close"]))) /
                                candle_range) if candle_range > 0 else 1.0
            prior = [float(item.get("volume") or 0.0)
                     for item in bars[max(0, index - 20):index]
                     if float(item.get("volume") or 0.0) > 0.0]
            baseline_volume = (sum(prior) / len(prior)) if prior else 0.0
            volume_ratio = (float(current.get("volume") or 0.0) / baseline_volume
                            if baseline_volume > 0 else 1.0)
            opening_range = or_high - or_low
            breakout_strength = ((float(current["close"]) - or_high) / opening_range
                                 if opening_range > 0 else 0.0)
            selected = current
            selected_index = index
            if quality_filter:
                if (volume_ratio < float(min_volume_ratio) or
                        breakout_strength < float(min_breakout_strength) or
                        body_ratio < float(min_body_ratio) or
                        upper_wick_ratio > float(max_upper_wick_ratio)):
                    continue
                if index + 1 >= len(bars):
                    return {"status": "WAITING", "bars": len(bars),
                            "or_high": or_high, "or_low": or_low,
                            "stop_reference": midpoint,
                            "reason": "breakout requires a completed hold candle"}
                confirmation = bars[index + 1]
                if (confirmation["close"] <= or_high or
                        confirmation["low"] < or_high):
                    continue
                selected = confirmation
                selected_index = index + 1
            return {
                "status": "HIT", "setup": "opening-range-breakout",
                "signal_timestamp": selected["timestamp"], "signal_date": selected["date"],
                "signal_index": selected_index, "close": selected["close"],
                "high": selected["high"], "low": selected["low"],
                "volume": selected["volume"], "or_high": or_high, "or_low": or_low,
                "stop_reference": midpoint,
                "breakout_timestamp": current["timestamp"],
                "breakout_volume_ratio": round(volume_ratio, 4),
                "breakout_strength": round(breakout_strength, 4),
                "breakout_body_ratio": round(body_ratio, 4),
                "breakout_upper_wick_ratio": round(upper_wick_ratio, 4),
                "confirmation_required": bool(quality_filter),
                "reason": ("breakout plus completed hold candle"
                           if quality_filter else
                           "first completed close above the opening range"),
            }
    return {"status": "NO_SETUP", "bars": len(bars), "or_high": or_high,
            "or_low": or_low, "stop_reference": midpoint,
            "reason": "no completed close above the opening range"}


def estimate_costs(buy_value, sell_value, config=None):
    cfg = _config(config)
    brokerage = 2.0 * float(cfg["brokerage_per_order_inr"])
    slippage = (buy_value + sell_value) * float(cfg["slippage_bps"]) / 10000.0
    transaction = (buy_value + sell_value) * float(cfg["transaction_bps"]) / 10000.0
    stt = sell_value * float(cfg["stt_sell_bps"]) / 10000.0
    gst = (brokerage + transaction) * float(cfg["gst_bps"]) / 10000.0
    total = brokerage + slippage + transaction + stt + gst
    return {"brokerage": round(brokerage, 2), "slippage": round(slippage, 2),
            "transaction": round(transaction, 2), "stt_sell": round(stt, 2),
            "gst": round(gst, 2), "total": round(total, 2)}


def plan_from_signal(signal, capital, max_loss, config=None, entry_override=None,
                     tick_override=None):
    """Create a paper-only long cash plan from an intraday signal."""
    cfg = _config(config)
    capital = _positive(capital, "capital")
    max_loss = _positive(max_loss, "max_loss")
    entry = _positive(entry_override if entry_override is not None else signal["close"], "entry")
    tick = cash_tick(entry, tick_override)
    stop = tick_round(signal["stop_reference"], tick, up=False)
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        raise IntradayError("opening-range midpoint must be below the entry")
    target = tick_round(entry + float(cfg["target_r"]) * risk_per_share, tick, up=True)
    max_by_risk = int(max_loss // risk_per_share)
    portfolio_cap = capital * float(cfg["max_position_pct"]) / 100.0
    trade_cap = min(_positive(cfg["max_trade_value_inr"], "max_trade_value_inr"),
                    ABSOLUTE_MAX_TRADE_VALUE_INR)
    max_by_position = int(min(portfolio_cap, trade_cap) // entry)
    qty = max(0, min(max_by_risk, max_by_position))
    while qty > 0:
        costs = estimate_costs(entry * qty, stop * qty, cfg)
        planned_loss = qty * risk_per_share + costs["total"]
        if planned_loss <= max_loss:
            break
        qty -= 1
    costs = estimate_costs(entry * qty, stop * qty, cfg)
    planned_loss = qty * risk_per_share + costs["total"]
    return {
        "symbol": signal.get("symbol"), "market": "NSE", "currency": "INR",
        "mode": "paper", "execution_enabled": False,
        "setup": signal.get("setup", "opening-range-breakout"),
        "signal_date": signal.get("signal_date"),
        "signal_timestamp": signal.get("signal_timestamp"), "entry": round(entry, 4),
        "stop": round(stop, 4), "target": round(target, 4),
        "flat_by": NSE_FLAT_BY.strftime("%H:%M"), "qty": qty,
        "risk_per_share": round(risk_per_share, 4),
        "planned_loss_inr": round(planned_loss, 2),
        "reward_risk": round((target - entry) / risk_per_share, 4),
        "status": "PAPER_SETUP" if qty > 0 and planned_loss <= max_loss else "BLOCKED",
        "reason": "sized within configured INR limits" if qty > 0 and planned_loss <= max_loss
                  else "capital or risk limit cannot fund one share",
    }


def _exit_session(bars, start, plan):
    last = start
    for index in range(start, len(bars)):
        stamp = _parse_timestamp(bars[index]["timestamp"])
        if stamp.time().replace(tzinfo=None) >= NSE_FLAT_BY:
            break
        last = index
        bar = bars[index]
        if bar["low"] <= plan["stop"]:
            return index, min(bar["open"], plan["stop"]), "stop"
        if bar["high"] >= plan["target"]:
            return index, max(bar["open"], plan["target"]), "target"
    return last, bars[last]["close"], "flat"


def backtest_session(bars, symbol, config=None, capital=100000.0, max_loss=1000.0, day=None):
    cfg = _config(config)
    bars = session_bars(bars, day)
    signal = signal_for(bars, cfg["range_minutes"])
    trades = []
    if signal["status"] == "HIT" and signal["signal_index"] + 1 < len(bars):
        signal["symbol"] = symbol
        entry_index = signal["signal_index"] + 1
        entry_bar = bars[entry_index]
        plan = plan_from_signal(signal, capital, max_loss, cfg, entry_bar["open"])
        if plan["status"] == "PAPER_SETUP":
            exit_index, exit_price, outcome = _exit_session(bars, entry_index, plan)
            qty = plan["qty"]
            gross = (exit_price - plan["entry"]) * qty
            costs = estimate_costs(plan["entry"] * qty, exit_price * qty, cfg)
            net = gross - costs["total"]
            risk = plan["risk_per_share"] * qty + costs["total"]
            trades.append({
                "symbol": symbol, "signal_timestamp": signal["signal_timestamp"],
                "entry_timestamp": entry_bar["timestamp"],
                "exit_timestamp": bars[exit_index]["timestamp"],
                "entry": plan["entry"], "stop": plan["stop"], "target": plan["target"],
                "exit": round(exit_price, 4), "qty": qty, "outcome": outcome,
                "gross_pnl": round(gross, 2), "costs": costs,
                "net_pnl": round(net, 2),
                "r_multiple": round(net / risk, 4) if risk else 0.0,
            })
    return summarize(trades, symbol, len(bars), cfg)


def summarize(trades, symbol, bar_count, config):
    nets = [trade["net_pnl"] for trade in trades]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "symbol": symbol, "mode": "paper_backtest", "execution_enabled": False,
        "bar_count": bar_count, "config": dict(config), "trades": trades,
        "summary": {
            "trades": len(trades), "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(100.0 * len(wins) / len(trades), 2) if trades else 0.0,
            "net_pnl_inr": round(sum(nets), 2),
            "average_r": round(sum(t["r_multiple"] for t in trades) / len(trades), 4)
                         if trades else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else None,
        },
        "caveats": [
            "Historical results are not predictions or investment advice.",
            "Intraday bars cannot model queue priority, halts or exact gap fills.",
            "Charges and slippage are configurable estimates; verify them with your broker.",
            "The default opening-range rule is a transparent example, not the user's strategy.",
        ],
    }


def _write(path, document):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return str(target)


def _emit(document, as_json=False):
    if as_json:
        print(json.dumps(document, indent=2, sort_keys=True))
    elif document.get("status") == "HIT":
        print("NSE INTRADAY SIGNAL  %s" % document.get("signal_timestamp"))
        print("  %s" % document.get("reason", ""))
    elif document.get("mode") == "paper_backtest":
        summary = document["summary"]
        print("NSE INTRADAY BACKTEST  %s  trades %d  net INR %.2f" %
              (document["symbol"], summary["trades"], summary["net_pnl_inr"]))
    else:
        print("%s  %s" % (document.get("status", "OK"), document.get("reason", "")))


def _parser():
    parser = argparse.ArgumentParser(description="Indian NSE intraday paper research")
    sub = parser.add_subparsers(dest="command")
    clock = sub.add_parser("clock", help="show IST/NSE session status")
    clock.add_argument("--as-of")
    clock.add_argument("--json", action="store_true")

    def common(command):
        command.add_argument("--symbol", required=True)
        command.add_argument("--csv")
        command.add_argument("--offline")
        command.add_argument("--yahoo", action="store_true",
                             help="read completed bars from Yahoo Finance")
        command.add_argument("--angelone", action="store_true",
                             help="read historical bars from Angel One")
        command.add_argument("--credentials-file",
                             help="local Angel One JSON file; never written by this tool")
        command.add_argument("--universe", dest="universe_path",
                             help="NSE universe JSON containing Angel One tokens")
        command.add_argument("--interval", default="5m",
                             choices=("1m", "2m", "3m", "5m", "10m", "15m", "30m", "60m", "90m", "1h", "1d"))
        command.add_argument("--range", dest="range_name", default="1d",
                             help="Yahoo range, for example 1d or 5d")
        command.add_argument("--day", help="IST trading date YYYY-MM-DD")
        command.add_argument("--fromdate", help="Angel One start, YYYY-MM-DD HH:MM")
        command.add_argument("--todate", help="Angel One end, YYYY-MM-DD HH:MM")
        command.add_argument("--range-minutes", type=int, dest="range_minutes")
        command.add_argument("--target-r", type=float, dest="target_r")
        command.add_argument("--max-position-pct", type=float, dest="max_position_pct")
        command.add_argument("--max-trade-value", type=float, dest="max_trade_value_inr",
                             help="maximum notional value per trade in INR")
        command.add_argument("--json", action="store_true")

    scan = sub.add_parser("scan", help="scan completed intraday bars")
    common(scan)
    scan.add_argument("--write", action="store_true")
    scan.add_argument("--out", default=os.path.join("trader", "intraday_watchlist.json"))

    plan = sub.add_parser("plan", help="create a paper-only intraday plan")
    common(plan)
    plan.add_argument("--capital", type=float, required=True)
    plan.add_argument("--max-loss", type=float, required=True, dest="max_loss")
    plan.add_argument("--write", action="store_true")
    plan.add_argument("--out", default=os.path.join("trader", "intraday_plan.json"))

    backtest = sub.add_parser("backtest", help="backtest the opening-range rule")
    common(backtest)
    backtest.add_argument("--capital", type=float, default=100000.0)
    backtest.add_argument("--max-loss", type=float, default=1000.0, dest="max_loss")
    backtest.add_argument("--write", action="store_true")
    backtest.add_argument("--out", default=os.path.join("trader", "intraday_backtest.json"))
    return parser


def _config_from_args(args):
    cfg = dict(DEFAULTS)
    for name in ("range_minutes", "target_r", "max_position_pct", "max_trade_value_inr"):
        value = getattr(args, name, None)
        if value is not None:
            cfg[name] = value
    return cfg


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "clock":
            now = dt.datetime.fromisoformat(args.as_of) if args.as_of else None
            _emit(session_status(now), args.json)
            return 0
        if args.command not in ("scan", "plan", "backtest"):
            _parser().print_help()
            return 0
        effective_day = args.day or dt.datetime.now(IST).date().isoformat()
        market_day = calendar_status(effective_day)
        if not market_day["trading_day"]:
            document = {"market": "NSE", "currency": "INR", "mode": "intraday_%s" % args.command,
                        "execution_enabled": False, "status": "BLOCKED",
                        "reason": market_day["reason"],
                        "calendar_known": market_day["calendar_known"]}
            _emit(document, args.json)
            return 1
        cfg = _config_from_args(args)
        bars, source = load_source(
            args.symbol, args.csv, args.offline, args.yahoo, args.angelone,
            args.credentials_file, args.universe_path, args.interval,
            args.range_name, effective_day, args.fromdate, args.todate,
        )
        signal = signal_for(bars, cfg["range_minutes"], effective_day)
        signal.update({"symbol": args.symbol.upper(), "source": source})
        if args.command == "scan":
            document = {"market": "NSE", "currency": "INR", "mode": "intraday_scan",
                        "execution_enabled": False, "signal": signal, "config": cfg}
        elif args.command == "plan":
            if signal["status"] != "HIT":
                document = {"status": "BLOCKED", "reason": "no completed intraday signal",
                            "signal": signal}
            else:
                document = plan_from_signal(signal, args.capital, args.max_loss, cfg)
                document["source"] = source
        else:
            document = backtest_session(bars, args.symbol.upper(), cfg,
                                         args.capital, args.max_loss, effective_day)
            document["source"] = source
        _emit(document, args.json)
        if args.write:
            print("Wrote %s" % _write(args.out, document))
        return 0 if document.get("status", "OK") not in ("BLOCKED", "INSUFFICIENT_DATA") else 1
    except (IntradayError, OSError, ValueError, TypeError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
