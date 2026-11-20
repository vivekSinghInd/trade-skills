"""Angel One instrument-master support for Indian NSE universes.

Angel One publishes a daily instrument master containing exchange, trading
symbol and token metadata. This module normalizes the public file and filters
the NSE cash-equity series. It deliberately does not authenticate, place
orders, or infer liquidity from the instrument master.
"""

from __future__ import annotations

import datetime as _dt
import csv
import gzip
import io
import json
import os
import tempfile
import urllib.request
from pathlib import Path


ANGELONE_MASTER_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)
NIFTY1000_CONSTITUENT_URL = (
    "https://www.niftyindices.com/IndexConstituent/ind_nifty1000_list.csv"
)
USER_AGENT = "day-trader-skill-india/1.0"


class UniverseError(Exception):
    """Expected universe source or schema error."""


def _value(row, names, default=None):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return default


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=None):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "y")


def _exchange(row):
    return str(_value(row, ("exch_seg", "exchange", "exchange_segment"), "")).strip().upper()


def _trading_symbol(row):
    return str(_value(row, ("symbol", "tradingsymbol", "trading_symbol"), "")).strip().upper()


def normalize_instrument(row):
    """Normalize one Angel One row without discarding broker metadata."""
    if not isinstance(row, dict):
        raise UniverseError("instrument rows must be objects")
    trading_symbol = _trading_symbol(row)
    exchange_segment = _exchange(row)
    if not trading_symbol or not exchange_segment:
        raise UniverseError("instrument row needs symbol and exchange segment")
    series = trading_symbol.rsplit("-", 1)[-1] if "-" in trading_symbol else ""
    raw_tick = _float(_value(row, ("tick_size_paise", "tick_size", "ticksize")))
    if raw_tick is None:
        tick_inr = _float(_value(row, ("tick_size_inr",)))
        if tick_inr is not None:
            raw_tick = tick_inr * 100.0
    # Angel One's master represents equity ticks in paise, for example 5.0
    # for a five-paise tick. Preserve both units for later broker integration.
    tick_inr = raw_tick / 100.0 if raw_tick is not None else None
    return {
        "token": str(_value(row, ("token", "symboltoken", "symbol_token"), "")).strip(),
        "trading_symbol": trading_symbol,
        "base_symbol": trading_symbol.rsplit("-", 1)[0],
        "name": str(_value(row, ("name",), "")).strip().upper(),
        "exchange_segment": exchange_segment,
        "series": series,
        "instrument_type": str(_value(row, ("instrumenttype", "instrument_type"), "")).strip().upper(),
        "expiry": str(_value(row, ("expiry",), "")).strip(),
        "strike": _float(_value(row, ("strike",), "-1"), -1.0),
        "lot_size": _int(_value(row, ("lotsize", "lot_size"), "1"), 1),
        "tick_size_paise": raw_tick,
        "tick_size_inr": tick_inr,
        "is_cas_enabled": _bool(_value(row, ("is_cas_enabled",), False)),
    }


def normalize_rows(rows):
    """Normalize a complete master and skip malformed non-tradable rows."""
    if not isinstance(rows, list):
        raise UniverseError("instrument master must be a JSON list")
    result = []
    for row in rows:
        try:
            item = normalize_instrument(row)
        except UniverseError:
            continue
        if item["token"]:
            result.append(item)
    if not result:
        raise UniverseError("instrument master contains no usable rows")
    return result


def nse_cash(rows, include_be=False):
    """Return one row per NSE cash-equity base symbol.

    EQ is the default series. BE may be included explicitly for trade-to-trade
    securities. F&O, indices and other non-cash segments are excluded here.
    The Angel One master does not consistently label ETFs separately, so EQ
    output may include ETFs and should be reviewed with a separate security
    master when a stock-only universe is required. Broker test instruments
    containing ``NSETEST`` are excluded even when they are labelled EQ.
    """
    allowed = {"EQ"}
    if include_be:
        allowed.add("BE")
    selected = {}
    for item in normalize_rows(rows):
        if item["exchange_segment"] not in ("NSE", "NSE_CM", "NSE-CM"):
            continue
        if item["series"] not in allowed:
            continue
        if "NSETEST" in item["base_symbol"]:
            continue
        selected.setdefault(item["base_symbol"], item)
    return [selected[key] for key in sorted(selected)]


def normalize_index_symbol(value):
    """Normalize an exchange/index symbol to the Angel One base-symbol form."""
    symbol = str(value or "").strip().upper()
    if not symbol:
        return ""
    # Nifty constituent files normally contain bare symbols, but tolerate
    # Yahoo-style and broker-series suffixes when a cached/custom file uses
    # them. Do not alter legitimate hyphens inside a symbol.
    if symbol.endswith(".NS"):
        symbol = symbol[:-3]
    if symbol.endswith("-EQ") or symbol.endswith("-BE"):
        symbol = symbol.rsplit("-", 1)[0]
    return symbol.strip()


