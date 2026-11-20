# The Day Trader skill

11 Claude skills for day trading: scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a dollar.

Free, MIT, no signup, no API key. The tools are plain Python that read free
public data: Yahoo Finance charts, Nasdaq Trader's symbol lists, SEC EDGAR
and the House Clerk.

One scans thousands of charts for setups. Another tracks breaking news on
every stock you watch. One watches a setup bar by bar and tells you the moment
to get in, and another sets your stop and your target before you do. And the
craziest one is the Time Machine: before you risk a dollar, it replays the
rest of the session 10,000 times from the stock's own recent bars, plus every
past day the same setup fired, and tells you the honest odds.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

For the current NSE intraday handover, verified status, and next-session checklist, see [HANDOVER.md](HANDOVER.md).

> **Looking for the original Trader skill?** Copy an investor's 13F, the
> stock screener, tax lots and rebalancing live in
> [github.com/Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill).
> This repo is its day-trading sequel: the Trader skill v2.

## Install

Paste this into Claude:

```
https://github.com/Jakeschincariol/day-trader-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add Jakeschincariol/day-trader-skill
/plugin install day-trader-skill@day-trader-skill
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/day-trader-skill:trader-scanner` and so on. Copy the folders instead if you want
plain `/trader-scanner`:

```bash
git clone https://github.com/Jakeschincariol/day-trader-skill.git
cp -r day-trader-skill/trader-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. No Claude Code at all? Paste any single `SKILL.md` at the
top of a chat and it runs as a mode. You lose the Python tools, but the method
works.

The tools need Python 3.8 or newer. Nothing to pip install. The SEC tools
(news filings, the analyst) ask you to identify yourself: put your name and
email in `trader/config.json` (`"sec_user_agent": "Your Name you@example.com"`)
or in the `SEC_USER_AGENT` environment variable.

## India intraday extension

The `trader-universe` and `trader-intraday` folders on this branch add an
Indian NSE workflow for long-only intraday cash-equity research. The universe
syncs Angel One's public instrument master and filters NSE cash equities. The
daily runner defaults to the official Nifty 1000 profile, intersected with the
current Angel One NSE EQ master; use `--full-universe` for all cached NSE EQ
symbols.
Intraday uses timestamped bars, IST/NSE session conventions and an
opening-range example. It now includes a full-universe scanner, a read-only
NSE corporate-announcement monitor, a completed-bar watcher, paper alerts,
portfolio cash, aggregate open-risk and daily-loss gating, a persistent paper
journal, a strict preflight report, exchange-calendar checks, and a U.S.-style
plan time machine plus multi-day replay. `daily_runner.py` now ranks setups by
relative breakout quality, rescans completed 5-minute bars, retries delayed
entry bars, and recycles capacity after exits. Its default data split is Yahoo
for breakout scanning/selection and Angel One LTP for live paper monitoring.
It orchestrates those stages as one restart-safe paper process, and
`dashboard.py` serves a read-only live view. It is paper-only.
The original eleven skills remain the U.S.-market baseline; this extension does
not silently reinterpret their U.S. data or session rules. Indian plans default
to a maximum ₹10,000 notional per trade, in addition to the maximum-loss rule.

```bash
python3 trader-universe/universe.py sync
python3 trader-universe/universe.py sync --index nifty1000 \
  --out trader/nifty1000_universe.json
python3 trader-universe/universe.py list --limit 50
python3 trader-intraday/intraday.py scan --symbol RELIANCE \
  --csv data/reliance-5m.csv
python3 trader-intraday/intraday.py scan --symbol RELIANCE \
  --yahoo --interval 5m --range 1d --day 2026-11-13 --json
python3 trader-intraday/intraday.py scan --symbol RELIANCE \
  --angelone --credentials-file D:\\MyStockMarketTool\\ScreenEdge.Api\\appsettings.json \
  --universe trader/nse_universe.json --day 2026-11-13 --json
python3 trader-intraday/nse_news.py --symbols RELIANCE INFY --hours 24 --json
python3 trader-intraday/scan.py --universe trader/nse_universe.json \
  --yahoo --day 2026-11-13 --write
python3 trader-intraday/daily_runner.py --yahoo --day 2026-11-16 \
  --capital 100000 --max-loss 1000 --max-trade-value 10000 \
  --max-open-risk 3000 --rescan-every-minutes 5
python3 trader-intraday/daily_runner.py --nifty1000 --yahoo --day 2026-11-16 \
  --capital 100000 --max-loss 1000 --max-trade-value 10000 \
  --max-open-risk 3000 --rescan-every-minutes 5
