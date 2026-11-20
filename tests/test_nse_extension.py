import datetime as dt
import sys
import unittest

from _load import load


intraday = load("trader-intraday", "intraday")
sys.modules["intraday"] = intraday
portfolio = load("trader-intraday", "portfolio")
sys.modules["portfolio"] = portfolio
news = load("trader-intraday", "nse_news")
alerts = load("trader-intraday", "alerts")
replay = load("trader-intraday", "replay")
scan = load("trader-intraday", "scan")
watch = load("trader-intraday", "watch")
preflight = load("trader-intraday", "preflight")
journal = load("trader-intraday", "paper_journal")
sys.modules["alerts"] = alerts
sys.modules["paper_journal"] = journal
monitor = load("trader-intraday", "paper_monitor")
sys.modules["paper_monitor"] = monitor
sys.modules["nse_news"] = news
sys.modules["scan"] = scan
daily_runner = load("trader-intraday", "daily_runner")
from nse_calendar import calendar_status


def bar(minute, close, high=None, low=None, opening=None, day="2026-01-05"):
    close = float(close)
    high = float(high if high is not None else close + 1)
    low = float(low if low is not None else close - 1)
    opening = float(opening if opening is not None else close)
    total_minutes = 9 * 60 + minute
    hour, minute_value = divmod(total_minutes, 60)
    stamp = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hour, minute_value))
    return {"timestamp": stamp.isoformat(), "open": opening, "high": high,
            "low": low, "close": close, "volume": 100}


def hit_bars():
    return [
        bar(15, 100, 102, 98), bar(20, 101, 102, 98), bar(25, 99, 102, 98),
        bar(30, 104, 105, 100, 101), bar(35, 104, 105, 103),
        bar(40, 106, 110, 103, 104),
    ]


def history_bars(days):
    rows = []
    for day in days:
        rows.extend([
            bar(15, 100, 102, 98, day=day),
            bar(20, 101, 102, 98, day=day),
            bar(25, 99, 102, 98, day=day),
            bar(30, 104, 105, 100, 101, day=day),
            bar(35, 104, 105, 103, 104, day=day),
        ])
        for minute in range(40, 390, 5):
            rows.append(bar(minute, 104, 105, 103, 104, day=day))
    return rows


