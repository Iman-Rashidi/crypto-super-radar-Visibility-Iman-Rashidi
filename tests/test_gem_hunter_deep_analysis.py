"""Unit tests for read-only Gem Hunter deep analysis."""
import unittest
from unittest.mock import patch

from crypto_super_radar.web_app import build_gem_hunter_deep_analysis


class GemHunterDeepAnalysisTests(unittest.TestCase):
    def dex_payload(self):
        return {"pairs": [{
            "chainId": "solana",
            "baseToken": {"address": "MintExample", "symbol": "EX", "name": "Example"},
            "priceUsd": "0.001",
            "volume": {"h24": 250000},
            "liquidity": {"usd": 80000},
            "fdv": 900000,
            "marketCap": 850000,
            "txns": {
                "m5": {"buys": 12, "sells": 8},
                "h1": {"buys": 90, "sells": 70},
                "h6": {"buys": 500, "sells": 450},
                "h24": {"buys": 2000, "sells": 1800},
            },
            "priceChange": {"m5": 1.2, "h1": 2.5, "h6": 8.0, "h24": 24.0},
            "dexId": "pumpswap",
        }]}

    @patch("crypto_super_radar.web_app._deep_analysis_json")
    def test_missing_rugcheck_is_explicit_not_fabricated(self, fetch):
        fetch.side_effect = [self.dex_payload(), None]
        report = build_gem_hunter_deep_analysis("solana", "MintExample", "EX")
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["security"]["provider"], "NOT_AVAILABLE")
        self.assertEqual(report["security"]["mint_authority"], "NOT_AVAILABLE")
        self.assertEqual(report["security"]["freeze_authority"], "NOT_AVAILABLE")
        self.assertEqual(report["security"]["lp_locked_pct"], "NOT_AVAILABLE")
        self.assertEqual(report["whales"]["large_transfers"], "NOT_AVAILABLE")
        self.assertEqual(report["trade_authorization"], "NO_TRADE")

    @patch("crypto_super_radar.web_app._deep_analysis_json")
    def test_euro_scenarios_and_market_pressure(self, fetch):
        fetch.side_effect = [self.dex_payload(), None]
        report = build_gem_hunter_deep_analysis("solana", "MintExample", "EX")
        self.assertEqual(report["eur_scenarios"]["10"]["minus_20_pct"], 8.0)
        self.assertEqual(report["eur_scenarios"]["50"]["plus_10_pct"], 55.0)
        self.assertEqual(report["eur_scenarios"]["100"]["plus_20_pct"], 120.0)
        self.assertEqual(report["buy_sell_pressure"]["h24"]["buys"], 2000)
        self.assertEqual(report["buy_sell_pressure"]["h24"]["sells"], 1800)
        self.assertEqual(report["market"]["price_usd"], 0.001)
        self.assertGreaterEqual(report["opportunity_score"], 0)
        self.assertLessEqual(report["opportunity_score"], 100)
        self.assertGreaterEqual(report["risk_score"], 0)
        self.assertLessEqual(report["risk_score"], 100)

    @patch("crypto_super_radar.web_app._deep_analysis_json")
    def test_no_market_pair_returns_unavailable_without_fake_price(self, fetch):
        fetch.side_effect = [None, None]
        report = build_gem_hunter_deep_analysis("solana", "MintExample")
        self.assertEqual(report["status"], "MARKET_DATA_UNAVAILABLE")
        self.assertEqual(report["market"]["price_usd"], "NOT_AVAILABLE")
        self.assertEqual(report["price_levels"], "NOT_AVAILABLE")

    @patch("crypto_super_radar.web_app._deep_analysis_json")
    def test_symbol_lookup_resolves_highest_liquidity_mint_for_rugcheck(self, fetch):
        dex = {
            "pairs": [
                {
                    "chainId": "solana",
                    "baseToken": {"address": "BONK_LOW_MINT", "symbol": "BONK"},
                    "priceUsd": "0.00001",
                    "liquidity": {"usd": 12000},
                    "volume": {"h24": 100000},
                    "txns": {"h24": {"buys": 100, "sells": 80}},
                },
                {
                    "chainId": "solana",
                    "baseToken": {"address": "BONK_HIGH_MINT", "symbol": "BONK"},
                    "priceUsd": "0.00002",
                    "liquidity": {"usd": 95000},
                    "volume": {"h24": 900000},
                    "txns": {"h24": {"buys": 900, "sells": 700}},
                },
            ]
        }
        rugcheck = {
            "mintAuthority": None,
            "freezeAuthority": None,
            "score_normalised": 120,
            "totalHolders": 25000,
            "topHolders": [{"pct": 4.2}, {"pct": 3.1}],
            "markets": [{"lp": {"lpLockedPct": 98.5}}],
            "risks": [],
        }
        fetch.side_effect = [dex, rugcheck]
        report = build_gem_hunter_deep_analysis("solana", "", "BONK")
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["market"]["price_usd"], 0.00002)
        self.assertEqual(report["market"]["liquidity_usd"], 95000)
        self.assertEqual(report["market"]["volume_24h_usd"], 900000)
        self.assertEqual(report["security"]["provider"], "RUGCHECK")
        self.assertEqual(report["security"]["lp_locked_pct"], 98.5)
        self.assertEqual(report["whales"]["holder_count"], 25000)
        self.assertIn("BONK_HIGH_MINT", fetch.call_args_list[1].args[0])
        self.assertTrue(fetch.call_args_list[0].args[0].endswith("/latest/dex/search?q=BONK"))
        self.assertTrue(fetch.call_args_list[1].args[0].endswith(
            "/v1/tokens/BONK_HIGH_MINT/report/summary"
        ))

    @patch("urllib.request.urlopen")
    def test_provider_http_requests_send_json_and_browser_user_agent(self, urlopen):
        from unittest.mock import MagicMock
        from crypto_super_radar.web_app import _deep_analysis_json
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        urlopen.return_value = response
        self.assertEqual(_deep_analysis_json("https://api.dexscreener.com/test"), {"ok": True})
        request = urlopen.call_args.args[0]
        self.assertIn("Mozilla/5.0", request.get_header("User-agent"))
        self.assertEqual(request.get_header("Accept"), "application/json")

    def test_invalid_request_is_rejected_and_no_trade(self):
        report = build_gem_hunter_deep_analysis("unknown", "", "")
        self.assertEqual(report["status"], "INVALID_INPUT")
        self.assertEqual(report["trade_authorization"], "NO_TRADE")


if __name__ == "__main__":
    unittest.main()
