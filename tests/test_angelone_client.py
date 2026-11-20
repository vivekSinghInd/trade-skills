import json
import os
import sys
import tempfile
import unittest

from _load import ROOT

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import angelone_client
from angelone_client import AngelOneClient, AngelOneCredentials, default_day_window, totp


class AngelOneClientTests(unittest.TestCase):
    def test_totp_matches_rfc6238_sha1_vector(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(totp(secret, 59), "287082")

    def test_credentials_load_from_broker_settings_without_printing_values(self):
        document = {"BrokerSettings": {
            "ApiKey": "api-key-for-test",
            "ClientCode": "client-for-test",
            "Password": "password-for-test",
            "TotpSecret": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
        }}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
            json.dump(document, handle)
            path = handle.name
        try:
            credentials = AngelOneCredentials.from_json(path)
        finally:
            os.unlink(path)
        self.assertEqual(credentials.client_code, "client-for-test")
        self.assertEqual(credentials.api_key, "api-key-for-test")

    def test_default_window_is_explicit_ist_session_window(self):
        self.assertEqual(default_day_window("2026-11-13"),
                         ("2026-11-13 09:15", "2026-11-13 15:30"))

    def test_ltp_data_is_read_only_and_returns_quote_data(self):
        credentials = AngelOneCredentials("api", "client", "password",
                                          "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        client = AngelOneClient(credentials)
        client._token = "jwt"
        original_post = angelone_client._post
        calls = []

        def fake_post(url, payload, headers, timeout):
            calls.append((url, payload, headers, timeout))
            return {"status": True, "data": {"ltp": "123.45", "close": "120.00"}}

        angelone_client._post = fake_post
        try:
            result = client.ltp_data("NSE", "RELIANCE-EQ", " RelianceToken ")
        finally:
            angelone_client._post = original_post
        self.assertEqual(result["ltp"], "123.45")
        self.assertEqual(calls[0][0], angelone_client.LTP_URL)
        self.assertEqual(calls[0][1], {"exchange": "NSE", "tradingsymbol": "RELIANCE-EQ",
                                       "symboltoken": " RelianceToken "})


if __name__ == "__main__":
    unittest.main()
