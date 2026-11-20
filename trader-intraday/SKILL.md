---
name: trader-intraday
description: >-
  Research Indian NSE intraday cash-equity opening-range breakouts from
  completed IST OHLCV bars. Monitor exchange announcements, scan the NSE
  universe, create INR paper plans, check exits and replay saved sessions;
  never place an order.
argument-hint: "[SYMBOL] [--yahoo|--angelone|--csv BARS.csv]"
---

# Indian NSE intraday trading

Use this skill for a paper-only intraday workflow on NSE-listed cash equities.
It is the Indian companion to the original U.S. day-trading modules and keeps
their assumptions separate. The default example uses completed 5-minute bars
and a 15-minute opening range beginning at 09:15 IST.

The workflow is:

1. Maintain the NSE cash universe with `trader-universe`.
2. Read recent exchange corporate announcements with `nse_news.py` and treat
   them as review flags, not automatic trade filters.
3. Load timestamped OHLCV bars from Yahoo Finance for scanning and selection.
4. Normalize timestamps to IST and keep the NSE regular session from 09:15 to
   15:30. Input data must begin at 09:15 for the selected session.
5. Scan one symbol or the full cached universe, then find the first completed
   close above the opening-range high.
6. Watch one symbol using completed bars and create a long-only cash plan with
   the range midpoint as the example stop,
   an R-multiple target, INR sizing and a 15:15 flat-by boundary.
7. Check the plan's stop, target and flat-by events from completed bars, then
   apply portfolio cash, trade-count and daily-loss limits.
8. Run the plan-level time machine: same-symbol historical candle-return paths
   plus every prior ORB session with the same stop-first rule.
9. Backtest or replay with the next bar open, stop-first same-bar handling, costs and
   slippage as explicit estimates.

For a normal session, use `daily_runner.py` instead of manually chaining these
commands. It refreshes the public Angel One instrument master, checks the
calendar and universe age, scans the selected cached EQ universe with Yahoo,
reviews news for the leading non-ETF hits, creates risk-sized paper entries,
writes the journal, and monitors selected positions with Angel One LTP. Yahoo
is the primary scan/selection feed; if a completed Yahoo candle is more than
60 seconds late, the runner automatically uses Angel One's read-only
historical candle endpoint for that symbol and records the fallback source in
the report. Angel One remains the live-price feed after selection.
The daily runner defaults to `--nifty1000`, using the official Nifty 1000
constituents intersected with the current Angel One NSE EQ master. Pass
`--full-universe` to explicitly use all cached NSE EQ symbols.
The portfolio gate uses cash, the ₹10,000 per-trade cap, aggregate open
stop-risk, and the daily loss limit; it does not discard valid setups merely
because they have lower absolute share volume. During market hours the runner
rescans completed 5-minute bars, retries candidates that did not yet have a
next entry bar, and can reuse capacity after an exit. It is
idempotent for a trading day: after a restart it resumes from the existing
journal without re-entering symbols already traded that day.

The opening-range breakout is a transparent example, not a recommendation and
not the user's strategy. Replace the signal and exit logic only after the
user's actual rules, market universe, liquidity constraints and invalidation
conditions have been documented. Do not optimize it into a historical story.

## Commands

From the repository root:

```bash
python3 trader-intraday/intraday.py clock
python3 trader-intraday/intraday.py scan --symbol RELIANCE \
  --csv data/reliance-5m.csv
python3 trader-intraday/intraday.py scan --symbol RELIANCE \
  --yahoo --interval 5m --range 1d --day 2026-11-13 --json
python3 trader-intraday/intraday.py plan --symbol RELIANCE \
  --angelone --credentials-file D:\\MyStockMarketTool\\ScreenEdge.Api\\appsettings.json \
  --universe trader/nse_universe.json --day 2026-11-13 \
  --capital 100000 --max-loss 1000 --write
python3 trader-intraday/intraday.py backtest --symbol RELIANCE \
  --csv data/reliance-5m.csv --write
python3 trader-intraday/nse_news.py --symbols RELIANCE INFY --hours 24 --json
python3 trader-intraday/scan.py --universe trader/nse_universe.json \
  --yahoo --day 2026-11-13 --write
python3 trader-intraday/watch.py --symbol RELIANCE --yahoo --day 2026-11-13
python3 trader-intraday/alerts.py --plan trader/intraday_plan.json \
  --csv data/reliance-5m.csv --now 2026-11-13T15:20:00+05:30 --json
python3 trader-intraday/batch_backtest.py --csv-dir data/nse-5m --json
python3 trader-intraday/replay.py --csv-dir data/nse-history \
  --iterations 10000 --seed 19 --json
python3 trader-intraday/replay.py --csv data/reliance-5m-history.csv \
  --plan trader/intraday_plan.json --replays 10000 --window 2 --json
python3 trader-intraday/preflight.py --day 2026-11-16 \
  --universe trader/nse_universe.json --json
python3 trader-intraday/paper_journal.py status --json
python3 trader-intraday/daily_runner.py --yahoo --day 2026-11-16 \
  --capital 100000 --max-loss 1000 --max-trades 3 \
  --max-daily-loss 3000 --max-trade-value 10000 \
  --every 30 --until 15:15
python3 trader-intraday/dashboard.py --host 127.0.0.1 --port 8765
```

