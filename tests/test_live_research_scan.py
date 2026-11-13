"""Safety and fail-closed tests for the integrated live research scan."""
from __future__ import annotations

import unittest

from scripts.live_research_scan import run_scan


class FakeResult:
    def __init__(self, gate_statuses, signal="NO TRADE"):
        self.gate_statuses = gate_statuses
        self.signal = signal

    def to_record(self):
        return {
            "asset": "cex:BTCUSDT",
            "signal": self.signal,
            "safety_status": "UNKNOWN",
            "gates": [{"gate_id": i + 1, "status": status} for i, status in enumerate(self.gate_statuses)],
            "summary_en": "Test-only result",
            "summary_fa": "خروجی آزمایشی",
            "decision_blockers": {},
        }


class FakeEngine:
    def __init__(self, statuses, signal="NO TRADE"):
        self.result = FakeResult(statuses, signal)
        self.seen_asset = None

    def analyze(self, asset):
        self.seen_asset = asset
        return self.result


class LiveResearchScanTests(unittest.TestCase):
    def test_scan_is_limited_to_cex_or_perpetual_public_symbols(self):
        for invalid in ("base:0x1234567890", "solana:So11111111111111111111111111111111111111112", "cex:BTC/USDT", "perp:BTCUSDT&x=1"):
            with self.subTest(asset=invalid):
                with self.assertRaises(ValueError):
                    run_scan(invalid)

    def test_unknown_gate_blocks_decision_and_records_no_order_safety(self):
        engine = FakeEngine(["PASS"] * 11 + ["UNKNOWN"])
        report = run_scan("cex:BTCUSDT", engine_factory=lambda: engine)
        self.assertEqual(report["execution_status"], "COMPLETED")
        self.assertEqual(report["gate_status"], "INCONCLUSIVE")
        self.assertEqual(report["decision"]["signal"], "NO TRADE")
        self.assertEqual(report["gate_summary"]["unknown"], 1)
        self.assertTrue(report["safety"]["read_only"])
        self.assertFalse(report["safety"]["orders_submitted"])
        self.assertFalse(report["safety"]["transactions_signed"])
        self.assertEqual(engine.seen_asset.raw, "cex:BTCUSDT")

    def test_all_twelve_pass_is_required_for_pass_status(self):
        report = run_scan(
            "perp:BTCUSDT",
            engine_factory=lambda: FakeEngine(["PASS"] * 12, signal="LONG"),
        )
        self.assertEqual(report["gate_status"], "PASS")
        self.assertEqual(report["gate_summary"]["pass"], 12)
        self.assertFalse(report["safety"]["orders_submitted"])

    def test_failed_gate_is_blocked(self):
        report = run_scan(
            "cex:BTCUSDT",
            engine_factory=lambda: FakeEngine(["PASS"] * 10 + ["FAIL", "UNKNOWN"]),
        )
        self.assertEqual(report["gate_status"], "BLOCKED")
        self.assertEqual(report["decision"]["signal"], "NO TRADE")

    def test_fail_closed_invariant_rejects_signal_with_unknown_gate(self):
        with self.assertRaisesRegex(RuntimeError, "Fail-closed invariant"):
            run_scan(
                "cex:BTCUSDT",
                engine_factory=lambda: FakeEngine(["PASS"] * 11 + ["UNKNOWN"], signal="LONG"),
            )


if __name__ == "__main__":
    unittest.main()