class NSEExtension(unittest.TestCase):
    def test_calendar_blocks_exchange_holiday_and_unknown_year(self):
        holiday = calendar_status("2026-11-10")
        unknown = calendar_status("2027-01-04")
        self.assertFalse(holiday["trading_day"])
        self.assertIn("holiday", holiday["reason"].lower())
        self.assertFalse(unknown["trading_day"])
        self.assertFalse(unknown["calendar_known"])

    def test_news_is_tagged_and_deduplicated(self):
        document = {"data": [{
            "symbol": "RELIANCE", "subject": "Quarterly financial results and new order",
            "broadcastDate": "05-Jan-2026 10:00:00", "attchmntFile": "/filing.pdf",
            "seq_id": "1",
        }]}
        items = news.parse_announcements(document, ["RELIANCE"])
        self.assertEqual(items[0]["symbol"], "RELIANCE")
        self.assertIn("earnings", items[0]["tags"])
        self.assertIn("deal", items[0]["tags"])
        state = {"seen": {}}
        now = dt.datetime(2026, 1, 5, 10, 1, tzinfo=intraday.IST)
        self.assertEqual(len(news.collect_new(items, state, now)), 1)
        self.assertEqual(news.collect_new(items, state, now), [])

    def test_yahoo_headline_is_parsed_for_nse_symbol(self):
        rss = b"""<rss><channel><item><title>RELIANCE announces new contract order</title>
        <link>https://example.test/story</link><guid>story-1</guid>
        <pubDate>Mon, 05 Jan 2026 10:00:00 +0530</pubDate></item></channel></rss>"""
        items = news.parse_yahoo_rss(rss, "RELIANCE")
        self.assertEqual(items[0]["symbol"], "RELIANCE")
        self.assertEqual(items[0]["source"], "Yahoo Finance headlines")
        self.assertIn("deal", items[0]["tags"])

    def test_scanner_collects_hit_from_loader(self):
        result = scan.scan_symbols(
            ["RELIANCE"], lambda symbol: (hit_bars(), "fixture"), "2026-01-05"
        )
        self.assertEqual(len(result["hits"]), 1)
        self.assertEqual(result["hits"][0]["symbol"], "RELIANCE")

    def test_scanner_drops_currently_forming_bar(self):
        result = scan.completed_session_bars(
            hit_bars(), "2026-01-05",
            now=dt.datetime(2026, 1, 5, 9, 42, tzinfo=intraday.IST),
            interval_minutes=5,
        )
        self.assertEqual(result[-1]["timestamp"], "2026-01-05T09:35:00+05:30")

    def test_scanner_does_not_signal_from_forming_breakout_bar(self):
        result = scan.scan_symbols(
            ["RELIANCE"], lambda symbol: (hit_bars(), "fixture"), "2026-01-05",
            now=dt.datetime(2026, 1, 5, 9, 32, tzinfo=intraday.IST),
        )
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["no_setup"][0]["status"], "WAITING")

    def test_portfolio_gate_applies_cash_and_daily_loss(self):
        trades = [
            {"symbol": "AAA", "entry_timestamp": "2026-01-05T09:35:00+05:30",
             "exit_timestamp": "2026-01-05T09:40:00+05:30", "entry": 100,
             "exit": 99, "qty": 100, "gross_pnl": -100, "costs": {"total": 0}},
            {"symbol": "BBB", "entry_timestamp": "2026-01-05T09:45:00+05:30",
             "exit_timestamp": "2026-01-05T09:50:00+05:30", "entry": 100,
             "exit": 101, "qty": 100, "gross_pnl": 100, "costs": {"total": 0}},
        ]
        result = portfolio.allocate_trades(trades, capital=10000, max_trades=3,
                                           max_daily_loss=50, max_trade_value=10000)
        self.assertEqual(len(result["selected"]), 1)
        self.assertEqual(result["rejected"][0]["reason"], "daily loss limit")
        self.assertEqual(result["summary"]["net_pnl_inr"], -100.0)

    def test_alerts_use_stop_first_on_same_bar(self):
        plan = {"symbol": "RELIANCE", "status": "PAPER_SETUP",
                "signal_timestamp": "2026-01-05T09:30:00+05:30",
                "entry": 104, "stop": 101, "target": 120, "flat_by": "15:15"}
        bars = hit_bars() + [bar(45, 105, 111, 100, 106)]
        result = alerts.check_plan(plan, bars, "2026-01-05",
                                   "2026-01-05T10:00:00+05:30", 5)
        self.assertEqual(result["status"], "TRIGGERED")
        self.assertEqual(result["event"], "stop")

    def test_live_ltp_triggers_target_without_completed_yahoo_bar(self):
        plan = {"symbol": "RELIANCE", "status": "PAPER_SETUP",
                "signal_date": "2026-01-05",
                "entry_timestamp": "2026-01-05T09:35:00+05:30",
                "entry": 104, "stop": 101, "target": 110, "flat_by": "15:15"}
        result = alerts.check_live_price(
            plan, 110.25, "2026-01-05", "2026-01-05T10:00:00+05:30")
        self.assertEqual(result["status"], "TRIGGERED")
        self.assertEqual(result["event"], "target")
        self.assertEqual(result["event_price"], 110.25)

    def test_paper_monitor_uses_angelone_ltp_after_selection(self):
        with __import__("tempfile").TemporaryDirectory() as folder:
            path = folder + "/state.json"
            state = journal.new_state("2026-01-05", 100000)
            plan = {"symbol": "RELIANCE", "status": "PAPER_SETUP", "entry": 100,
                    "stop": 90, "target": 120, "qty": 50,
                    "planned_loss_inr": 500, "signal_date": "2026-01-05",
                    "entry_timestamp": "2026-01-05T09:35:00+05:30"}
            journal.enter(state, plan)
            journal.write_state(path, state)
            original_credentials = monitor.AngelOneCredentials.from_environment
            original_ltp = monitor.load_angelone_ltp
            monitor.AngelOneCredentials.from_environment = classmethod(
                lambda cls: cls("api", "client", "password",
                                "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"))
            monitor.load_angelone_ltp = lambda *args, **kwargs: {
                "symbol": "RELIANCE", "price": 101.0, "source": "angelone-ltp"
            }
            try:
                report = monitor.check_once(
                    path, "2026-01-05", source="angelone",
                    universe_path="unused",
                    now=dt.datetime(2026, 1, 5, 10, 0, tzinfo=intraday.IST),
                )
            finally:
                monitor.AngelOneCredentials.from_environment = original_credentials
                monitor.load_angelone_ltp = original_ltp
            self.assertEqual(report["results"][0]["source"], "angelone-ltp")
            self.assertEqual(report["results"][0]["last_price"], 101.0)
            self.assertEqual(report["open_positions"], 1)

    def test_bootstrap_is_seeded_and_is_not_a_forecast(self):
        first = replay.bootstrap_daily([10.0, -5.0], iterations=20, seed=19)
        second = replay.bootstrap_daily([10.0, -5.0], iterations=20, seed=19)
        self.assertEqual(first, second)
        self.assertTrue(first["not_a_forecast"])

    def test_plan_time_machine_replays_paths_and_history(self):
        days = []
        cursor = dt.date(2026, 1, 2)
        while len(days) < 22:
            key = cursor.isoformat()
            if cursor.weekday() < 5 and calendar_status(key)["trading_day"]:
                days.append(key)
            cursor += dt.timedelta(days=1)
        bars = history_bars(days)
        plan_day = days[-1]
        plan = {"symbol": "RELIANCE", "signal_date": plan_day,
                "signal_timestamp": plan_day + "T09:30:00+05:30",
                "entry": 104, "stop": 101, "target": 110, "flat_by": "15:15"}
        result = replay.run_plan_replay(bars, plan, replays=100, seed=19,
                                        minimum_sessions=20)
        self.assertEqual(result["mode"], "nse_intraday_time_machine")
        self.assertEqual(result["mc"]["replays"], 100)
        self.assertEqual(result["data"]["prior_sessions"], 21)
        self.assertTrue(result["mc"]["not_a_forecast"])

    def test_paper_journal_carries_cash_and_realized_pnl(self):
        state = journal.new_state("2026-01-05", 10000)
        plan = {"symbol": "RELIANCE", "status": "PAPER_SETUP", "entry": 100,
                "stop": 98, "target": 104, "qty": 50, "planned_loss_inr": 100}
        journal.enter(state, plan)
        self.assertEqual(state["cash_inr"], 5000.0)
        journal.exit_position(state, "RELIANCE", 102, "target")
        self.assertEqual(state["realized_pnl_inr"], 100.0)
        self.assertEqual(state["cash_inr"], 10100.0)

    def test_paper_journal_enforces_aggregate_open_risk(self):
        state = journal.new_state("2026-01-05", 100000)
        first = {"symbol": "AAA", "status": "PAPER_SETUP", "entry": 100,
                 "stop": 90, "target": 120, "qty": 10, "planned_loss_inr": 100}
        second = {"symbol": "BBB", "status": "PAPER_SETUP", "entry": 100,
                  "stop": 90, "target": 120, "qty": 10, "planned_loss_inr": 100}
        journal.enter(state, first, max_open_risk=150)
        with self.assertRaisesRegex(journal.JournalError, "open stop-risk"):
            journal.enter(state, second, max_open_risk=150)

    def test_daily_runner_ranks_by_setup_quality_not_raw_volume_only(self):
        candidates = [
            {"symbol": "LOWVOL", "status": "PAPER_SETUP", "selection_score": 30,
             "breakout_volume_ratio": 2, "breakout_volume": 1000000},
            {"symbol": "STRONG", "status": "PAPER_SETUP", "selection_score": 50,
             "breakout_volume_ratio": 1.5, "breakout_volume": 100000},
            {"symbol": "BLOCKED", "status": "BLOCKED", "reason": "no next bar"},
        ]
        ranked = daily_runner.rank_paper_candidates(candidates)
        self.assertEqual([item["symbol"] for item in ranked[:2]], ["STRONG", "LOWVOL"])
        self.assertEqual(ranked[0]["selection_rank"], 1)
        self.assertNotIn("selection_rank", ranked[2])

    def test_daily_runner_builds_all_candidates_when_limit_is_zero(self):
        hits = []
        for symbol, volume in (("AAA", 1000), ("BBB", 2000)):
            signal = intraday.signal_for(hit_bars(), day="2026-01-05")
            signal.update({"symbol": symbol, "volume": volume, "is_etf": False})
            hits.append(signal)
        candidates = daily_runner.build_paper_candidates(
            hits, {}, lambda symbol: (hit_bars(), "fixture"),
            "2026-01-05", dt.datetime(2026, 1, 5, 10, 0, tzinfo=intraday.IST),
            5, 100000, 1000, 10000, 2, 0, workers=2)
        self.assertEqual({item["symbol"] for item in candidates}, {"AAA", "BBB"})
        self.assertTrue(all("selection_score" in item for item in candidates))

    def test_daily_runner_can_select_nifty1000_profile(self):
        args = daily_runner._parser().parse_args(["--once"])
        self.assertEqual(args.universe_profile, "nifty1000")
        self.assertTrue(args.nifty1000_universe.endswith("nifty1000_universe.json"))
        self.assertEqual(
            daily_runner._parser().parse_args(["--full-universe"]).universe_profile,
            "full")

    def test_paper_monitor_is_read_only_when_positions_wait(self):
        with self.subTest("monitor"), __import__("tempfile").TemporaryDirectory() as folder:
            path = folder + "/state.json"
            state = journal.new_state("2026-01-05", 100000)
            plan = {"symbol": "RELIANCE", "status": "PAPER_SETUP", "entry": 100,
                    "stop": 90, "target": 120, "qty": 50,
                    "planned_loss_inr": 500, "signal_timestamp": "2026-01-05T09:30:00+05:30"}
            journal.enter(state, plan)
            journal.write_state(path, state)
            original_loader = monitor.load_source
            monitor.load_source = lambda *args, **kwargs: (hit_bars(), "fixture")
            try:
                report = monitor.check_once(
                    path, "2026-01-05", source="offline",
                    offline="unused", now=dt.datetime(2026, 1, 5, 10, 0, tzinfo=intraday.IST),
                )
            finally:
                monitor.load_source = original_loader
            self.assertEqual(report["results"][0]["status"], "WAITING")
            self.assertEqual(report["open_positions"], 1)

    def test_preflight_rejects_saved_data_with_gap(self):
        result = preflight.run_preflight(
            "2026-11-13", "trader/nse_universe.json", max_age_hours=1000,
            symbol="RELIANCE", source="offline", offline="trader/yahoo_5m_today",
            now="2026-11-13T16:00:00+05:30",
        )
        self.assertEqual(result["status"], "NOT_READY")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            [check["name"] for check in result["checks"] if check["status"] == "FAIL"],
            ["candles"],
        )

    def test_watcher_drops_incomplete_bar(self):
        now = dt.datetime(2026, 1, 5, 9, 46, tzinfo=intraday.IST)
        result = watch.completed_bars(intraday.normalize_bars(hit_bars()), now, 5)
        self.assertEqual(result[-1]["timestamp"], "2026-01-05T09:40:00+05:30")


if __name__ == "__main__":
    unittest.main()
