# NSE Intraday Handover

Last updated: 2026-11-16
Timezone: Asia/Kolkata
Repository: `Jakeschincariol-day-trader-skill`

## Current scope

This repository is now the only retained cloned repository. The NSE implementation is intraday-only, long-only, and paper-only.

Intentionally excluded for now:

- Swing trading
- Short selling
- Stock options
- Automatic AngelOne order placement
- Live execution or broker reconciliation

## Configured risk rules

- Capital: INR 100,000
- Maximum loss per trade: INR 1,000
- Maximum daily loss: INR 3,000
- Maximum trade notional: INR 10,000
- Maximum open positions: 10
- Maximum aggregate open stop-risk: INR 3,000
- Maximum entries per day: 20, subject to cash, open-risk and daily-loss gates
- Stop: opening-range midpoint rule
- Target: 2R
- Exit: stop-first, target, or flat by 15:15 IST
- Execution: always paper-only (`execution_enabled: false`)

## Implemented flow

1. Load the official Nifty 1000 profile by default, or the full NSE EQ
   universe when `--full-universe` is explicit; Nifty 1000 is intersected with
   the current AngelOne instrument master.
2. Validate the NSE calendar and reject weekends, holidays, and unknown calendar years.
3. Load 5-minute candles from Yahoo for scanning and candidate selection.
4. Normalize timestamps to IST and reject incomplete or gapped sessions.
5. Scan the selected universe for the opening-range breakout setup.
6. Check NSE corporate filings and Yahoo RSS headlines.
7. Rank candidates by relative breakout volume, breakout strength, reward/risk and entry gap, not raw absolute share volume.
8. Produce paper plans with quantity, entry, stop, target, risk, and notional.
9. Monitor selected positions with Angel One's read-only LTP endpoint for live paper marks and exits.
10. Rescan completed 5-minute bars, retry delayed next-bar candidates, and recycle capacity after exits.
11. Persist paper cash, open positions, realized P&L, and daily limits in the paper journal.
12. Use the time machine for historical replay and seeded same-symbol path simulation.

For normal operation, run the single restart-safe orchestrator:

```powershell
python trader-intraday\daily_runner.py `
  --yahoo --capital 100000 --max-loss 1000 `
  --max-trades 20 --max-open-positions 10 `
  --max-daily-loss 3000 --max-open-risk 3000 `
  --max-trade-value 10000 --rescan-every-minutes 5 `
  --every 30 --until 15:15
```

The current 2026-11-16 paper process was intentionally left running with its
original entries. The new allocator and rescan behavior applies to the next
fresh trading-day run.

The local read-only dashboard is served separately:

```powershell
python trader-intraday\dashboard.py --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. It refreshes every five seconds from the same
paper-run report and journal, showing stages, scan/news counts, last completed
prices, unrealized/realized P&L, paper exits and data errors.

## Verification completed

- Full repository test suite: 348 tests passed.
- Python AST validation: 15 Python files passed.
- Holiday smoke test: 2026-11-10 correctly returned `BLOCKED` for Diwali-Balipratipada.
- Preflight correctly rejected the saved RELIANCE data because it contains one missing 5-minute bar.
- AngelOne instrument master refreshed on 2026-11-14 11:28 IST: 2,763 NSE EQ symbols.
- Broker `NSETEST` instruments are explicitly excluded; refreshed universe contains zero test symbols.
- Official Nifty 1000 profile refreshed on 2026-11-16: 1,000 published
  constituents, 989 currently matched to AngelOne NSE EQ symbols.

## Important current blocker

The saved `trader/yahoo_5m_today/RELIANCE.csv` data is not ready for a paper session:

- 74 completed bars
- Last bar: 2026-11-13 15:25 IST
- One timeline gap detected
- Preflight result: `NOT_READY`

Do not treat this file as a valid session until the missing bar is repaired or the data is re-fetched and passes preflight.

## Next-session checklist

The next fresh NSE session is the next exchange trading day after the current
paper session. Start the new runner with the command above only after the
previous day's paper state has been archived or the journal has rolled to the
new trading date.

Before the next session:

1. Refresh the AngelOne instrument master and confirm the universe timestamp.
2. Obtain complete 5-minute data for the selected universe through Yahoo.
3. Run preflight and require `READY_FOR_PAPER`.
4. Run the full-universe scanner only after preflight passes; the daily runner
   will repeat the scan every five minutes on completed bars.
5. Review NSE filings and Yahoo headlines before accepting a paper setup.
6. Let the daily runner allocate entries using the aggregate open-risk gate;
   do not manually add a position to the journal during an active run.
7. Use Yahoo completed bars for scan/selection and Angel One LTP for selected-position monitoring.
8. Exit by stop, target, or 15:15 IST.
9. Reconcile the paper journal after the session.

Example preflight command:

```powershell
python trader-intraday/preflight.py `
  --day 2026-11-16 `
  --universe trader/nse_universe.json `
  --symbol RELIANCE `
  --angelone `
  --credentials-file <local-credentials-file> `
  --json
```

For offline validation instead:

```powershell
python trader-intraday/preflight.py `
  --day 2026-11-16 `
  --universe trader/nse_universe.json `
  --symbol RELIANCE `
  --offline trader/yahoo_5m_today `
  --json
```

## Safety boundaries

- `D:\MyStockMarketTool` is reference code and must not be modified.
- Never print or commit AngelOne credentials, API keys, or TOTP secrets.
- A `READY_FOR_PAPER` result is not authorization for a live order.
- Live execution requires a separate, explicit design and approval.
- The embedded holiday calendar currently covers 2026; future unknown years are deliberately blocked.

## Main files

- `nse_calendar.py`: NSE trading-day and holiday gate
- `india_market.py`: IST session and candle normalization
- `india_universe.py`: NSE/AngelOne universe handling
- `trader-intraday/intraday.py`: signal, sizing, and paper plan
- `trader-intraday/scan.py`: universe scanner
- `trader-intraday/watch.py`: completed-bar watcher
- `trader-intraday/alerts.py`: paper stop/target/flat-by evaluation
- `trader-intraday/nse_news.py`: NSE and Yahoo news intake
- `trader-intraday/preflight.py`: paper-readiness gate
- `trader-intraday/paper_journal.py`: persistent paper state
- `trader-intraday/paper_monitor.py`: completed-bar paper exits and marks
- `trader-intraday/daily_runner.py`: end-to-end restart-safe paper orchestration
- `trader-intraday/dashboard.py` and `dashboard.html`: local real-time view
- `trader-intraday/replay.py`: historical and seeded time-machine replay
- `tests/test_nse_extension.py`: NSE extension coverage
