"""Tests for the quota-free local read-only CSR Copilot."""
import unittest
from unittest.mock import Mock

from crypto_super_radar import ai_copilot


class LocalCopilotTests(unittest.TestCase):
    def test_no_provider_key_is_required(self):
        result = ai_copilot.chat("سلام", lambda: {})
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["provider"], "داخلی")
        self.assertFalse(result["quota"]["provider_api"])
        self.assertIsNone(result["quota"]["hourly_message_limit"])

    def test_gem_hunter_answer_uses_real_context_fields(self):
        context = {
            "gem_hunter": {
                "status": "OK",
                "generated_at_utc": "2026-11-26T00:00:00Z",
                "discovered_candidate_count": 1,
                "tier1_count": 1,
                "tier2_count": 0,
                "top_candidates": [{
                    "name": "Example Token", "symbol": "EXT", "chain": "base",
                    "contract_address_short": "0x1234…abcd",
                    "price_usd": 0.01, "liquidity_usd": 50000,
                    "security_status": "UNKNOWN", "risk_score": 88,
                    "mint_authority_active": True, "freeze_authority_active": False,
                    "honeypot_detected": False, "sell_tax_pct": 5,
                }],
                "trade_authorization": "NO_TRADE",
            }
        }
        result = ai_copilot.chat("بهترین نامزدهای Gem Hunter کدام‌اند و چه ریسک‌هایی دارند؟", lambda: context)
        self.assertIn("Example Token", result["answer"])
        self.assertIn("EXT", result["answer"])
        self.assertIn("UNKNOWN", result["answer"])
        self.assertIn("50000", result["answer"])
        self.assertIn("0x1234", result["answer"])
        self.assertIn("Mint فعال=بله", result["answer"])
        self.assertIn("مالیات فروش=5%", result["answer"])
        self.assertIn("NO_TRADE", result["answer"])

    def test_challenge_answer_does_not_invent_missing_prices(self):
        context = {"challenge1_v2": {"status": "OK", "five_assets": [{
            "symbol": "BTC-USDT", "signal_raw": "LONG",
            "entry_price": None, "stop_loss": 89000, "take_profit": None,
        }]}}
        result = ai_copilot.chat("وضعیت سیگنال CHALLENGE_1_V2 چیست؟", lambda: context)
        self.assertIn("BTC-USDT", result["answer"])
        self.assertIn("ناموجود", result["answer"])
        self.assertIn("89000", result["answer"])

    def test_missing_context_is_reported_without_external_request(self):
        provider = Mock(return_value={})
        post = Mock(side_effect=AssertionError("network call must not happen"))
        result = ai_copilot.chat("بهترین نامزدهای Gem Hunter", provider, http_post=post)
        self.assertIn("نامزد Gem Hunter ثبت نشده", result["answer"])
        post.assert_not_called()
        provider.assert_called_once()

    def test_no_hourly_quota_across_many_messages(self):
        for index in range(75):
            result = ai_copilot.chat(f"پرسش {index}", lambda: {})
            self.assertEqual(result["status"], "OK")

    def test_message_validation(self):
        with self.assertRaises(ai_copilot.CopilotError) as empty:
            ai_copilot.chat("  ", lambda: {})
        with self.assertRaises(ai_copilot.CopilotError) as long:
            ai_copilot.chat("x" * (ai_copilot.MAX_MESSAGE_CHARS + 1), lambda: {})
        self.assertEqual(empty.exception.code, "MESSAGE_REQUIRED")
        self.assertEqual(long.exception.status, 413)

    def test_answers_are_read_only(self):
        result = ai_copilot.chat("خلاصه وضعیت داشبورد", lambda: {})
        self.assertTrue(result["read_only"])
        self.assertFalse(result["orders_submitted"])
        self.assertEqual(result["provider"], "داخلی")


if __name__ == "__main__":
    unittest.main()
