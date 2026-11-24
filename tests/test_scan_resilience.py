import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crypto_super_radar import integrated_engine, web_app
from crypto_super_radar.config import AppConfig
from crypto_super_radar.models import GateResult, GateStatus, Signal, AssetSpec


class ScanResilienceTests(unittest.TestCase):
    def _base_result(self):
        gates = [
            GateResult(i, f"gate-{i}", f"دروازه {i}", GateStatus.UNKNOWN,
                       "fixture", "fixture", {"direction": "NONE"})
            for i in range(1, 10)
        ]
        gates[2].evidence = {"direction": "NONE"}
        return SimpleNamespace(
            gates=gates, metrics={}, signal=Signal.NO_TRADE,
            confirmation_score=40.0, estimated_risk_score=30.0,
            opportunity_rank=25.0, summary_fa="تحلیل پژوهشی",
            summary_en="Research only", safety_status=None,
        )

    def test_sentiment_and_market_intelligence_exceptions_do_not_abort_engine(self):
        asset = AssetSpec.parse("cex:BTCUSDT")
        data = SimpleNamespace(
            http=SimpleNamespace(limiter=None),
            whale_evidence={asset.raw: {"status": "UNKNOWN", "reason": "whale timeout"}},
            candle_frames={}, funding_frames={},
            snapshots={asset.raw: SimpleNamespace(price_usd=50000.0)},
        )
        store = SimpleNamespace(
            record=lambda *args, **kwargs: None,
            summary=lambda *args, **kwargs: {
                "observation_count": 0, "recent_bias": "UNKNOWN",
            },
        )
        engine = integrated_engine.WhaleAwareConfirmationEngine(
            AppConfig(), data_service=data,
            sentiment_fetcher=lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("sentiment timeout")),
            whale_store=store,
        )
        result = self._base_result()
        with patch.object(engine.base, "analyze", return_value=result), \
             patch.object(engine, "_multi_fold_validation", return_value={"status": "UNKNOWN", "reason": "no history"}), \
             patch.object(integrated_engine, "monitor_elliott_waves", return_value={"status": "UNKNOWN"}), \
             patch.object(integrated_engine, "monitor_chart_patterns", return_value={"status": "UNKNOWN"}), \
             patch.object(integrated_engine, "analyze_market_behavior", return_value={"status": "UNKNOWN", "reason": "no history"}), \
             patch.object(integrated_engine, "fetch_market_intelligence",
                          side_effect=RuntimeError("enrichment timeout")), \
             patch.object(integrated_engine, "contradicts_confirmed_breakout", return_value=False):
            completed = engine.analyze(asset)

        self.assertIs(completed, result)
        self.assertEqual([g.gate_id for g in completed.gates[-2:]], [10, 11])
        self.assertEqual(completed.gates[-1].status, GateStatus.UNKNOWN)
        self.assertEqual(completed.metrics["sentiment_coverage_status"], "UNKNOWN")
        self.assertEqual(completed.metrics["market_intelligence"]["status"], "PARTIAL")
        self.assertEqual(completed.signal, Signal.NO_TRADE)

    def test_scan_worker_payload_keeps_all_three_dashboard_sections_on_partial_enrichment(self):
        job_id = "scan-resilience-test"
        asset = AssetSpec.parse("cex:BTCUSDT")
        result = self._base_result()
        result.metrics["market_intelligence"] = {
            "status": "PARTIAL",
            "reason": "Sentiment/whale enrichment unavailable",
        }
        fake_engine = SimpleNamespace(
            analyze=lambda *args, **kwargs: result,
            data=SimpleNamespace(candle_frames={}, snapshots={}),
        )
        old_job = web_app.JOBS.get(job_id)
        web_app.JOBS[job_id] = {"status": "running"}
        try:
            with patch.object(web_app, "ENGINE", fake_engine), \
                 patch.object(web_app, "observed_price_ranges", return_value={
                     "status": "KNOWN", "reason": "validated market candles",
                     "current_price": 50000.0, "candle_age_hours": 1.0,
                     "windows": {"7": {"status": "KNOWN", "low": 49000, "high": 51000}},
                 }), \
                 patch.object(web_app, "_timeframe_ranges", return_value={"1d": {"status": "KNOWN"}}), \
                 patch.object(web_app, "_enforce_daily_candle_freshness"), \
                 patch.object(web_app, "_enforce_volume_cross_validation"), \
                 patch.object(web_app, "_coin_display", return_value={
                     "name_fa": "بیت‌کوین", "name_en": "Bitcoin", "symbol": "BTC",
                 }), \
                 patch.object(web_app, "_build_user_diagnosis", return_value={
                     "final_recommendation": "NO_TRADE",
                 }):
                web_app._run_scan(job_id, asset, "", "")

            job = web_app.JOBS[job_id]
            self.assertEqual(job["status"], "completed")
            payload = job["result"]
            self.assertIn("diagnosis", payload)
            self.assertIn("timeframe_ranges", payload)
            self.assertIn("market_overview", payload)
            self.assertIn("market_intelligence", payload)
            self.assertFalse(payload["safety"]["orders_submitted"])
        finally:
            web_app.JOBS.pop(job_id, None)
            if old_job is not None:
                web_app.JOBS[job_id] = old_job


if __name__ == "__main__":
    unittest.main()
