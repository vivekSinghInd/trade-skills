---
name: trader-universe
description: >-
  Build and inspect an Indian NSE cash-equity universe from Angel One's public
  SmartAPI instrument master. Cache the daily file, retain tokens and trading
  symbols, filter NSE EQ or BE series, optionally intersect with the official
  Nifty 1000 constituent list, and feed symbols to paper scanners.
argument-hint: "[sync|list] [--include-be]"
---

# Angel One NSE universe

Use this skill to maintain the symbol universe for the Indian intraday
workflow. It downloads Angel One's public instrument master, keeps a local
cache, filters the `NSE_CM` cash-equity segment, and writes a stable JSON file
containing base symbols and Angel One token metadata.

This is an instrument reference, not a market-data feed. The master tells us
which instruments and tokens exist; it does not provide current prices,
volume, liquidity, corporate-action adjustment or a trading recommendation.
The intraday scanner still needs completed intraday bars from a suitable data
source.

## Commands

From the repository root:

```bash
python3 trader-universe/universe.py sync
python3 trader-universe/universe.py sync --index nifty1000 \
  --out trader/nifty1000_universe.json
python3 trader-universe/universe.py list --limit 50
python3 trader-universe/universe.py list --pattern RELIANCE
python3 trader-intraday/intraday.py scan --symbol RELIANCE \
  --angelone --universe trader/nse_universe.json
```

`sync` writes the raw validated master to
`trader/angelone/OpenAPIScripMaster.json` and the filtered universe to
`trader/nse_universe.json`. Use `--json` for machine-readable output,
`--include-be` to include trade-to-trade BE series, and `--pattern` to write a
focused universe. The default is EQ series only.

Use `--index nifty1000` to download NSE Indices' official Nifty 1000
constituents, cache them under `trader/indices/nifty1000.csv`, and intersect
them with the current Angel One NSE EQ master. The generated file records the
index source, constituent timestamp and profile. The official list may contain
symbols absent from the broker master after a corporate-action or symbol
change; those names are reflected in the resulting count rather than silently
invented.

If the Angel One endpoint is temporarily unavailable, `sync` uses an existing
cache and labels the result `angelone-cache-fallback`. Use
`--no-cache-fallback` when a fresh master is mandatory. A cache is not fresh
just because it exists; inspect `fetched_at` and refresh it before a trading
session.

## Angel One fields retained

Each normalized instrument keeps the Angel One token, trading symbol, base
symbol, company name, exchange segment, series, instrument type, expiry,
strike, lot size and tick size. The public master represents the sampled
equity tick size in paise, so the output keeps both `tick_size_paise` and
`tick_size_inr`. The broker/security master remains authoritative if those
values differ for an instrument.

The source URL is the public SmartAPI instrument endpoint. It is not an
authenticated order endpoint and the skill never sends credentials. Angel One
may rate-limit, truncate or change delivery of the large daily file; a
validated atomic cache prevents a partial response from replacing the last
known-good file.

## Filtering and data quality

The default filter is `exch_seg=NSE` or `NSE_CM` and trading symbol suffix
`-EQ`. It excludes BSE, NFO, futures, options, indices and other non-cash
segments. Angel One does not consistently label ETFs separately in this file,
so EQ output may include ETFs; use a separate security master if a stock-only
universe is required. `--include-be` adds the NSE BE series but does not claim
that every BE symbol is suitable for trading. The instrument master has no
reliable liquidity score, so add price, turnover, spread, surveillance and
tradability filters from market data before using a broad universe.

Duplicate base symbols are reduced to one deterministic row. Symbols are
written without the `-EQ` suffix so they can be passed to the intraday data
loader. The Angel One `trading_symbol` and `token` remain available for broker
or SmartAPI market-data integration.

## Safety boundary

This skill is reference-data only and paper-only. It cannot place orders,
authenticate to Angel One, fetch a private account, or send a broker request.
No financial advice is provided. Do not treat a universe membership result as
an endorsement, liquidity guarantee, or signal.

Before using the output, verify the master date, active series, price and
volume data, corporate actions, surveillance status, broker permissions and
the user's risk limits. Stock options are not included by the default filter;
they require a separate expiry, strike, lot-size and liquidity workflow.

Not financial advice. Validate current exchange and broker data before making
any decision, and paper trade while the strategy is being tested.