Use `--json` for machine-readable output. Use `--range-minutes` to change the
opening-range length only when the bar interval and strategy specification
support it. The absolute maximum notional is ₹10,000 per trade; the optional
`--max-trade-value` flag can reduce that limit but cannot increase it. The
single-symbol command keeps one signal and plan unambiguous; `scan.py` and
`batch_backtest.py` provide the universe and portfolio views.

`nse_news.py` reads the NSE corporate-announcement endpoint, filters by symbol
and time window, tags subjects for review and stores a 30-day seen-state file.
`watch.py` and `alerts.py` use completed bars only. They print paper status and
write JSON when requested; they do not submit orders. `daily_runner.py` is the
restart-safe end-to-end orchestrator. `dashboard.py` serves the read-only local
dashboard at `http://127.0.0.1:8765` and refreshes its JSON state every five
seconds in the browser.

`replay.py` has two research modes. The multi-day mode runs the rule on each
saved session, applies the same cash and daily limits day by day, and can
bootstrap observed daily results. The plan mode is the closer U.S. Time Machine
equivalent: it samples same-symbol candle returns by time of day and replays
the ORB rule over prior sessions. Both are diagnostics, not forward-looking
probabilities, and plan mode requires at least 20 prior full sessions by
default.

`preflight.py` must report `READY_FOR_PAPER` before a paper session. It blocks
weekends, known NSE holidays and unknown calendar years, and reports stale
universes, invalid limits, missing credentials and incomplete candles.
`paper_journal.py` persists paper entries and exits so daily cash, open
positions and realized loss are not reset when a process restarts.

## Data contract

CSV input must contain `timestamp` or `datetime`, `open`, `high`, `low`,
`close`, and optionally `volume`. ISO timestamps with an offset are converted
to IST; a timestamp without an offset is treated as IST. Epoch timestamps are
treated as UTC. Bars are validated, sorted and deduplicated by timestamp.

Yahoo Finance and Angel One modes fetch completed historical candles for
research. Yahoo may be delayed, incomplete or rate-limited; Angel One requires
the user's own local credentials and a current NSE token universe. Neither mode
places orders. Verify candles, halts, corporate actions, liquidity and the
current exchange session before relying on any result.

Angel One credentials can also be supplied with `ANGELONE_API_KEY`,
`ANGELONE_CLIENT_CODE`, `ANGELONE_PASSWORD` and `ANGELONE_TOTP_SECRET`.
The tool reads a credential file or environment variables in memory only and
does not copy secrets into the repository.

`india_market.py` and `nse_calendar.py` supply the NSE session clock, dated
equity holidays, unknown-year guard, symbol suffixes and price tick rules. The
calendar is based on the exchange's published holiday page and must be
extended from a new NSE calendar before a future year is used. Holiday data and
instrument-specific overrides should come from a dated exchange or broker
security master rather than being guessed by the strategy code.

## Risk and execution boundary

This skill is paper-only. Every plan, alert, scan, preflight, journal and backtest contains
`execution_enabled: false`. No function places an order, connects to a
broker, or sends a live alert. A future broker handoff would require an
explicit yes for the exact ticket and an independent risk check, but this skill
does not create one. No financial advice is provided.

Sizing uses the smaller of the maximum loss and maximum position percentage,
including estimated round-trip charges. A stop is a plan, not a guaranteed
fill. Intraday gaps, halts, spread, queue position and slippage can make the
real loss larger than the displayed estimate.

The current scope is long-only NSE cash equity. Stock options, futures,
shorting, margin and automated execution are excluded. Options should be a
separate layer after the underlying signal, expiry, liquidity, spread and the
user's strategy have been specified and tested independently.

## Review checklist

Confirm the bar interval, timezone, session start, opening-range length,
completed-bar assumption, next-bar entry, stop-first rule, flat-by handling,
cost assumptions and sample period. Review the JSON output and all caveats.
Do not interpret a positive backtest as a forecast or a suitability judgment.

Not financial advice. Active trading can cause rapid losses. Paper trade first,
validate with realistic data, and consult a qualified professional about your
own circumstances.
