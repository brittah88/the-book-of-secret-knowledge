import json
import tempfile
import unittest
from pathlib import Path

import nft_monitor as nm

SETTINGS = dict(nm.DEFAULT_SETTINGS)
ETH = {"platform_fee_pct": 2.5, "gas": 0.003, "min_profit": 0.02}
ME = "0x1111111111111111111111111111111111111111"


def snap(listings, floor=1.0, best_offer=None, fee=0.0, chain="ethereum"):
    return nm.MarketSnapshot(
        chain=chain, collection="c", name="C", floor=floor, best_offer=best_offer,
        listings=[nm.Listing(str(i), p, s) for i, (p, s) in enumerate(listings)],
        creator_fee_pct=fee,
    )


def kinds(alerts):
    return sorted(a.kind for a in alerts)


class DetectorTests(unittest.TestCase):
    def run_detect(self, s, holdings=(), cost=None, prev=None):
        return nm.detect(s, list(holdings), {ME}, SETTINGS, ETH, cost, prev)

    def test_offer_over_floor_net_of_fees_and_gas(self):
        a = self.run_detect(snap([(1.0, "0xa"), (1.1, "0xb"), (1.2, "0xc")], best_offer=1.2))
        arb = [x for x in a if x.kind == "offer_over_floor"][0]
        self.assertAlmostEqual(arb.est_profit, 1.2 * 0.975 - 1.0 - 0.006, places=6)

    def test_no_arb_when_fees_eat_the_spread(self):
        a = self.run_detect(snap([(1.0, "0xa")], best_offer=1.02))
        self.assertNotIn("offer_over_floor", kinds(a))

    def test_underpriced_listing(self):
        a = self.run_detect(snap([(0.5, "0xa"), (0.95, "0xb"), (1.0, "0xc"), (1.05, "0xd")]))
        under = [x for x in a if x.kind == "underpriced"]
        self.assertEqual([x.token_id for x in under], ["0"])

    def test_tight_market_is_not_flagged(self):
        a = self.run_detect(snap([(1.0, "0xa"), (1.01, "0xb"), (1.02, "0xc"), (1.03, "0xd")]))
        self.assertEqual(a, [])

    def test_own_listing_below_best_offer_is_critical(self):
        a = self.run_detect(snap([(0.8, ME), (1.0, "0xb"), (1.1, "0xc")], best_offer=0.9))
        crit = [x for x in a if x.kind == "own_below_offer"]
        self.assertEqual(len(crit), 1)
        self.assertEqual(crit[0].severity, "critical")
        # Our own listing is never suggested as something to buy.
        self.assertFalse(any(x.kind == "offer_over_floor" and x.token_id == "0" for x in a))

    def test_take_profit(self):
        h = [nm.Holding("ethereum", ME, "v", "c", "1")]
        a = self.run_detect(snap([], best_offer=2.0), holdings=h, cost=1.0)
        self.assertIn("take_profit", kinds(a))
        a = self.run_detect(snap([], best_offer=1.1), holdings=h, cost=1.0)
        self.assertNotIn("take_profit", kinds(a))

    def test_floor_move(self):
        a = self.run_detect(snap([], floor=0.8), prev=1.0)
        self.assertEqual(kinds(a), ["floor_move"])
        self.assertEqual(a[0].severity, "warning")

    def test_scam_airdrop(self):
        h = [nm.Holding("ethereum", ME, "v", "free-eth", "1", name="Claim at eth-gift.xyz"),
             nm.Holding("ethereum", ME, "v", "c", "2", name="Normal Art #2")]
        markets = {"ethereum:c": snap([], floor=1.0)}
        a = nm.detect_scams(h, markets)
        self.assertEqual([x.token_id for x in a], ["1"])


class SafetyTests(unittest.TestCase):
    def test_rejects_private_keys_and_seed_phrases(self):
        for secret in ["0x" + "ab" * 32, "ab" * 32,
                       " ".join(["abandon"] * 11 + ["about"])]:
            with self.assertRaises(nm.ConfigError):
                nm.validate_address("ethereum", secret)

    def test_accepts_public_addresses(self):
        nm.validate_address("ethereum", ME)
        nm.validate_address("solana", "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")

    def test_config_refuses_secret(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write('[[wallets]]\nchain="ethereum"\naddress="0x%s"\n' % ("cd" * 32))
        with self.assertRaises(nm.ConfigError):
            nm.load_config(Path(fh.name))


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes

    def get_json(self, url, headers=None, body=None):
        for frag, payload in self.routes.items():
            if frag in url:
                return payload
        raise AssertionError(f"unexpected url {url}")


class OpenSeaParsingTests(unittest.TestCase):
    def test_snapshot_parses_v2_shapes(self):
        wei = 10 ** 18
        routes = {
            "/listings/collection/art/best": {"listings": [
                {"price": {"current": {"currency": "ETH", "decimals": 18, "value": str(int(0.6 * wei))}},
                 "protocol_data": {"parameters": {"offerer": "0xABC",
                                                  "offer": [{"token": "0xT", "identifierOrCriteria": "7"}]}}},
                {"price": {"current": {"currency": "ETH", "decimals": 18, "value": str(int(0.5 * wei))}},
                 "protocol_data": {"parameters": {"offerer": "0xDEF",
                                                  "offer": [{"token": "0xT", "identifierOrCriteria": "9"}]}}},
            ]},
            "/offers/collection/art": {"offers": [
                # 3 WETH for 5 items = 0.6 each
                {"price": {"currency": "WETH", "decimals": 18, "value": str(3 * wei)},
                 "protocol_data": {"parameters": {"consideration": [{"itemType": 4, "startAmount": "5"}]}}},
                {"price": {"currency": "WETH", "decimals": 18, "value": str(int(0.55 * wei))},
                 "protocol_data": {"parameters": {"consideration": [{"itemType": 4, "startAmount": "1"}]}}},
            ]},
            "/collections/art/stats": {"total": {"floor_price": 0.5}},
            "/collections/art": {"name": "Art", "safelist_status": "verified",
                                 "fees": [{"fee": 5.0, "required": True}, {"fee": 1.0, "required": False}]},
        }
        p = nm.OpenSeaProvider(FakeHttp(routes), "key", 20)
        s = p.snapshot("ethereum", "art")
        self.assertEqual(s.floor, 0.5)
        self.assertAlmostEqual(s.best_offer, 0.6)
        self.assertEqual(s.creator_fee_pct, 5.0)
        self.assertTrue(s.verified)
        self.assertEqual([l.token_id for l in s.listings], ["9", "7"])
        self.assertEqual(s.listings[0].seller, "0xdef")


class DemoRunTests(unittest.TestCase):
    def test_demo_end_to_end(self):
        cfg = nm.load_config(nm.HERE / "fixtures" / "demo_config.toml")
        with tempfile.TemporaryDirectory() as d:
            cfg["base_dir"] = Path(d)
            providers = nm.build_providers(cfg, None, nm.HERE / "fixtures" / "demo.json")
            state = nm.State(":memory:")
            report = nm.run_once(cfg, providers, state, nm.Http(0), quiet=True)
            self.assertEqual(report["errors"], [])
            got = {a["kind"] for a in report["alerts"]}
            self.assertTrue({"offer_over_floor", "underpriced", "own_below_offer",
                             "take_profit", "scam_airdrop"} <= got)
            # A second run inside the cooldown produces no new alerts.
            again = nm.run_once(cfg, providers, state, nm.Http(0), quiet=True)
            self.assertEqual(again["new_alerts"], [])
            json.loads((Path(d) / "demo_report.json").read_text())


if __name__ == "__main__":
    unittest.main()
