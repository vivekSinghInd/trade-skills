#!/usr/bin/env python3
"""Read-only NSE corporate-announcement monitor for the intraday workflow.

The NSE web endpoint is an exchange disclosure feed, not a recommendation
engine.  This module keeps a small seen-state file, tags announcements for
review, and never changes a signal or places an order automatically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from india_market import IST


NSE_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-application"
NSE_API = "https://www.nseindia.com/api/corporate-announcements?index=equities"
YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"
USER_AGENT = "day-trader-skill-india/1.0"


class NSENewsError(Exception):
    """Expected NSE news or state error."""


TAG_PATTERNS = (
    ("earnings", r"result|financial|profit|loss|revenue|quarter|audited"),
    ("board", r"board meeting|board of directors|meeting intimation"),
    ("management", r"appointment|resignation|director|key managerial|management"),
    ("deal", r"order|contract|award|acquisition|merger|joint venture|partnership"),
    ("capital", r"fund raising|preferential|rights issue|buyback|dividend|bonus"),
    ("rating", r"credit rating|rating revision|downgrade|upgrade"),
    ("legal", r"regulatory|legal|fraud|investigation|penalty|notice|sebi"),
    ("halt", r"trading halt|suspension|surveillance|asm|gsm"),
)


def _value(row, names):
    if not isinstance(row, dict):
        return None
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return str(value).strip()
    return None


def _parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text.replace(" IST", "")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=IST)
            return parsed.astimezone(IST)
        except ValueError:
            pass
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%m-%Y %H:%M:%S",
                "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return None


def _absolute_url(value):
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return urllib.parse.urljoin("https://www.nseindia.com", value)


def tags_for(subject, details=""):
    text = (str(subject or "") + " " + str(details or "")).lower()
    tags = [tag for tag, pattern in TAG_PATTERNS if re.search(pattern, text)]
    return tags or ["general"]


def normalize_announcement(row):
    """Normalize one NSE API row without retaining arbitrary response data."""
    symbol = (_value(row, ("symbol", "ticker", "sm_symbol")) or "").upper()
    subject = _value(row, ("subject", "desc", "description", "purpose")) or ""
    details = _value(row, ("details", "text", "attachment")) or ""
    broadcast = _value(row, ("broadcastDate", "broadcast_date", "date", "timestamp"))
    attachment = _value(row, ("attchmntFile", "attachmentFile", "url", "link"))
    identifier = _value(row, ("seq_id", "seqId", "id", "symbol", "attchmntFile"))
    if not identifier:
        identifier = hashlib.sha256(
            (symbol + "|" + subject + "|" + str(broadcast or "") + "|" + str(attachment or "")).encode("utf-8")
        ).hexdigest()
    parsed = _parse_time(broadcast)
    return {
        "id": str(identifier),
        "symbol": symbol,
        "subject": subject,
        "broadcast_at": parsed.isoformat() if parsed else str(broadcast or ""),
        "url": _absolute_url(attachment),
        "tags": tags_for(subject, details),
        "source": "NSE corporate announcements",
    }


def parse_announcements(document, symbols=None, since=None):
    """Parse the list-shaped or data-wrapped NSE response."""
    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict):
        rows = document.get("data") or document.get("announcements") or []
    else:
        raise NSENewsError("NSE announcements response is not an object or list")
    wanted = {str(item).upper() for item in (symbols or ()) if str(item).strip()}
    result = []
    for row in rows:
        item = normalize_announcement(row)
        if wanted and item["symbol"] not in wanted:
            continue
        parsed = _parse_time(item["broadcast_at"])
        if since and parsed and parsed < since:
            continue
        result.append(item)
    return sorted(result, key=lambda item: item["broadcast_at"], reverse=True)


def parse_yahoo_rss(document, symbol, since=None):
    """Normalize Yahoo Finance headlines for an NSE Yahoo symbol."""
    try:
        root = ET.fromstring(document)
    except (ET.ParseError, TypeError, ValueError) as exc:
        raise NSENewsError("Yahoo Finance returned invalid RSS") from exc
    result = []
    for row in root.findall(".//item"):
        title = (row.findtext("title") or "").strip()
        link = (row.findtext("link") or "").strip()
        guid = (row.findtext("guid") or link or title).strip()
        published = (row.findtext("pubDate") or "").strip()
        parsed = None
        if published:
            try:
                parsed = parsedate_to_datetime(published).astimezone(IST)
            except (TypeError, ValueError, OverflowError):
                parsed = _parse_time(published)
        if since and parsed and parsed < since:
            continue
        if not title:
            continue
        identifier = hashlib.sha256(("yahoo|" + str(symbol).upper() + "|" + guid).encode("utf-8")).hexdigest()
        result.append({
            "id": identifier, "symbol": str(symbol).upper(), "subject": title,
            "broadcast_at": parsed.isoformat() if parsed else published,
            "url": link or None, "tags": tags_for(title),
            "source": "Yahoo Finance headlines",
        })
    return sorted(result, key=lambda item: item["broadcast_at"], reverse=True)


def _fetch_json(url=NSE_API, timeout=20):
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    common = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
              "Accept": "application/json,text/plain,*/*"}
    try:
        opener.open(urllib.request.Request(NSE_PAGE, headers=common), timeout=timeout).read(1)
        headers = dict(common)
        headers["Referer"] = NSE_PAGE
        with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise NSENewsError("NSE announcements are unavailable") from exc


def _fetch_text(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                    "Accept": "application/rss+xml,application/xml,text/xml,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise NSENewsError("news feed is unavailable") from exc


def load_state(path):
    target = Path(path)
    if not target.is_file():
        return {"seen": {}}
    try:
        with open(target, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError) as exc:
        raise NSENewsError("cannot read NSE news state") from exc
    return state if isinstance(state, dict) else {"seen": {}}


def collect_new(items, state, now=None):
    now = now or dt.datetime.now(IST)
    seen = state.setdefault("seen", {})
    fresh = []
    for item in items:
        if item["id"] not in seen:
            fresh.append(item)
            seen[item["id"]] = now.isoformat()
    cutoff = now - dt.timedelta(days=30)
    state["seen"] = {
        key: value for key, value in seen.items()
        if (_parse_time(value) is None or _parse_time(value) >= cutoff)
    }
    state["updated_at"] = now.isoformat()
    return fresh


def write_state(path, state):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return str(target)


def _parser():
    parser = argparse.ArgumentParser(description="NSE corporate-announcement monitor")
    parser.add_argument("--symbols", nargs="*", help="NSE symbols; default is all returned announcements")
    parser.add_argument("--source", choices=("nse", "yahoo", "both"), default="both")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--state", default=os.path.join("trader", "state", "nse_news.json"))
    parser.add_argument("--url", default=NSE_API, help="NSE announcements endpoint")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--all", action="store_true", help="show already-seen announcements too")
    parser.add_argument("--write", action="store_true", help="persist seen-state")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        since = dt.datetime.now(IST) - dt.timedelta(hours=args.hours)
        items = []
        errors = []
        if args.source in ("nse", "both"):
            try:
                items.extend(parse_announcements(_fetch_json(args.url), args.symbols, since))
            except NSENewsError as exc:
                errors.append("NSE: %s" % exc)
        if args.source in ("yahoo", "both"):
            for symbol in args.symbols or ():
                try:
                    items.extend(parse_yahoo_rss(_fetch_text(YAHOO_RSS % urllib.parse.quote(symbol + ".NS")),
                                                symbol, since))
                except NSENewsError as exc:
                    errors.append("Yahoo %s: %s" % (symbol, exc))
        items.sort(key=lambda item: item["broadcast_at"], reverse=True)
        state = load_state(args.state)
        fresh = items if args.all else collect_new(items, state)
        document = {"market": "NSE", "mode": "news", "source": args.source,
                    "symbols": [str(item).upper() for item in (args.symbols or ())],
                    "items": fresh, "errors": errors, "status": "PARTIAL" if errors else "OK",
                    "execution_enabled": False}
        if args.write or not args.all:
            write_state(args.state, state)
        if args.json:
            print(json.dumps(document, indent=2, sort_keys=True))
        elif fresh:
            for item in fresh:
                print("%s  %s  [%s]" % (item["symbol"], item["subject"], ",".join(item["tags"])))
        else:
            print("NO_NEW_NSE_NEWS")
        return 2 if errors and not fresh else 1 if errors else 0
    except NSENewsError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
