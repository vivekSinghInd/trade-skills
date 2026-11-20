#!/usr/bin/env python3
"""Local read-only dashboard for the NSE paper workflow."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
INTRADAY_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(INTRADAY_DIR) not in sys.path:
    sys.path.insert(0, str(INTRADAY_DIR))

import paper_journal


HTML_PATH = INTRADAY_DIR / "dashboard.html"


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value
    except (OSError, ValueError, TypeError):
        return default


def _status_text(report, state):
    monitor = report.get("monitor", {}) if isinstance(report, dict) else {}
    if monitor.get("results"):
        statuses = [str(item.get("status", "UNKNOWN")) for item in monitor["results"]]
        if "ERROR" in statuses:
            return "DATA_ERROR"
        if any(status in ("TRIGGERED", "EXPIRED") for status in statuses):
            return "EXIT_RECORDED"
        return "MONITORING"
    if state.get("open_positions"):
        return "PAPER_OPEN"
    return report.get("status", "WAITING") if isinstance(report, dict) else "WAITING"


def build_status(root=ROOT):
    report = _read_json(root / "trader" / "today_paper_run.json", {})
    state = _read_json(root / "trader" / "state" / "nse_paper.json", {})
    news = _read_json(root / "trader" / "state" / "nse_news.json", {})
    scan = _read_json(root / "trader" / "today_full_nse_scan.json", {})
    candidates = report.get("candidates", []) if isinstance(report, dict) else []
    entered = report.get("entered", []) if isinstance(report, dict) else []
    rejected = report.get("rejected", []) if isinstance(report, dict) else []
    selection = {
        "candidates": len(candidates),
        "paper_setups": sum(item.get("status") == "PAPER_SETUP" for item in candidates),
        "blocked": sum(item.get("status") == "BLOCKED" for item in candidates),
        "entered": len(entered),
        "rejected": len(rejected),
        "rescans": int(report.get("rescan_count", 0)) if isinstance(report, dict) else 0,
    }
    candidate_board = []
    for item in candidates[:30]:
        if not isinstance(item, dict):
            continue
        candidate_board.append({
            "symbol": item.get("symbol"),
            "status": item.get("status"),
            "rank": item.get("selection_rank"),
            "score": item.get("selection_score"),
            "entry": item.get("entry"),
            "stop": item.get("stop"),
            "target": item.get("target"),
            "reason": item.get("rejection_reason") or item.get("reason"),
        })
    monitor = report.get("monitor", {}) if isinstance(report, dict) else {}
    by_symbol = {
        str(item.get("symbol", "")).upper(): item
        for item in monitor.get("results", []) if isinstance(item, dict)
    }
    positions = []
    for position in state.get("open_positions", []) if isinstance(state, dict) else []:
        item = dict(position)
        live = by_symbol.get(str(position.get("symbol", "")).upper(), {})
        item.update({key: live[key] for key in (
            "status", "source", "last_completed_bar", "last_price",
            "unrealized_pnl_inr", "reason") if key in live})
        positions.append(item)
    return {
        "market": "NSE",
        "currency": "INR",
        "as_of": monitor.get("checked_at") or report.get("updated_at"),
        "runner_status": _status_text(report, state),
        "execution_enabled": False,
        "paper_only": True,
        "day": state.get("day") or report.get("day"),
        "stages": {
            "calendar": "READY" if report.get("market_status", {}).get("trading_day") else "BLOCKED",
            "universe": "READY" if report.get("universe_refresh", {}).get("count", scan.get("universe_count", 0)) else "UNKNOWN",
            "scan": "DONE" if scan else "WAITING",
            "news": "ERROR" if news.get("errors") else "DONE" if news else "WAITING",
            "paper": "OPEN" if positions else "CLOSED" if state.get("trades") else "WAITING",
            "monitor": "ACTIVE" if positions else "IDLE",
        },
        "scan": {
            "universe_count": scan.get("universe_count", 0),
            "scanned": scan.get("scanned", 0),
            "breakout_hits": len(scan.get("hits", [])),
            "no_setup": len(scan.get("no_setup", [])),
            "errors": len(scan.get("failed", [])),
        },
        "news": {
            "fresh_items": len(news.get("items", [])),
            "errors": news.get("errors", []),
        },
        "selection": selection,
        "candidate_board": candidate_board,
        "limits": report.get("limits", {}) if isinstance(report, dict) else {},
        "portfolio": {
            "starting_capital_inr": state.get("starting_capital_inr", 0.0),
            "cash_inr": monitor.get("cash_inr", state.get("cash_inr", 0.0)),
            "realized_pnl_inr": monitor.get("realized_pnl_inr", state.get("realized_pnl_inr", 0.0)),
            "unrealized_pnl_inr": monitor.get("unrealized_pnl_inr", 0.0),
            "open_risk_inr": paper_journal.open_risk_inr(state),
            "open_positions": len(positions),
            "closed_trades": len(state.get("trades", [])),
        },
        "positions": positions,
        "trades": state.get("trades", []),
        "data_notes": [
            "Prices and exits use completed 5-minute candles.",
            "Yahoo Finance may be delayed or rate-limited.",
            "No broker order API is enabled.",
        ],
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "NSEPaperDashboard/1.0"

    def _send(self, code, content_type, payload):
        body = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(build_status(self.server.root), sort_keys=True))
            return
        if path in ("/", "/index.html"):
            try:
                self._send(200, "text/html; charset=utf-8", HTML_PATH.read_text(encoding="utf-8"))
            except OSError:
                self._send(500, "text/plain; charset=utf-8", "dashboard.html is unavailable")
            return
        self._send(404, "text/plain; charset=utf-8", "Not found")

    def log_message(self, format, *args):  # noqa: A002 - stdlib handler API
        return


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve the NSE paper dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.root = ROOT
    print("NSE PAPER DASHBOARD http://%s:%d" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
