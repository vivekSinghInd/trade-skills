#!/usr/bin/env python3
"""Sync and inspect an NSE cash-equity universe from Angel One's master."""

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

from india_universe import (ANGELONE_MASTER_URL, NIFTY1000_CONSTITUENT_URL,
                            UniverseError)
from india_universe import download_master, nse_cash, read_master
from india_universe import (download_nifty1000_constituents,
                            read_nifty1000_constituents, universe_document,
                            write_document, write_master,
                            write_nifty1000_constituents)


DEFAULT_CACHE = os.path.join("trader", "angelone", "OpenAPIScripMaster.json")
DEFAULT_OUT = os.path.join("trader", "nse_universe.json")
DEFAULT_NIFTY1000_CACHE = os.path.join("trader", "indices", "nifty1000.csv")


def _emit(document, as_json=False):
    if as_json:
        print(json.dumps(document, indent=2, sort_keys=True))
        return
    label = document.get("index") or "NSE CASH UNIVERSE"
    print("%s  %d symbols" % (label, document.get("count", 0)))
    print("  source: %s" % document.get("source", ""))
    print("  series: %s" % ", ".join(document.get("series", [])))
    print("  sample: %s" % ", ".join(document.get("symbols", [])[:10]))


def _load_document(path):
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or not isinstance(document.get("symbols"), list):
        raise UniverseError("universe file must contain a symbols list")
    return document


def _parser():
    parser = argparse.ArgumentParser(description="Angel One NSE universe tools")
    sub = parser.add_subparsers(dest="command")

    sync = sub.add_parser("sync", help="download and cache the Angel One master")
    sync.add_argument("--url", default=ANGELONE_MASTER_URL)
    sync.add_argument("--cache", default=DEFAULT_CACHE)
    sync.add_argument("--out", default=DEFAULT_OUT)
    sync.add_argument("--include-be", action="store_true")
    sync.add_argument("--pattern")
    sync.add_argument("--index", choices=("full", "nifty1000"), default="full",
                      help="write the full NSE cash universe or official Nifty 1000")
    sync.add_argument("--constituent-url", default=NIFTY1000_CONSTITUENT_URL)
    sync.add_argument("--constituent-cache", default=DEFAULT_NIFTY1000_CACHE)
    sync.add_argument("--no-cache-fallback", action="store_true")
    sync.add_argument("--json", action="store_true")

    show = sub.add_parser("list", help="inspect a previously written universe")
    show.add_argument("--universe", default=DEFAULT_OUT)
    show.add_argument("--pattern")
    show.add_argument("--limit", type=int, default=25)
    show.add_argument("--json", action="store_true")
    return parser


def sync(args):
    cache = Path(args.cache)
    source = "angelone-instrument-master"
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        rows = download_master(args.url)
        write_master(cache, rows)
    except UniverseError:
        if args.no_cache_fallback or not cache.is_file():
            raise
        rows = read_master(cache)
        source = "angelone-cache-fallback"
        fetched_at = dt.datetime.fromtimestamp(cache.stat().st_mtime,
                                               dt.timezone.utc).isoformat()
    if args.index == "nifty1000":
        index_cache = Path(args.constituent_cache)
        index_source = args.constituent_url
        index_fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            constituents = download_nifty1000_constituents(args.constituent_url)
            write_nifty1000_constituents(index_cache, constituents)
        except UniverseError:
            if args.no_cache_fallback or not index_cache.is_file():
                raise
            constituents = read_nifty1000_constituents(index_cache)
            index_source = "niftyindices-cache-fallback"
            index_fetched_at = dt.datetime.fromtimestamp(
                index_cache.stat().st_mtime, dt.timezone.utc).isoformat()
        document = universe_document(
            rows, source, args.include_be, args.pattern, fetched_at,
            allowed_symbols=constituents, index_name="NIFTY 1000",
            index_source=index_source, index_fetched_at=index_fetched_at)
        document["profile"] = "nifty1000"
    else:
        document = universe_document(rows, source, args.include_be, args.pattern,
                                     fetched_at)
        document["profile"] = "full"
    document["master_cache"] = str(cache)
    write_document(args.out, document)
    _emit(document, args.json)
    if not args.json:
        print("  wrote: %s" % args.out)
    return 0


def show(args):
    document = _load_document(args.universe)
    symbols = document["symbols"]
    if args.pattern:
        needle = args.pattern.upper()
        symbols = [symbol for symbol in symbols if needle in symbol]
    if args.json:
        result = dict(document)
        result["symbols"] = symbols[:max(0, args.limit)]
        result["count"] = len(symbols)
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("%d symbols" % len(symbols))
        print("\n".join(symbols[:max(0, args.limit)]))
    return 0


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "sync":
            return sync(args)
        if args.command == "list":
            return show(args)
        _parser().print_help()
        return 0
    except (UniverseError, OSError, ValueError, TypeError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