python3 trader-intraday/dashboard.py --host 127.0.0.1 --port 8765
python3 trader-intraday/watch.py --symbol RELIANCE --yahoo --day 2026-11-13
python3 trader-intraday/alerts.py --plan trader/intraday_plan.json \
  --offline data/today --now 2026-11-13T15:20:00+05:30 --json
python3 trader-intraday/batch_backtest.py --csv-dir data/nse-5m --json
python3 trader-intraday/replay.py --csv-dir data/nse-history --iterations 10000 --json
python3 trader-intraday/replay.py --csv data/reliance-5m-history.csv \
  --plan trader/intraday_plan.json --replays 10000 --window 2 --json
python3 trader-intraday/preflight.py --day 2026-11-16 \
  --universe trader/nse_universe.json --json
python3 trader-intraday/paper_journal.py status --json
```

The default strategies are replaceable examples, not investment advice and not
the user's later options strategy. Options, broker execution, shorting and
margin are intentionally outside this Indian-market slice. Stock options
should be added only as a separate layer on top of a verified underlying
signal and the user's written strategy.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans thousands of charts for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Tracks breaking news on every stock you watch and flags what moves it. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter, so you know when to get out. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a dollar: 10,000 simulated sessions plus every past time the setup fired. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade. |
| `/trader-broker` | Order Executor | Sends approved orders to your broker, paper trading by default, never without your yes. |
| `/trader-alerts` | Price Alerts | Rings when a price, a stop or the opening bell hits. |
| `/trader-analyst` | Earnings Analyst | Reads earnings reports and sums up what changed. |
| `/trader-pelosi` | Pelosi Tracker | Reads Congress's stock-trade disclosures the minute they are filed. |

## The India extension

| command | job | what it does |
| --- | --- | --- |
| `/trader-intraday` | NSE Intraday Research | Reads completed IST bars, scans an opening-range breakout, and creates an INR paper plan. |
| `/trader-universe` | NSE Universe | Caches Angel One's instrument master and writes the NSE EQ symbol universe with broker tokens. |
| NSE corporate announcements | NSE News | Reads exchange announcements, tags them for review and remembers seen items. |
| NSE scanner | Universe Scanner | Runs the intraday rule over the cached NSE universe and records hits, no-setups and data errors. |
| NSE watcher | Entry Timer | Polls completed bars for one symbol until the long-only setup triggers or expires. |
| NSE alerts | Price Alerts | Checks paper stop, target and flat-by events from completed bars. |
| NSE portfolio gate | Risk and Budget | Applies cash, ₹10,000 trade notional, aggregate open stop-risk, position-count and daily-loss limits to paper trades. |
| NSE journal | Paper State | Persists simulated entries, exits, cash, open positions and realized daily P&L. |
| NSE preflight | Readiness Gate | Checks calendar, universe freshness, limits, credentials and completed-bar quality. |
| NSE replay | Time Machine | Replays a plan through same-symbol historical candle paths and prior ORB sessions. |
| NSE daily runner | End-to-end paper workflow | Refreshes the universe, scans the full universe, checks news, ranks relative setup quality, retries candidates every completed 5-minute cycle, sizes paper entries and monitors exits with restart-safe state. |
| NSE dashboard | Live paper view | Shows workflow stages, scan/news counts, selection/rejection counts, open-risk budget, paper positions, marks, P&L and journal exits; refreshes every five seconds. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by the close                                       (while it is open)
news / analyst / pelosi                                                        (on a schedule)
```

The NSE paper flow is separate:

```
universe sync -> NSE announcements -> NSE universe scan -> one-symbol watcher
-> paper plan -> plan time machine -> completed-bar alerts -> paper journal
-> portfolio gate -> multi-day replay
```

The NSE announcement reader uses the exchange's corporate-filings feed for
disclosures, not a sentiment or recommendation model. Open and verify each
filing before treating it as relevant to a trade.

An example, the trade in the reel: Tesla, Monday October 19 2026, real data.

1. **`/trader-scanner`** at 10:15 AM ET: of the 231 charts in the reel's
   scan, 42 had closed above their 15-minute opening range. TSLA is one: range 354.47 to 362.18,
   the 10:10 candle closed at 364.18, 0.55 % over the top.
2. **`/trader-news`**: the morning's TSLA headlines, in New York time
   ("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", 8:04,
   tagged earnings and deal). Tesla reports this week: a day trade is out
   before the close, so it is not exposed to the report.
3. **`/trader-entry`** watched TSLA bar by bar. At 10:12 it said waiting (the
   10:10 bar was still forming). At 10:15 it said triggered: the first
   5-minute close above the range, 364.18.
