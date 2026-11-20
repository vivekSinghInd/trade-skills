"""Read-only Angel One SmartAPI helpers for NSE market data.

Credentials are loaded from environment variables or an explicitly supplied
local JSON configuration file.  Values are never written to the repository or
included in exception messages.  This module deliberately exposes only login,
historical-candle, and LTP reads; it does not contain order-placement methods.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import hmac
import json
import os
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


LOGIN_URL = "https://apiconnect.angelbroking.com/rest/auth/angelbroking/user/v1/loginByPassword"
HISTORICAL_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/historical/v1/getCandleData"
LTP_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/order/v1/getLtpData"
USER_AGENT = "day-trader-skill-india/1.0"

INTERVALS = {
    "1m": "ONE_MINUTE",
    "3m": "THREE_MINUTE",
    "5m": "FIVE_MINUTE",
    "10m": "TEN_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1h": "ONE_HOUR",
    "1d": "ONE_DAY",
}


class AngelOneError(Exception):
    """Expected read-only SmartAPI or credential configuration error."""


def _value(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(str(name).lower())
        if value not in (None, ""):
            return str(value).strip()
    return None


def _required(value, name):
    if not value:
        raise AngelOneError("missing Angel One credential field: %s" % name)
    return value


def _base32_decode(secret):
    normalized = "".join(str(secret).split()).upper()
    try:
        padding = "=" * ((8 - len(normalized) % 8) % 8)
        return base64.b32decode(normalized + padding, casefold=True)
    except (ValueError, binascii.Error) as exc:
        raise AngelOneError("Angel One TOTP secret is not valid base32") from exc


def totp(secret, at=None):
    """Return the six-digit RFC 6238 SHA-1 TOTP for a secret."""
    moment = time.time() if at is None else float(at)
    counter = int(moment // 30)
    digest = hmac.new(
        _base32_decode(secret), struct.pack(">Q", counter), hashlib.sha1
    ).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return "%06d" % (value % 1000000)


@dataclass(frozen=True)
class AngelOneCredentials:
    api_key: str
    client_code: str
    password: str
    totp_secret: str

    @classmethod
    def from_environment(cls):
        return cls(
            api_key=_required(os.environ.get("ANGELONE_API_KEY"), "ANGELONE_API_KEY"),
            client_code=_required(
                os.environ.get("ANGELONE_CLIENT_CODE"), "ANGELONE_CLIENT_CODE"
            ),
            password=_required(
                os.environ.get("ANGELONE_PASSWORD"), "ANGELONE_PASSWORD"
            ),
            totp_secret=_required(
                os.environ.get("ANGELONE_TOTP_SECRET"), "ANGELONE_TOTP_SECRET"
            ),
        )

    @classmethod
    def from_json(cls, path, section="BrokerSettings"):
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as exc:
            raise AngelOneError("cannot read Angel One credential file") from exc
        if section:
            data = document.get(section) if isinstance(document, dict) else None
        else:
            data = document
        if not isinstance(data, dict):
            raise AngelOneError("credential file has no BrokerSettings object")
        return cls(
            api_key=_required(_value(data, "ApiKey", "api_key"), "ApiKey"),
            client_code=_required(
                _value(data, "ClientCode", "clientcode", "client_code"), "ClientCode"
            ),
            password=_required(_value(data, "Password", "password"), "Password"),
            totp_secret=_required(
                _value(data, "TotpSecret", "totp", "totp_secret"), "TotpSecret"
            ),
        )


def _headers(api_key, token=None):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-PrivateKey": api_key,
        "X-ClientLocalIP": os.environ.get("ANGELONE_CLIENT_LOCAL_IP", "127.0.0.1"),
        "X-ClientPublicIP": os.environ.get("ANGELONE_CLIENT_PUBLIC_IP", "0.0.0.0"),
        "X-MACAddress": os.environ.get(
            "ANGELONE_MAC_ADDRESS", "00:00:00:00:00:00"
        ),
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _post(url, payload, headers, timeout=20):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise AngelOneError("Angel One request failed with HTTP %s" % exc.code) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise AngelOneError("Angel One request could not reach the API") from exc
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AngelOneError("Angel One returned invalid JSON") from exc
    if not isinstance(document, dict):
        raise AngelOneError("Angel One returned an unexpected response")
    return document


class AngelOneClient:
    """Small read-only SmartAPI client for historical candles and LTP data."""

    def __init__(self, credentials, timeout=20):
        if not isinstance(credentials, AngelOneCredentials):
            raise AngelOneError("credentials must be AngelOneCredentials")
        self.credentials = credentials
        self.timeout = timeout
        self._token = None

    def login(self):
        document = _post(
            LOGIN_URL,
            {
                "clientcode": self.credentials.client_code,
                "password": self.credentials.password,
                "totp": totp(self.credentials.totp_secret),
            },
            _headers(self.credentials.api_key),
            self.timeout,
        )
        data = document.get("data") or {}
        token = data.get("jwtToken") or data.get("jwt_token")
        if not document.get("status") or not token:
            raise AngelOneError(
                "Angel One login was rejected: %s"
                % (document.get("message") or document.get("errorcode") or "unknown error")
            )
        self._token = str(token)
        return self._token

    def historical_candles(
        self, exchange, symbol_token, interval, fromdate, todate
    ):
        if interval not in INTERVALS:
            raise AngelOneError(
                "unsupported Angel One interval; use one of: %s"
                % ", ".join(sorted(INTERVALS))
            )
        token = self._token or self.login()
        document = _post(
            HISTORICAL_URL,
            {
                "exchange": str(exchange).upper(),
                "symboltoken": str(symbol_token),
                "interval": INTERVALS[interval],
                "fromdate": str(fromdate),
                "todate": str(todate),
            },
            _headers(self.credentials.api_key, token),
            self.timeout,
        )
        if not document.get("status"):
            raise AngelOneError(
                "Angel One historical request was rejected: %s"
                % (document.get("message") or document.get("errorcode") or "unknown error")
            )
        rows = document.get("data") or []
        if not isinstance(rows, list):
            raise AngelOneError("Angel One historical response has no candle rows")
        return rows

    def ltp_data(self, exchange, trading_symbol, symbol_token):
        """Return the current Angel One quote for one instrument.

        SmartAPI's LTP endpoint is a market-data read. It is intentionally
        kept separate from the order API so this project remains paper-only.
        The raw ``data`` object is returned because it can also contain the
        broker's OHLC reference fields.
        """
        token = self._token or self.login()
        document = _post(
            LTP_URL,
            {
                "exchange": str(exchange).upper(),
                "tradingsymbol": str(trading_symbol),
                "symboltoken": str(symbol_token),
            },
            _headers(self.credentials.api_key, token),
            self.timeout,
        )
        if not document.get("status"):
            raise AngelOneError(
                "Angel One LTP request was rejected: %s"
                % (document.get("message") or document.get("errorcode") or "unknown error")
            )
        data = document.get("data") or {}
        if not isinstance(data, dict):
            raise AngelOneError("Angel One LTP response has no quote data")
        ltp = _value(data, "ltp", "lasttradedprice", "last_traded_price")
        try:
            if ltp is None or float(ltp) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise AngelOneError("Angel One LTP response has no valid last traded price")
        return data


def default_day_window(day=None):
    """Return an IST trading-day window suitable for a historical request."""
    selected = day or dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=5, minutes=30))
    ).date().isoformat()
    return "%s 09:15" % selected, "%s 15:30" % selected
