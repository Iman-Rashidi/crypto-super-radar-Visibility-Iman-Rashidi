from __future__ import annotations

import unittest

from crypto_super_radar.challenge1_v2_orchestration import (
    Challenge1V2DataBlocked,
    _regular_funding,
)

NOW = 1_800_000_000_000


class FundingClient:
    def __init__(self, rows):
        self.rows = rows

    def get_funding_rate_history(self, *, inst_id, limit):
        self.request = (inst_id, limit)
        return self.rows


def event(symbol="BTC-USDT", **extra):
    return {
        "instId": symbol,
        "fundingTime": str(NOW - 60_000),
        "fundingRate": "0.0001",
        **extra,
    }


class StandardCryptoFundingTests(unittest.TestCase):
    def test_missing_kind_defaults_only_for_exact_standard_crypto_perpetual(self):
        client = FundingClient([event()])
        result = _regular_funding(client, "BTC-USDT", asof_ms=NOW)
        self.assertEqual(result["funding_kind"], "0")
        self.assertEqual(result["funding_classification"], "STANDARD_CRYPTO_FUNDING")
        self.assertEqual(result["funding_rate"], 0.0001)
        self.assertEqual(client.request, ("BTC-USDT", 100))

    def test_missing_kind_supported_for_all_five_configured_symbols(self):
        for symbol in ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT"):
            with self.subTest(symbol=symbol):
                result = _regular_funding(FundingClient([event(symbol)]), symbol, asof_ms=NOW)
                self.assertEqual(result["funding_classification"], "STANDARD_CRYPTO_FUNDING")

    def test_explicit_stock_dividend_kind_stays_blocked(self):
        with self.assertRaisesRegex(Challenge1V2DataBlocked, "not regular"):
            _regular_funding(FundingClient([event(fundingKind="1")]), "BTC-USDT", asof_ms=NOW)

    def test_instrument_identity_is_mandatory_and_exact(self):
        with self.assertRaisesRegex(Challenge1V2DataBlocked, "identity"):
            _regular_funding(FundingClient([{"fundingTime": NOW - 60_000, "fundingRate": "0.0001"}]), "BTC-USDT", asof_ms=NOW)
        with self.assertRaisesRegex(Challenge1V2DataBlocked, "identity"):
            _regular_funding(FundingClient([event("ETH-USDT")]), "BTC-USDT", asof_ms=NOW)

    def test_invalid_rate_stays_blocked(self):
        with self.assertRaisesRegex(Challenge1V2DataBlocked, "fundingRate"):
            _regular_funding(FundingClient([event(fundingRate="NaN")]), "BTC-USDT", asof_ms=NOW)

    def test_non_standard_instrument_without_kind_stays_blocked(self):
        with self.assertRaisesRegex(Challenge1V2DataBlocked, "non-standard"):
            _regular_funding(FundingClient([event("DOGE-USDT")]), "DOGE-USDT", asof_ms=NOW)


if __name__ == "__main__":
    unittest.main()
