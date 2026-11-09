"""Offline integration tests for the dependency-light HTTP dashboard."""
from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from http.client import HTTPConnection
from urllib.parse import quote

from crypto_super_radar.web_app import Handler


class QuietHandler(Handler):
    def log_message(self, fmt, *args):
        pass


class WebAppHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method: str, path: str, body=None, headers=None):
        connection = HTTPConnection(self.host, self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        response_headers = dict(response.getheaders())
        connection.close()
        return status, response_headers, payload

    def test_health_endpoint_is_live_and_not_cacheable(self):
        status, headers, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"status": "ok"})
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_home_page_is_persian_and_lists_twelve_gates(self):
        status, headers, payload = self.request("GET", "/")
        page = payload.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('lang="fa" dir="rtl"', page)
        self.assertIn("۱۲ فیلتر اجباری", page)
        self.assertIn("Paper Research", page)
        self.assertIn("ماشین‌حساب حرفه‌ای سناریوی Spot", page)
        self.assertIn('id="calc-amount"', page)
        self.assertIn("calc-break-even", page)

    def test_history_endpoint_returns_local_sqlite_summary(self):
        status, headers, payload = self.request(
            "GET", "/history?asset=" + quote("cex:BTCUSDT", safe="")
        )
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["storage"], "LOCAL_SQLITE")
        self.assertIn("observation_count", result)

    def test_unknown_job_returns_not_found(self):
        status, _, payload = self.request("GET", "/jobs/not-a-real-job")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(payload), {"error": "job not found"})

    def test_cross_origin_scan_post_is_rejected_before_engine_execution(self):
        status, _, payload = self.request(
            "POST",
            "/scan",
            body="asset=cex%3ABTCUSDT",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len("asset=cex%3ABTCUSDT")),
                "Origin": "https://attacker.example",
            },
        )
        self.assertEqual(status, 403)
        self.assertIn(b"Cross-origin", payload)


if __name__ == "__main__":
    unittest.main()
