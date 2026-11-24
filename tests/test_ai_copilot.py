"""Unit tests for the guarded read-only AI Copilot."""
import unittest
from unittest.mock import Mock, patch

from crypto_super_radar import ai_copilot


class CopilotGuardTests(unittest.TestCase):
    def setUp(self):
        with ai_copilot._LOCK:
            ai_copilot._REQUESTS.clear()

    def test_groq_existing_key_is_selected_without_new_secret(self):
        with patch.dict("os.environ", {"GROQ_API_KEY": "gsk_existing", "OPENROUTER_API_KEY": "or_existing"}, clear=True):
            config = ai_copilot._provider_config()
        self.assertEqual(config["provider"], "Groq")
        self.assertEqual(config["key"], "gsk_existing")
        self.assertEqual(config["model"], "openai/gpt-oss-20b")

    def test_openrouter_existing_key_is_fallback(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "or_existing"}, clear=True):
            config = ai_copilot._provider_config()
        self.assertEqual(config["provider"], "OpenRouter")
        self.assertTrue(config["model"].endswith(":free"))

    def test_no_key_fails_closed(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ai_copilot.CopilotError) as caught:
                ai_copilot.chat("سلام", lambda: {})
        self.assertEqual(caught.exception.code, "AI_PROVIDER_NOT_CONFIGURED")

    def test_empty_and_oversized_messages_are_rejected(self):
        with patch.dict("os.environ", {"GROQ_API_KEY": "gsk_test"}, clear=True):
            with self.assertRaises(ai_copilot.CopilotError) as empty:
                ai_copilot.chat("  ", lambda: {})
            with self.assertRaises(ai_copilot.CopilotError) as long:
                ai_copilot.chat("x" * 2001, lambda: {})
        self.assertEqual(empty.exception.code, "MESSAGE_REQUIRED")
        self.assertEqual(long.exception.status, 413)

    def test_rate_limit_caps_at_twenty_per_hour(self):
        for _ in range(20):
            ai_copilot._rate_limit("test-client", now=1000)
        with self.assertRaises(ai_copilot.CopilotError) as caught:
            ai_copilot._rate_limit("test-client", now=1001)
        self.assertEqual(caught.exception.status, 429)

    def test_chat_sends_only_sanitized_context_and_no_tools(self):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "تحلیل فقط‌خواندنی"}}]}
        response.raise_for_status.return_value = None
        post = Mock(return_value=response)
        context = {
            "five_assets": [{
                "symbol": "BTC-USDT", "signal_raw": "LONG", "entry_price": 90000,
                "stop_loss": 89000, "take_profit": 92000,
                "ai_votes": [
                    {"reviewer_id": "nemotron", "provider": "OpenRouter", "opinion": "APPROVE", "reason_fa": "ساختار معتبر"},
                    {"reviewer_id": "groq", "provider": "Groq", "opinion": "VETO", "reason_fa": "ریسک بالا"},
                ],
            }],
            "gem_hunter": {"top_candidates": [{
                "name": "Example Token", "symbol": "EXT", "chain": "base",
                "contract_address_short": "0x1234…abcd", "price_usd": 0.01,
                "liquidity_usd": 50000, "security_status": "UNKNOWN",
            }]},
        }
        with patch.dict("os.environ", {"GROQ_API_KEY": "gsk_test"}, clear=True):
            result = ai_copilot.chat("وضعیت بازار چیست؟", lambda: context, http_post=post)
        request = post.call_args.kwargs["json"]
        self.assertEqual(result["answer"], "تحلیل فقط‌خواندنی")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["orders_submitted"])
        self.assertNotIn("tools", request)
        self.assertIn("READ-ONLY", request["messages"][0]["content"])
        self.assertIn("نام کامل واقعی توکن", request["messages"][0]["content"])
        self.assertIn("رأی Nemotron", request["messages"][0]["content"])
        self.assertIn("Groq", request["messages"][0]["content"])
        self.assertIn("BTC-USDT", request["messages"][1]["content"])
        self.assertIn("Example Token", request["messages"][1]["content"])
        self.assertIn("0x1234", request["messages"][1]["content"])
        self.assertIn('"entry_price":90000', request["messages"][1]["content"])
        self.assertIn('"stop_loss":89000', request["messages"][1]["content"])

    def test_rate_limit_is_applied_per_client(self):
        with patch.dict("os.environ", {"GROQ_API_KEY": "gsk_test"}, clear=True):
            for _ in range(20):
                ai_copilot._rate_limit("ip-a")
            with self.assertRaises(ai_copilot.CopilotError):
                ai_copilot._rate_limit("ip-a")
            ai_copilot._rate_limit("ip-b")


if __name__ == "__main__":
    unittest.main()