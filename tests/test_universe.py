import gzip
import json
import sys
import tempfile
import unittest

from _load import ROOT, load

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import india_universe

universe_cli = load("trader-universe", "universe")


ROWS = [
    {"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE",
     "exch_seg": "NSE", "instrumenttype": "", "lotsize": "1", "tick_size": "5.000000"},
    {"token": "3045", "symbol": "SBIN-EQ", "name": "SBIN",
     "exch_seg": "nse_cm", "instrumenttype": "", "lotsize": "1", "tick_size": "5.000000"},
    {"token": "111", "symbol": "RELIANCE25DEC3000CE", "name": "RELIANCE",
     "exch_seg": "NFO", "instrumenttype": "OPTSTK", "lotsize": "250", "tick_size": "5.000000"},
    {"token": "222", "symbol": "ABC-BE", "name": "ABC",
     "exch_seg": "NSE_CM", "instrumenttype": "", "lotsize": "1", "tick_size": "5.000000"},
    {"token": "333", "symbol": "RELIANCE-EQ", "name": "RELIANCE",
     "exch_seg": "BSE", "instrumenttype": "", "lotsize": "1", "tick_size": "5.000000"},
    {"token": "999", "symbol": "011NSETEST-EQ", "name": "011NSETEST",
     "exch_seg": "NSE", "instrumenttype": "", "lotsize": "1", "tick_size": "5.000000"},
]


class Universe(unittest.TestCase):
    def test_nse_cash_defaults_to_eq_and_preserves_token(self):
        rows = india_universe.nse_cash(ROWS)
        self.assertEqual([row["base_symbol"] for row in rows], ["RELIANCE", "SBIN"])
        self.assertEqual(rows[0]["token"], "2885")
        self.assertEqual(rows[0]["tick_size_inr"], 0.05)

    def test_be_is_explicit(self):
        rows = india_universe.nse_cash(ROWS, include_be=True)
        self.assertEqual([row["base_symbol"] for row in rows], ["ABC", "RELIANCE", "SBIN"])

    def test_broker_test_symbols_are_excluded(self):
        rows = india_universe.nse_cash(ROWS)
        self.assertNotIn("011NSETEST", [row["base_symbol"] for row in rows])

    def test_gzip_master_and_document_contract(self):
        payload = gzip.compress(json.dumps(ROWS).encode("utf-8"))
        normalized = india_universe.decode_master_bytes(payload)
        document = india_universe.universe_document(normalized, "fixture")
        self.assertEqual(document["symbols"], ["RELIANCE", "SBIN"])
        self.assertEqual(document["count"], 2)

    def test_normalized_cache_can_be_reloaded(self):
        rows = india_universe.nse_cash(ROWS)
        with tempfile.TemporaryDirectory() as folder:
            path = folder + "/master.json"
            india_universe.write_master(path, rows)
            loaded = india_universe.read_master(path)
        reliance = next(row for row in loaded if row["base_symbol"] == "RELIANCE")
        self.assertEqual(reliance["tick_size_inr"], 0.05)

    def test_nifty1000_csv_is_normalized_and_intersected(self):
        csv_payload = "Company Name,Symbol,Series\nReliance Industries,RELIANCE,EQ\nState Bank of India,SBIN.NS,EQ\nMissing,MISSING,EQ\n"
        symbols = india_universe.parse_nifty1000_constituents(csv_payload)
        selected = india_universe.filter_symbols(ROWS, symbols)
        self.assertEqual([row["base_symbol"] for row in selected], ["RELIANCE", "SBIN"])

    def test_nifty1000_document_records_index_metadata(self):
        document = india_universe.universe_document(
            ROWS, "fixture", allowed_symbols=["RELIANCE"],
            index_name="NIFTY 1000", index_source="official", index_fetched_at="now")
        self.assertEqual(document["index"], "NIFTY 1000")
        self.assertEqual(document["index_source"], "official")
        self.assertEqual(document["symbols"], ["RELIANCE"])

    def test_cli_parser_has_sync_and_list(self):
        self.assertEqual(universe_cli._parser().parse_args(["sync"]).command, "sync")
        self.assertEqual(universe_cli._parser().parse_args(["list"]).command, "list")
        args = universe_cli._parser().parse_args(["sync", "--index", "nifty1000"])
        self.assertEqual(args.index, "nifty1000")


if __name__ == "__main__":
    unittest.main()
