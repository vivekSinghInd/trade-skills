#!/usr/bin/env python3
"""Run the NSE paper workflow end to end and resume safely after restarts.

The runner is deliberately paper-only.  It owns the daily orchestration:
calendar/universe gate, selected-universe breakout scan, news review, next-bar
paper planning, journal entries, and completed-bar exit monitoring.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTRADAY_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(INTRADAY_DIR) not in sys.path:
    sys.path.insert(0, str(INTRADAY_DIR))

import alerts
import intraday
import nse_news
import paper_journal
import paper_monitor
import scan
from india_market import IST, NSE_FLAT_BY
from india_universe import (ANGELONE_MASTER_URL, UniverseError, download_master,
                            NIFTY1000_CONSTITUENT_URL,
                            download_nifty1000_constituents,
                            nse_cash, read_master, read_nifty1000_constituents,
                            universe_document, write_document, write_master,
                            write_nifty1000_constituents)
from nse_calendar import calendar_status
from preflight import run_preflight


DEFAULT_CAPITAL = 100000.0
DEFAULT_MAX_LOSS = 1000.0
DEFAULT_MAX_TRADES = 20
DEFAULT_MAX_OPEN_POSITIONS = 10
DEFAULT_MAX_DAILY_LOSS = 3000.0
DEFAULT_MAX_OPEN_RISK = 3000.0
DEFAULT_MAX_TRADE_VALUE = 10000.0
DEFAULT_NEWS_CANDIDATES = 20
DEFAULT_CANDIDATE_LIMIT = 0
DEFAULT_RESCAN_EVERY_MINUTES = 5.0
DEFAULT_WORKERS = 8
DEFAULT_NIFTY1000_UNIVERSE = os.path.join("trader", "nifty1000_universe.json")
DEFAULT_NIFTY1000_CACHE = os.path.join("trader", "indices", "nifty1000.csv")


def _write(path, document):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(target) + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, target)
    return str(target)


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _parse_interval(value):
    match = re.fullmatch(r"(\d+)m", str(value).strip().lower())
    if not match or int(match.group(1)) <= 0:
        raise ValueError("interval must be a positive minute interval such as 5m")
    return int(match.group(1))


def _is_etf(row):
    text = "%s %s" % (row.get("base_symbol", ""), row.get("name", ""))
    return bool(re.search(r"ETF|BEES|NIFTY", text, re.IGNORECASE))


def universe_rows(path):
    document = _read_json(path, {})
    rows = document.get("instruments", []) if isinstance(document, dict) else []
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("base_symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        if str(row.get("series") or "EQ").upper() != "EQ":
            continue
        seen.add(symbol)
        result.append(dict(row, base_symbol=symbol, is_etf=_is_etf(row)))
    return result


def refresh_universe(path, cache_path=None, profile="full",
                     constituent_url=NIFTY1000_CONSTITUENT_URL,
                     constituent_cache_path=None):
    """Refresh a full NSE or official Nifty 1000 universe without credentials."""
    profile = str(profile or "full").strip().lower()
    if profile not in ("full", "nifty1000"):
        raise UniverseError("unsupported universe profile: %s" % profile)
    target = Path(path)
    cache = Path(cache_path or (ROOT / "trader" / "angelone" / "OpenAPIScripMaster.json"))
    source = "angelone-instrument-master"
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        rows = download_master(ANGELONE_MASTER_URL)
        write_master(cache, rows)
    except UniverseError:
        if not cache.is_file():
            raise
        rows = read_master(cache)
        source = "angelone-cache-fallback"
        fetched_at = dt.datetime.fromtimestamp(cache.stat().st_mtime,
                                               dt.timezone.utc).isoformat()
    if profile == "nifty1000":
        index_cache = Path(constituent_cache_path or (ROOT / DEFAULT_NIFTY1000_CACHE))
        index_source = constituent_url
        index_fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            constituents = download_nifty1000_constituents(constituent_url)
            write_nifty1000_constituents(index_cache, constituents)
        except UniverseError:
            if not index_cache.is_file():
                raise
            constituents = read_nifty1000_constituents(index_cache)
            index_source = "niftyindices-cache-fallback"
            index_fetched_at = dt.datetime.fromtimestamp(
                index_cache.stat().st_mtime, dt.timezone.utc).isoformat()
        document = universe_document(
            rows, source, fetched_at=fetched_at, allowed_symbols=constituents,
            index_name="NIFTY 1000", index_source=index_source,
            index_fetched_at=index_fetched_at)
        document["profile"] = "nifty1000"
    else:
        document = universe_document(rows, source, fetched_at=fetched_at)
        document["profile"] = "full"
    write_document(target, document)
    return document


def _universe_path(args):
    return getattr(args, "universe_path", getattr(args, "universe", ""))


def _loader_factory(args, day):
    interval_minutes = _parse_interval(args.interval)

    def load_primary(symbol):
        return intraday.load_source(
            symbol,
            offline=args.offline,
            yahoo=args.source == "yahoo",
            angelone=args.source == "angelone",
            credentials_file=args.credentials_file,
            universe_path=_universe_path(args),
            interval=args.interval,
            range_name=args.range_name,
            day=day,
        )

    def load_fallback(symbol, primary_error=None):
        try:
            bars, _ = intraday.load_source(
                symbol,
                credentials_file=args.credentials_file,
                universe_path=_universe_path(args),
                interval=args.interval,
                range_name=args.range_name,
                day=day,
                angelone=True,
            )
            return bars, "angelone-historical-fallback"
        except (intraday.IntradayError, OSError, ValueError, TypeError) as exc:
            if primary_error is not None:
                raise intraday.IntradayError(
                    "%s; Angel One fallback failed: %s" % (primary_error, exc)
                ) from exc
            raise

    def loader(symbol):
        if args.source != "yahoo" or args.scan_fallback_source == "none":
            return load_primary(symbol)
        current = dt.datetime.now(IST)
        try:
            bars, source = load_primary(symbol)
        except (intraday.IntradayError, OSError, ValueError, TypeError) as exc:
            return load_fallback(symbol, primary_error=str(exc))
        if intraday.yahoo_data_is_stale(
                bars, day, now=current, interval_minutes=interval_minutes,
                grace_seconds=args.yahoo_fallback_delay_seconds,
                range_minutes=intraday.DEFAULTS["range_minutes"]):
            return load_fallback(symbol)
        return bars, source
    return loader


def scan_full_universe(rows, loader, day, interval_minutes, now, workers,
                       quality_filter=False):
    """Scan symbols concurrently while preserving the completed-bar gate."""
    hits = []
    no_setup = []
    failed = []

    def one(row):
        symbol = row["base_symbol"]
        try:
            bars, source = loader(symbol)
            closed = scan.completed_session_bars(
                bars, day, now=now, interval_minutes=interval_minutes)
            if not closed:
                return "no_setup", {"symbol": symbol, "status": "INSUFFICIENT_DATA"}
            signal = intraday.signal_for(closed, day=day,
                                         quality_filter=quality_filter)
            signal.update({"symbol": symbol, "source": source, "is_etf": row["is_etf"]})
            if signal.get("status") == "HIT":
                return "hit", signal
            return "no_setup", signal
        except (intraday.IntradayError, OSError, ValueError, TypeError) as exc:
            return "failed", {"symbol": symbol, "error": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = [pool.submit(one, row) for row in rows]
        for future in concurrent.futures.as_completed(futures):
            kind, item = future.result()
            if kind == "hit":
                hits.append(item)
            elif kind == "failed":
                failed.append(item)
            else:
                no_setup.append(item)
    hits.sort(key=lambda item: (-float(item.get("volume") or 0), item.get("symbol", "")))
    return {"hits": hits, "no_setup": no_setup, "failed": failed}


def _news_report(symbols, state_path, hours):
    """Fetch exchange/Yahoo news once and persist de-duplication state."""
    since = dt.datetime.now(IST) - dt.timedelta(hours=float(hours))
    items = []
    errors = []
    try:
        items.extend(nse_news.parse_announcements(
            nse_news._fetch_json(), symbols=symbols, since=since))
    except nse_news.NSENewsError as exc:
        errors.append("NSE: %s" % exc)
    for symbol in symbols:
        try:
            rss = nse_news._fetch_text(
                nse_news.YAHOO_RSS % nse_news.urllib.parse.quote(symbol + ".NS"))
            items.extend(nse_news.parse_yahoo_rss(rss, symbol, since=since))
        except nse_news.NSENewsError as exc:
            errors.append("Yahoo %s: %s" % (symbol, exc))
    items.sort(key=lambda item: item.get("broadcast_at", ""), reverse=True)
    state = nse_news.load_state(state_path)
    fresh = nse_news.collect_new(items, state)
    nse_news.write_state(state_path, state)
    return {"source": "both", "symbols": list(symbols), "items": fresh,
            "errors": errors, "status": "PARTIAL" if errors else "OK",
            "execution_enabled": False}


def _entry_bar(bars, signal, day, interval_minutes, now):
    closed = scan.completed_session_bars(
        bars, day, now=now, interval_minutes=interval_minutes)
    signal_timestamp = signal.get("signal_timestamp")
    if not signal_timestamp:
        return closed, None
    for bar in closed:
        if intraday._parse_timestamp(bar["timestamp"]) > intraday._parse_timestamp(signal_timestamp):
            return closed, bar
    return closed, None


def _selection_metrics(signal, closed, entry_bar, plan):
    """Add comparable setup-quality metrics instead of ranking raw share volume."""
    signal_index = int(signal.get("signal_index") or 0)
    prior_volumes = [float(bar.get("volume") or 0.0)
                     for bar in closed[:signal_index]
                     if float(bar.get("volume") or 0.0) > 0.0]
    baseline_volume = statistics.mean(prior_volumes) if prior_volumes else 0.0
    breakout_volume = float(signal.get("volume") or 0.0)
    volume_ratio = breakout_volume / baseline_volume if baseline_volume > 0 else 1.0
    opening_range = float(signal.get("or_high") or 0.0) - float(signal.get("or_low") or 0.0)
    breakout_strength = ((float(signal.get("close") or 0.0) - float(signal.get("or_high") or 0.0))
                         / opening_range) if opening_range > 0 else 0.0
    entry = float(plan.get("entry") or 0.0)
    entry_gap_pct = (abs(float(entry_bar.get("open") or 0.0) - float(signal.get("close") or 0.0))
                     / entry) if entry > 0 else 0.0

    # Cap each component so one unusually large print cannot dominate the book.
    score = (
        min(max(volume_ratio, 0.0), 10.0) * 10.0
        + min(max(breakout_strength, 0.0), 2.0) * 25.0
        + min(max(float(plan.get("reward_risk") or 0.0), 0.0), 4.0) * 5.0
        - min(max(entry_gap_pct, 0.0), 0.05) * 100.0
    )
    return {
        "breakout_volume_ratio": round(volume_ratio, 4),
        "breakout_strength": round(breakout_strength, 4),
        "entry_gap_pct": round(entry_gap_pct, 6),
        "selection_score": round(score, 4),
    }


def build_paper_candidates(hits, rows_by_symbol, loader, day, now, interval_minutes,
                           capital, max_loss, max_trade_value, target_r, limit,
                           workers=DEFAULT_WORKERS):
    """Build all eligible next-bar plans, unless an explicit limit is supplied."""
    selected_hits = hits if int(limit) <= 0 else hits[:int(limit)]
    row_by_symbol = rows_by_symbol

    def one(hit):
        symbol = hit["symbol"]
        try:
            bars, source = loader(symbol)
            closed, entry_bar = _entry_bar(bars, hit, day, interval_minutes, now)
            if entry_bar is None:
                return {"symbol": symbol, "status": "BLOCKED",
                        "reason": "no completed next-bar entry yet",
                        "signal": hit, "source": source}
            signal = dict(hit)
            signal["symbol"] = symbol
            signal["source"] = source
            cfg = dict(intraday.DEFAULTS)
            cfg["target_r"] = float(target_r)
            cfg["max_trade_value_inr"] = float(max_trade_value)
            tick = row_by_symbol.get(symbol, {}).get("tick_size_inr")
            plan = intraday.plan_from_signal(
                signal, capital, max_loss, cfg,
                entry_override=entry_bar["open"], tick_override=tick)
            plan["entry_timestamp"] = entry_bar["timestamp"]
            plan["source"] = source
            plan["breakout_volume"] = hit.get("volume")
            plan["news_status"] = "PENDING"
            plan.update(_selection_metrics(hit, closed, entry_bar, plan))
            return plan
        except (intraday.IntradayError, OSError, ValueError, TypeError) as exc:
            return {"symbol": symbol, "status": "BLOCKED",
                    "reason": str(exc), "signal": hit}

    candidates = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = [pool.submit(one, hit) for hit in selected_hits]
        for future in concurrent.futures.as_completed(futures):
            candidates.append(future.result())
    return candidates


def rank_paper_candidates(candidates):
    """Rank viable plans by relative setup quality and annotate their rank."""
    setups = [item for item in candidates if item.get("status") == "PAPER_SETUP"]
    others = [item for item in candidates if item.get("status") != "PAPER_SETUP"]
    setups.sort(key=lambda item: (
        -float(item.get("selection_score") or 0.0),
        -float(item.get("breakout_volume_ratio") or 0.0),
        -float(item.get("breakout_volume") or 0.0),
        str(item.get("symbol") or ""),
    ))
    for rank, item in enumerate(setups, 1):
        item["selection_rank"] = rank
    return setups + others


def _attach_news(candidates, news):
    by_symbol = {}
    for item in news.get("items", []):
        by_symbol.setdefault(item.get("symbol", "").upper(), []).append(item)
    errors = set()
    for error in news.get("errors", []):
        match = re.search(r"Yahoo ([A-Z0-9&.-]+)", error)
        if match:
            errors.add(match.group(1).upper())
    for candidate in candidates:
        symbol = candidate.get("symbol", "").upper()
        candidate["news_items"] = by_symbol.get(symbol, [])
        candidate["news_status"] = "ERROR" if symbol in errors else (
            "REVIEW" if candidate["news_items"] else "NO_FRESH_ITEMS")
    return candidates


def _enter_candidates(state, candidates, state_path, max_loss, max_trades,
                      max_daily_loss, max_trade_value, day,
                      max_open_positions, max_open_risk):
    entered = []
    rejected = []
    already_traded = {
        str(item.get("symbol") or "").upper()
        for item in state.get("open_positions", []) + state.get("trades", [])
    }
    for candidate in candidates:
        if candidate.get("status") != "PAPER_SETUP":
            continue
        symbol = str(candidate.get("symbol") or "").upper()
        if symbol in already_traded:
            candidate["status"] = "SKIPPED"
            candidate["rejection_reason"] = "already traded today"
            rejected.append(candidate)
            continue
        try:
            paper_journal.enter(
                state, candidate, max_loss=max_loss, max_trades=max_trades,
                max_daily_loss=max_daily_loss, max_trade_value=max_trade_value,
                timestamp=candidate.get("entry_timestamp") or candidate.get("signal_timestamp"),
                max_open_positions=max_open_positions, max_open_risk=max_open_risk)
            entered.append(candidate)
            already_traded.add(symbol)
            plan_path = ROOT / "trader" / ("paper_plan_%s_%s.json" % (candidate["symbol"], day))
            _write(plan_path, candidate)
        except paper_journal.JournalError as exc:
            candidate["status"] = "REJECTED"
            candidate["rejection_reason"] = str(exc)
            rejected.append(candidate)
    paper_journal.write_state(state_path, state)
    return {"entered": entered, "rejected": rejected}


def _scan_cycle(args, rows, loader, rows_by_symbol, day, now, interval_minutes):
    """Run one full scan/news/plan cycle for initial entries or rescan entries."""
    scan_result = scan_full_universe(
        rows, loader, day, interval_minutes, now, args.workers,
        quality_filter=args.quality_filter)
    eligible_hits = [item for item in scan_result["hits"] if not item.get("is_etf")]
    news_symbols = [item["symbol"] for item in eligible_hits[:args.news_candidates]]
    news = _news_report(news_symbols, args.news_state, args.news_hours)
    candidates = build_paper_candidates(
        eligible_hits, rows_by_symbol, loader, day, now, interval_minutes,
        args.capital, args.max_loss, args.max_trade_value, args.target_r,
        args.candidate_limit, workers=args.workers)
    candidates = rank_paper_candidates(_attach_news(candidates, news))
    return scan_result, news, candidates


def run(args):
    day = args.day or dt.datetime.now(IST).date().isoformat()
    now = dt.datetime.now(IST)
    profile = getattr(args, "universe_profile", "full")
    args.universe_path = (args.nifty1000_universe
                          if profile == "nifty1000" else args.universe)
    if args.max_open_positions is None:
        args.max_open_positions = max(1, int(float(args.capital) // float(args.max_trade_value)))
    if args.max_open_risk is None:
        args.max_open_risk = min(float(args.max_daily_loss),
                                 float(args.max_loss) * 3.0,
                                 DEFAULT_MAX_OPEN_RISK)
    market = calendar_status(day)
    report = {"market": "NSE", "currency": "INR", "day": day,
              "execution_enabled": False, "mode": "nse_daily_paper_runner",
              "started_at": now.isoformat(), "market_status": market,
              "universe_profile": profile, "universe": args.universe_path,
              "data_sources": {"scan_and_selection": args.source,
                               "scan_fallback": (args.scan_fallback_source
                                                  if args.source == "yahoo" else None),
                               "yahoo_fallback_delay_seconds": (
                                   float(args.yahoo_fallback_delay_seconds)
                                   if args.source == "yahoo" else None),
                               "live_monitor": args.monitor_source},
              "limits": {
                  "capital_inr": float(args.capital),
                  "max_loss_per_trade_inr": float(args.max_loss),
                  "max_trades_per_day": int(args.max_trades),
                  "max_open_positions": int(args.max_open_positions),
                  "max_open_risk_inr": float(args.max_open_risk),
                  "max_daily_loss_inr": float(args.max_daily_loss),
                  "max_trade_value_inr": float(args.max_trade_value),
              }}
    state_path = Path(args.state)
    state = paper_journal.load_state(state_path, day, args.capital)

    if not market["trading_day"]:
        report.update({"status": "BLOCKED", "reason": market["reason"]})
        _write(args.out, report)
        return report

    # Idempotency: if this day already has a journal entry, resume monitoring
    # instead of re-entering the same symbols.  A resumed runner can still
    # rescan for fresh symbols after the current paper book is loaded.
    already_started = bool(state.get("open_positions") or state.get("trades"))
    rows = None
    loader = None
    interval_minutes = None
    rows_by_symbol = None
    last_rescan_at = None
    if already_started:
        report.update({"status": "RESUMED", "reason": "existing paper journal found"})
    else:
        if args.refresh_universe:
            try:
                report["universe_refresh"] = refresh_universe(
                    args.universe_path, profile=profile,
                    constituent_url=args.nifty1000_url,
                    constituent_cache_path=args.nifty1000_cache)
            except (UniverseError, OSError, ValueError, TypeError) as exc:
                report["universe_refresh"] = {"status": "ERROR", "reason": str(exc)}
        preflight = run_preflight(
            day, args.universe_path, capital=args.capital, max_loss=args.max_loss,
            max_trade_value=args.max_trade_value, max_daily_loss=args.max_daily_loss,
            source=args.source, monitor_source=args.monitor_source,
            credentials_file=args.credentials_file,
            max_age_hours=args.max_universe_age_hours, now=now.isoformat())
        report["preflight"] = preflight
        if preflight["status"] != "READY_FOR_PAPER":
            report.update({"status": "BLOCKED", "reason": "preflight failed"})
            _write(args.out, report)
            return report

        rows = universe_rows(args.universe_path)
        if not rows:
            raise intraday.IntradayError("NSE universe has no equity symbols")
        loader = _loader_factory(args, day)
        interval_minutes = _parse_interval(args.interval)
        rows_by_symbol = {row["base_symbol"]: row for row in rows}
        scan_result, news, candidates = _scan_cycle(
            args, rows, loader, rows_by_symbol, day, now, interval_minutes)
        report["scan"] = {"universe_count": len(rows), "scanned": len(rows),
                           **scan_result}
        _write(args.scan_out, report["scan"])
        report["news"] = news
        report["candidates"] = candidates
        entry_result = _enter_candidates(
            state, candidates, state_path, args.max_loss, args.max_trades,
            args.max_daily_loss, args.max_trade_value, day,
            args.max_open_positions, args.max_open_risk)
        report["entered"] = entry_result["entered"]
        report["rejected"] = entry_result["rejected"]
        report["status"] = "PAPER_STARTED" if report["entered"] else "NO_PAPER_ENTRIES"
        last_rescan_at = now

    if rows is None:
        rows = universe_rows(args.universe_path)
        if not rows:
            raise intraday.IntradayError("NSE universe has no equity symbols")
        loader = _loader_factory(args, day)
        interval_minutes = _parse_interval(args.interval)
        rows_by_symbol = {row["base_symbol"]: row for row in rows}
        # A restart with an existing journal gets one immediate rescan.  The
        # journal prevents duplicate symbols while allowing a new candidate or
        # a previously blocked next-bar plan to be considered.
        last_rescan_at = now - dt.timedelta(minutes=args.rescan_every_minutes)

    _write(args.out, report)
    if args.once:
        monitor_report = paper_monitor.check_once(
            state_path, day, source=args.monitor_source, offline=None,
            credentials_file=args.credentials_file, universe_path=args.universe_path,
            interval=args.interval, range_name=args.range_name, now=dt.datetime.now(IST))
        report["monitor"] = monitor_report
        _write(args.out, report)
        return report

    until = dt.datetime.strptime(args.until, "%H:%M").time()
    while True:
        monitor_report = paper_monitor.check_once(
            state_path, day, source=args.monitor_source, offline=None,
            credentials_file=args.credentials_file, universe_path=args.universe_path,
            interval=args.interval, range_name=args.range_name, now=dt.datetime.now(IST))
        report["monitor"] = monitor_report
        report["updated_at"] = monitor_report["checked_at"]

        current = dt.datetime.now(IST)
        if (args.rescan_every_minutes > 0 and
                (last_rescan_at is None or
                 (current - last_rescan_at).total_seconds()
                 >= args.rescan_every_minutes * 60.0) and
                current.time().replace(tzinfo=None) < until):
            scan_result, news, candidates = _scan_cycle(
                args, rows, loader, rows_by_symbol, day, current, interval_minutes)
            report["scan"] = {"universe_count": len(rows), "scanned": len(rows),
                               **scan_result}
            report["news"] = news
            report["candidates"] = candidates
            _write(args.scan_out, report["scan"])
            entry_result = _enter_candidates(
                state, candidates, state_path, args.max_loss, args.max_trades,
                args.max_daily_loss, args.max_trade_value, day,
                args.max_open_positions, args.max_open_risk)
            report["entered"] = report.get("entered", []) + entry_result["entered"]
            report["rejected"] = report.get("rejected", []) + entry_result["rejected"]
            report["last_rescan_at"] = current.isoformat()
            report["rescan_count"] = int(report.get("rescan_count", 0)) + 1
            last_rescan_at = current
            # The monitor below should reflect entries created by this cycle.
            monitor_report = paper_monitor.check_once(
                state_path, day, source=args.monitor_source, offline=None,
                credentials_file=args.credentials_file, universe_path=args.universe_path,
                interval=args.interval, range_name=args.range_name, now=current)
            report["monitor"] = monitor_report
            report["updated_at"] = monitor_report["checked_at"]

        _write(args.out, report)
        statuses = ", ".join(
            "%s:%s" % (item.get("symbol"), item.get("status"))
            for item in monitor_report.get("results", [])) or "no open positions"
        print("NSE DAILY RUN  %s  cash INR %.2f  realized INR %.2f  %s" % (
            monitor_report["checked_at"], monitor_report["cash_inr"],
            monitor_report["realized_pnl_inr"], statuses), flush=True)
        current = dt.datetime.now(IST)
        # Keep the runner alive with an empty paper book so the scheduled
        # rescans can discover a new breakout later in the session.
        if current.time().replace(tzinfo=None) >= until:
            return report
        time.sleep(min(max(args.every, 1.0), 300.0))


def _parser():
    parser = argparse.ArgumentParser(description="NSE end-to-end paper workflow")
    parser.add_argument("--day", default=None)
    parser.add_argument("--universe", default=os.path.join("trader", "nse_universe.json"))
    parser.add_argument("--nifty1000-universe", default=DEFAULT_NIFTY1000_UNIVERSE,
                        help="separate output file for the official Nifty 1000 profile")
    parser.add_argument("--universe-profile", choices=("full", "nifty1000"),
                        default="nifty1000", help="NSE symbol universe used for scanning")
    parser.add_argument("--nifty1000", dest="universe_profile", action="store_const",
                        const="nifty1000", help="use the official Nifty 1000 constituents")
    parser.add_argument("--full-universe", dest="universe_profile", action="store_const",
                        const="full", help="use all cached NSE EQ symbols instead")
    parser.add_argument("--nifty1000-url", default=NIFTY1000_CONSTITUENT_URL,
                        help="official Nifty 1000 constituent CSV URL")
    parser.add_argument("--nifty1000-cache", default=DEFAULT_NIFTY1000_CACHE,
                        help="cached Nifty 1000 constituent CSV")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--yahoo", action="store_true")
    source.add_argument("--angelone", action="store_true")
    source.add_argument("--offline")
    parser.add_argument("--scan-fallback-source", choices=("none", "angelone"),
                        default="angelone",
                        help="read-only fallback when Yahoo completed candles lag")
    parser.add_argument("--yahoo-fallback-delay-seconds", type=float, default=60.0,
                        help="grace after a candle is due before Angel One fallback")
    parser.add_argument("--monitor-source", choices=("angelone", "yahoo"),
                        default="angelone",
                        help="live-price source after Yahoo scan/selection; default: Angel One LTP")
    parser.add_argument("--credentials-file")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--range", dest="range_name", default="1d")
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--max-loss", type=float, default=DEFAULT_MAX_LOSS)
    parser.add_argument("--max-trades", type=int, default=DEFAULT_MAX_TRADES)
    parser.add_argument("--max-open-positions", type=int, default=DEFAULT_MAX_OPEN_POSITIONS)
    parser.add_argument("--max-daily-loss", type=float, default=DEFAULT_MAX_DAILY_LOSS)
    parser.add_argument("--max-open-risk", type=float, default=None,
                        help="maximum aggregate stop-risk across open paper positions")
    parser.add_argument("--max-trade-value", type=float, default=DEFAULT_MAX_TRADE_VALUE)
    parser.add_argument("--target-r", type=float, default=2.0)
    parser.add_argument("--news-candidates", type=int, default=DEFAULT_NEWS_CANDIDATES)
    parser.add_argument("--news-hours", type=float, default=24.0)
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATE_LIMIT,
                        help="plans to build per scan; 0 means every non-ETF breakout")
    parser.add_argument("--quality-filter", dest="quality_filter", action="store_true",
                        default=True,
                        help="require volume, candle-quality and next-bar hold confirmation")
    parser.add_argument("--legacy-entry-logic", dest="quality_filter", action="store_false",
                        help="use the original first-close breakout rule")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--every", type=float, default=30.0)
    parser.add_argument("--rescan-every-minutes", type=float,
                        default=DEFAULT_RESCAN_EVERY_MINUTES,
                        help="full-universe rescan cadence; 0 disables rescans")
    parser.add_argument("--until", default=NSE_FLAT_BY.strftime("%H:%M"))
    parser.add_argument("--max-universe-age-hours", type=float, default=48.0)
    parser.add_argument("--refresh-universe", dest="refresh_universe",
                        action="store_true", default=True,
                        help="refresh the public Angel One instrument master before scanning")
    parser.add_argument("--no-refresh-universe", dest="refresh_universe",
                        action="store_false", help="use the cached universe as-is")
    parser.add_argument("--state", default=os.path.join("trader", "state", "nse_paper.json"))
    parser.add_argument("--news-state", default=os.path.join("trader", "state", "nse_news.json"))
    parser.add_argument("--out", default=os.path.join("trader", "today_paper_run.json"))
    parser.add_argument("--scan-out", default=os.path.join("trader", "today_full_nse_scan.json"))
    parser.add_argument("--once", action="store_true", help="run one monitor pass and exit")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    args.source = "offline" if args.offline else "angelone" if args.angelone else "yahoo"
    try:
        report = run(args)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report.get("status") not in ("BLOCKED", "NO_PAPER_ENTRIES") else 1
    except (intraday.IntradayError, paper_journal.JournalError,
            nse_news.NSENewsError, OSError, ValueError, TypeError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