4. **`/trader-exit`** set the plan before the entry: stop at the range's
   midpoint, 358.32, so 5.86 a share at risk; target one times that risk,
   370.04; flat by 3:55 PM either way.
5. **`/trader-timemachine`** replayed the rest of the day 10,000 times, each
   5-minute candle drawn from TSLA's own candles at that time of day over the
   previous 59 sessions (seed 19, so anyone can repeat it): target first
   29.4 %, stop first 25.5 %, still open at the close 45.1 %. It also re-ran
   the same rule on those 59 real sessions: it fired 33 times, 13 hit the
   target, 15 the stop, 5 neither. Its verdict: close to a coin flip. That is
   the point of looking before you risk a dollar.
6. **`/trader-budget`** with a $5,000 account and a $50 max loss per trade:
   50 / 5.86 = 8 shares ($2,913.44). If the stop fills at 358.32, the loss is
   $46.88. It writes one bracket order: buy 8 TSLA, stop-loss 358.32,
   take-profit 370.04.
7. **`/trader-risk`** checks it against your limits: the max loss, the daily
   loss limit, trades per day, reward to risk, the entry window, position
   size and cash. PASS or BLOCK, with the reason.
8. **`/trader-broker`** shows you the ticket in plain words. You type yes for
   that exact order. Claude calls your brokerage connector (Alpaca's MCP
   server, paper account by default), and Claude's own "Allow once / Deny"
   card asks you one more time.
9. **`/trader-alerts`** rings on the stop, the target and at 3:55 PM to
   flatten whatever is still open.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 trader-scanner/scan.py universe                                # ~5,900 US stocks, Nasdaq Trader
python3 trader-scanner/scan.py daily                                   # pre-market setups, whole universe
python3 trader-scanner/scan.py orb --at 10:15                          # opening-range breakouts so far
python3 trader-news/news.py --tickers TSLA --hours 12                  # headlines, 8-Ks, today's move
python3 trader-entry/entry.py check TSLA --setup orb                   # waiting / triggered / expired
python3 trader-entry/entry.py watch TSLA --every 30                    # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by close
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-analyst/facts.py TSLA                                   # quarters, growth, EPS
python3 trader-pelosi/ptr.py index --member pelosi                     # her PTRs this year
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real data: TSLA's 5-minute bars
for the 60 days to October 19 2026, the 10:15 AM opening-range scan, Yahoo's
TSLA headlines that morning, the House Clerk's 2026 index, real PTR text and
real XBRL facts. They reproduce every number in the example above.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money. You can lose money. Decide for
yourself, and talk to a licensed professional if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. Set your brokerage tool to "Needs approval" in Claude
(Customize, Connectors, tool permissions), never "Always allow", so Claude's
own "Allow once / Deny" card is a second check. No options, margin, short
selling or leverage unless you turn them on.

**Paper first.** `"paper": true` is the default in `config.example.json`, and
Alpaca's MCP server defaults to its paper account. Keys live in environment
variables, never in the chat.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: an earnings report, a halt, a headline. Treat its odds as a
reality check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market or across a gap, a
stop order can fill well past its price. The max loss shown is the loss at
the stop price.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API: it can lag, change or rate-limit you. The scanner and the entry
timer read 1-minute or 5-minute bars, not every tick. Check anything that
matters against your broker before you act on it.

**Day-trading rules.** FINRA removed the $25,000 pattern-day-trader minimum
on June 4, 2026 (Regulatory Notice 26-10), and brokers may phase the change
in until October 20, 2027, so yours may still apply the old rule. If it does,
set `"pdt_guard": true` and `/trader-risk` counts your day trades.

**Congress's trades are public, not inside information.** Members must disclose
trades over $1,000 within 45 days (the STOCK Act). The Pelosi skill reads those
public filings the minute they post.

**Taxes.** Day-trading gains are short-term gains, and frequent trading makes
wash sales likely. Nothing here is tax advice.

## Files

```
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
trader-pelosi/      SKILL.md, ptr.py
india_market.py      shared IST, NSE session, symbol and tick conventions
nse_calendar.py      dated NSE equity holidays and unknown-calendar guard
india_universe.py    Angel One master normalization and NSE cash filtering
trader-intraday/     SKILL.md, intraday.py, config.example.json
                      nse_news.py, scan.py, watch.py, alerts.py,
                      portfolio.py, paper_journal.py, paper_monitor.py,
                      daily_runner.py, dashboard.py, dashboard.html,
                      preflight.py,
                      batch_backtest.py, replay.py
trader-universe/     SKILL.md, universe.py
tests/              unittest suite + offline fixtures
```

MIT. Made by [Jake Schincariol](https://opusjake.ai).
