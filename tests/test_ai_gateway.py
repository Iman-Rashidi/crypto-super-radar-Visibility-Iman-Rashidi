import json
import unittest

from crypto_super_radar.ai_gateway import enrich_public_headlines


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            error = requests.HTTPError("request failed")
            error.response = type("Response", (), {"status_code": self.status_code})()
            raise error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response=None):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class AIGatewayTests(unittest.TestCase):
    def setUp(self):
        self.headlines = [{"title": "Protocol upgrade announced", "summary": "New integration planned"}]
        self.output = {
            "summary": "Public headlines describe a planned protocol upgrade; claims remain unverified.",
            "themes": ["protocol upgrade"],
            "risk_flags": ["announcement is not independently verified"],
            "conflicting_evidence": [],
        }

    def test_disabled_without_api_key_and_makes_no_request(self):
        session = FakeSession()
        result = enrich_public_headlines(self.headlines, api_key="", session=session)
        self.assertEqual(result["status"], "DISABLED")
        self.assertEqual(session.calls, [])

    def test_valid_structured_response_is_explanatory_only(self):
        session = FakeSession(FakeResponse({
            "candidates": [{"content": {"parts": [{"text": json.dumps(self.output)}]}}],
        }))
        result = enrich_public_headlines(self.headlines, api_key="test-key", session=session, cache={})
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["themes"], ["protocol upgrade"])
        url, kwargs = session.calls[0]
        self.assertIn("generateContent", url)
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-key")
        self.assertEqual(kwargs["json"]["generationConfig"]["responseMimeType"], "application/json")
        self.assertNotIn("trade", result)

    def test_invalid_json_is_unknown(self):
        session = FakeSession(FakeResponse({
            "candidates": [{"content": {"parts": [{"text": "not-json"}]}}],
        }))
        result = enrich_public_headlines(self.headlines, api_key="test-key", session=session)
        self.assertEqual(result["status"], "UNKNOWN")

    def test_invalid_schema_is_unknown(self):
        malformed = dict(self.output, risk_flags="not-a-list")
        session = FakeSession(FakeResponse({
            "candidates": [{"content": {"parts": [{"text": json.dumps(malformed)}]}}],
        }))
        result = enrich_public_headlines(self.headlines, api_key="test-key", session=session)
        self.assertEqual(result["status"], "UNKNOWN")

    def test_quota_error_degrades_without_crashing(self):
        session = FakeSession(FakeResponse({}, status_code=429))
        result = enrich_public_headlines(self.headlines, api_key="test-key", session=session)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("quota", result["reason"])

    def test_empty_headlines_are_unknown(self):
        result = enrich_public_headlines([], api_key="test-key", session=FakeSession())
        self.assertEqual(result["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
