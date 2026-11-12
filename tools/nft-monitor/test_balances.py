import tempfile
import unittest
from pathlib import Path

import balances as bal
import nft_monitor as nm

HERE = Path(__file__).resolve().parent
ADDR = "0x1111111111111111111111111111111111111111"


class FakeAlchemyHttp:
    """Answers JSON-RPC (single and batch) and the Prices API."""

    def __init__(self):
        self.calls = []

    def get_json(self, url, headers=None, body=None):
        self.calls.append((url, body))
        if "/tokens/by-symbol" in url:
            return {"data": [{"symbol": "ETH", "prices": [{"currency": "usd", "value": "2000"}], "error": None}]}
        if "/tokens/by-address" in url:
            return {"data": [
                {"network": "eth-mainnet", "address": "0xAAA",
                 "prices": [{"currency": "usd", "value": "1.00"}], "error": None},
                {"network": "eth-mainnet", "address": "0xBBB", "prices": [], "error": "not found"},
            ]}
        if isinstance(body, list):  # batch token metadata
            meta = {"0xAAA": {"symbol": "USDC", "name": "USD Coin", "decimals": 6},
                    "0xBBB": {"symbol": "JUNK", "name": "Junk", "decimals": 18},
                    "0xCCC": {"symbol": None, "name": None, "decimals": None}}
            return [{"id": r["id"], "result": meta[r["params"][0]]} for r in body]
        method = body["method"]
        if method == "eth_getBalance":
            return {"result": hex(5 * 10 ** 17)}
        if method == "alchemy_getTokenBalances":
            if len(body["params"]) == 2:
                return {"result": {"tokenBalances": [
                    {"contractAddress": "0xAAA", "tokenBalance": hex(25_000_000)},
                    {"contractAddress": "0xZERO", "tokenBalance": "0x0"}], "pageKey": "p2"}}
            return {"result": {"tokenBalances": [
                {"contractAddress": "0xBBB", "tokenBalance": hex(10 ** 18)},
                {"contractAddress": "0xCCC", "tokenBalance": hex(7)}]}}
        raise AssertionError(method)


class AlchemyParsingTests(unittest.TestCase):
    def test_evm_assets_and_prices(self):
        http = FakeAlchemyHttp()
        src = bal.AlchemySource(http, "k")
        assets = src.evm_assets("ethereum", ADDR, "vault")
        by_sym = {a.symbol: a for a in assets}
        # Native + 2 sized tokens; zero balance and unknown-decimals token dropped.
        self.assertEqual(set(by_sym), {"ETH", "USDC", "JUNK"})
        self.assertAlmostEqual(by_sym["ETH"].amount, 0.5)
        self.assertAlmostEqual(by_sym["USDC"].amount, 25.0)
        src.price(assets)
        self.assertAlmostEqual(by_sym["ETH"].usd_value, 1000.0)
        self.assertAlmostEqual(by_sym["USDC"].usd_value, 25.0)
        self.assertIsNone(by_sym["JUNK"].usd_value)
        # The API key only ever goes to Alchemy.
        self.assertTrue(all("alchemy.com" in u for u, _ in http.calls))

    def test_requires_key(self):
        with self.assertRaises(nm.ConfigError):
            bal.AlchemySource(FakeAlchemyHttp(), "")


class MempoolTests(unittest.TestCase):
    def test_bitcoin_balance_includes_unconfirmed(self):
        class H:
            def get_json(self, url, headers=None, body=None):
                return {"chain_stats": {"funded_txo_sum": 300_000, "spent_txo_sum": 100_000},
                        "mempool_stats": {"funded_txo_sum": 50_000, "spent_txo_sum": 0}}
        [a] = bal.MempoolSource(H()).bitcoin_assets("bc1qxyz", "old")
        self.assertAlmostEqual(a.amount, 0.0025)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.cfg = nm.load_config(HERE / "fixtures" / "demo_config.toml")
        self.src = bal.DemoBalanceSource(HERE / "fixtures" / "demo_balances.json")

    def test_demo_scan(self):
        r = bal.scan_balances(self.cfg, self.src, self.src)
        found = {(a["chain"], a["symbol"]): a for a in r["found"]}
        # Every EVM wallet is checked on every chain, and off-chain finds are flagged.
        self.assertFalse(found[("arbitrum", "ARB")]["home_chain"])
        self.assertTrue(found[("arbitrum", "ARB")]["needs_gas"])
        self.assertTrue(found[("ethereum", "ETH")]["home_chain"])
        self.assertFalse(found[("ethereum", "USDC")]["needs_gas"])
        self.assertIn(("bitcoin", "BTC"), found)
        self.assertEqual(r["spam_hidden"], 1)
        self.assertEqual(len(r["unpriced"]), 1)
        self.assertEqual(r["dust"]["count"], 2)
        self.assertTrue(any("zksync" in s for s in r["skipped"]))
        self.assertEqual([a["usd_value"] for a in r["found"]],
                         sorted((a["usd_value"] for a in r["found"]), reverse=True))

    def test_run_writes_report(self):
        with tempfile.TemporaryDirectory() as d:
            self.cfg["base_dir"] = Path(d)
            rc = bal.run_balances(self.cfg, None, HERE / "fixtures" / "demo_balances.json", True)
            self.assertEqual(rc, 0)
            self.assertTrue((Path(d) / "demo_balances_report.json").exists())

    def test_chain_subset(self):
        self.cfg["balances"]["evm_chains"] = ["ethereum", "matic"]
        r = bal.scan_balances(self.cfg, self.src, self.src)
        chains = {a["chain"] for a in r["found"]}
        self.assertNotIn("arbitrum", chains)
        self.assertIn("polygon", chains)


class BitcoinSafetyTests(unittest.TestCase):
    def test_bitcoin_addresses_and_keys(self):
        nm.validate_address("bitcoin", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq")
        nm.validate_address("bitcoin", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2")
        for secret in ["5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ",
                       "xprv" + "9s21ZrQH143K" * 9]:
            with self.assertRaises(nm.ConfigError):
                nm.validate_address("bitcoin", secret)


if __name__ == "__main__":
    unittest.main()
