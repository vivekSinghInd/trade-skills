import datetime as dt
import sys
import unittest

from _load import ROOT, load

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

intraday = load("trader-intraday", "intraday")


def make_bar(stamp, close, high=None, low=None, opening=None, volume=100):
    close = float(close)
    high = float(high if high is not None else close + 1)
    low = float(low if low is not None else close - 1)
    opening = float(opening if opening is not None else close)
    return {
        "timestamp": stamp.isoformat(),
        "open": opening,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


class IntradayExtension(unittest.TestCase):
    def session(self):
        day = dt.date(2026, 1, 5)
        bars = []
        for minute, close in ((15, 100), (20, 101), (25, 99)):
            bars.append(make_bar(dt.datetime.combine(day, dt.time(9, minute)),
                                 close, high=102, low=98))
        bars.append(make_bar(dt.datetime.combine(day, dt.time(9, 30)), 104,
                             high=105, low=100, opening=101, volume=200))
        bars.append(make_bar(dt.datetime.combine(day, dt.time(9, 35)), 104,
                             high=105, low=103, opening=104, volume=150))
        bars.append(make_bar(dt.datetime.combine(day, dt.time(9, 40)), 106,
                             high=110, low=103, opening=104, volume=150))
        return bars

    def test_opening_range_signal_is_first_completed_breakout(self):
        result = intraday.signal_for(self.session())
        self.assertEqual(result["status"], "HIT")
        self.assertEqual(result["signal_timestamp"], "2026-01-05T09:30:00+05:30")
        self.assertEqual(result["or_high"], 102)

    def test_plan_is_paper_only(self):
        signal = intraday.signal_for(self.session())
        signal["symbol"] = "RELIANCE"
        plan = intraday.plan_from_signal(signal, 100000, 500)
        self.assertFalse(plan["execution_enabled"])
        self.assertEqual(plan["status"], "PAPER_SETUP")
        self.assertTrue(plan["stop"] < plan["entry"] < plan["target"])

    def test_backtest_uses_next_bar_and_target(self):
        result = intraday.backtest_session(
            self.session(), "RELIANCE",
            {"range_minutes": 15, "target_r": 1, "max_position_pct": 20},
            capital=100000, max_loss=500,
        )
        self.assertEqual(result["summary"]["trades"], 1)
        self.assertEqual(result["trades"][0]["outcome"], "target")
        self.assertEqual(result["trades"][0]["entry_timestamp"],
                         "2026-01-05T09:35:00+05:30")

    def test_yahoo_chart_json_is_normalized_to_ist(self):
        document = {
            "chart": {
                "result": [{
                    "timestamp": [1767600900],
                    "indicators": {"quote": [{
                        "open": [100], "high": [102], "low": [99],
                        "close": [101], "volume": [1000],
                    }]},
                }],
                "error": None,
            }
        }
        result = intraday.parse_yahoo_chart(document, "RELIANCE")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["timestamp"], "2026-01-05T13:45:00+05:30")

    def test_angel_candle_array_is_normalized(self):
        result = intraday.parse_angel_candles(
            [["2026-01-05 09:15", 100, 102, 99, 101, 1000]], "RELIANCE"
        )
        self.assertEqual(result[0]["close"], 101.0)
        self.assertEqual(result[0]["date"], "2026-01-05")

    def test_angel_ltp_is_normalized(self):
        result = intraday.parse_angel_ltp(
            {"ltp": "101.25", "open": "100", "high": "102", "low": "99"},
            "RELIANCE",
        )
        self.assertEqual(result["price"], 101.25)
        self.assertEqual(result["source"], "angelone-ltp")

    def test_india_defaults_match_one_percent_risk_setup(self):
        self.assertEqual(intraday.DEFAULTS["target_r"], 2.0)
        self.assertEqual(intraday.DEFAULTS["max_position_pct"], 75.0)
        self.assertEqual(intraday.DEFAULTS["max_trade_value_inr"], 10000.0)

    def test_plan_caps_trade_notional_at_ten_thousand(self):
        signal = intraday.signal_for(self.session())
        signal["symbol"] = "RELIANCE"
        plan = intraday.plan_from_signal(signal, 100000, 1000)
        self.assertLessEqual(plan["qty"] * plan["entry"], 10000.0)


if __name__ == "__main__":
    unittest.main()