def parse_nifty1000_constituents(payload):
    """Parse the official Nifty 1000 CSV and return normalized symbols."""
    if isinstance(payload, str):
        text = payload
    else:
        try:
            text = bytes(payload).decode("utf-8-sig")
        except (TypeError, UnicodeDecodeError) as exc:
            raise UniverseError("Nifty 1000 constituent file is not UTF-8 CSV: %s" % exc)
    try:
        reader = csv.DictReader(io.StringIO(text))
        headers = [str(header or "").strip().lower() for header in (reader.fieldnames or [])]
        candidates = ("symbol", "index symbol", "ticker", "security symbol", "scrip code")
        symbol_header = next((reader.fieldnames[index]
                              for index, header in enumerate(headers)
                              if header in candidates), None)
        if symbol_header is None:
            raise UniverseError("Nifty 1000 CSV has no symbol column")
        symbols = set()
        for row in reader:
            symbol = normalize_index_symbol(row.get(symbol_header))
            if symbol:
                symbols.add(symbol)
    except csv.Error as exc:
        raise UniverseError("Nifty 1000 constituent CSV is invalid: %s" % exc)
    if not symbols:
        raise UniverseError("Nifty 1000 constituent CSV contains no symbols")
    return sorted(symbols)


def download_nifty1000_constituents(url=NIFTY1000_CONSTITUENT_URL, timeout=60):
    """Download and validate the official Nifty 1000 constituent CSV."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/csv,text/plain,*/*",
        "Accept-Encoding": "identity",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except Exception as exc:
        raise UniverseError("Nifty 1000 constituent file unavailable: %s" % exc)
    if len(payload) < 20:
        raise UniverseError("Nifty 1000 constituent response is unexpectedly small")
    return parse_nifty1000_constituents(payload)


def read_nifty1000_constituents(path):
    """Read a cached Nifty 1000 CSV."""
    target = Path(path)
    try:
        with open(target, "rb") as handle:
            return parse_nifty1000_constituents(handle.read())
    except OSError as exc:
        raise UniverseError("cannot read Nifty 1000 constituent cache %s: %s" % (target, exc))


def write_nifty1000_constituents(path, symbols):
    """Write a validated constituent cache atomically."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = sorted({normalize_index_symbol(symbol) for symbol in symbols if normalize_index_symbol(symbol)})
    if not normalized:
        raise UniverseError("cannot cache an empty Nifty 1000 constituent list")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", 
                                         dir=str(target.parent), delete=False) as handle:
            temp_name = handle.name
            writer = csv.writer(handle)
            writer.writerow(["Symbol"])
            writer.writerows((symbol,) for symbol in normalized)
        os.replace(temp_name, str(target))
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return str(target)


def filter_symbols(rows, symbols, include_be=False):
    """Intersect Angel One NSE cash rows with an explicit symbol set."""
    wanted = {normalize_index_symbol(symbol) for symbol in symbols}
    wanted.discard("")
    if not wanted:
        raise UniverseError("Nifty 1000 constituent set is empty")
    selected = [row for row in nse_cash(rows, include_be)
                if row["base_symbol"] in wanted]
    if not selected:
        raise UniverseError("Nifty 1000 constituents do not match the NSE instrument master")
    return selected


def filter_pattern(rows, pattern=None):
    if not pattern:
        return list(rows)
    needle = str(pattern).strip().upper()
    return [row for row in rows if needle in row["base_symbol"] or needle in row["name"]]


def decode_master_bytes(payload):
    """Decode JSON or gzip-compressed JSON returned by the public endpoint."""
    if payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload)
        except OSError as exc:
            raise UniverseError("Angel One master gzip is invalid: %s" % exc)
    try:
        document = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UniverseError("Angel One master is not valid JSON: %s" % exc)
    return normalize_rows(document)


def read_master(path):
    target = Path(path)
    try:
        with open(target, "rb") as handle:
            return decode_master_bytes(handle.read())
    except OSError as exc:
        raise UniverseError("cannot read instrument cache %s: %s" % (target, exc))


def write_master(path, rows):
    """Write a validated master atomically so a partial download is not cached."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = [dict(row) for row in rows]
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=str(target.parent), delete=False) as handle:
            temp_name = handle.name
            json.dump(document, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temp_name, str(target))
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return str(target)


def download_master(url=ANGELONE_MASTER_URL, timeout=60):
    """Download and validate the current public Angel One instrument master."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except Exception as exc:
        raise UniverseError("Angel One instrument master unavailable: %s" % exc)
    if len(payload) < 100:
        raise UniverseError("Angel One instrument master response is unexpectedly small")
    return decode_master_bytes(payload)


def universe_document(rows, source, include_be=False, pattern=None, fetched_at=None,
                      allowed_symbols=None, index_name=None, index_source=None,
                      index_fetched_at=None):
    """Build the stable JSON contract consumed by the Indian scanners."""
    selected = (filter_symbols(rows, allowed_symbols, include_be)
                if allowed_symbols is not None else nse_cash(rows, include_be))
    selected = filter_pattern(selected, pattern)
    document = {
        "market": "NSE",
        "exchange_segment": "NSE_CM",
        "currency": "INR",
        "source": source,
        "fetched_at": fetched_at or _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "series": ["EQ", "BE"] if include_be else ["EQ"],
        "count": len(selected),
        "symbols": [row["base_symbol"] for row in selected],
        "instruments": selected,
    }
    if index_name:
        document["index"] = index_name
    if index_source:
        document["index_source"] = index_source
    if index_fetched_at:
        document["index_fetched_at"] = index_fetched_at
    return document


def write_document(path, document):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return str(target)
