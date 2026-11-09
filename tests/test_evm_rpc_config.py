"""Configuration tests for the public EVM RPC endpoints."""
import unittest
from urllib.parse import urlparse

from crypto_super_radar.config import RateLimitConfig
from crypto_super_radar.evm_transfers import DEFAULT_RPC


class EvmRpcConfigurationTests(unittest.TestCase):
    def test_ethereum_uses_publicnode_mainnet_endpoint(self):
        self.assertEqual(DEFAULT_RPC[1], "https://ethereum-rpc.publicnode.com")

    def test_every_default_rpc_host_has_a_rate_limit(self):
        intervals = RateLimitConfig().host_intervals
        for endpoint in DEFAULT_RPC.values():
            self.assertIn(urlparse(endpoint).netloc, intervals)
            self.assertGreater(intervals[urlparse(endpoint).netloc], 0)


if __name__ == "__main__":
    unittest.main()
