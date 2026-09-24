"""
balances.py - find forgotten coins and tokens across all your wallets.

Used by `nft_monitor.py --balances`. Read-only: public addresses in, a report
out. The key idea is that one 0x address is the SAME address on every
EVM chain, so every EVM wallet is checked on every chain below, not just
the chain it's listed under in your config. That is where forgotten money
usually hides.

Data sources
  * EVM chains and Solana: Alchemy (free key: https://dashboard.alchemy.com).
    Enable the networks you care about in your Alchemy app; any network that
    isn't enabled is skipped and listed under "skipped".
  * Bitcoin: mempool.space public API (no key).
  * USD prices: Alchemy Prices API.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nft_monitor import SCAM_NAME_RE, ConfigError, Http

# Our chain name -> (Alchemy network id, native symbol, native decimals)
EVM_NETWORKS = {
    "ethereum":   ("eth-mainnet", "ETH", 18),
    "base":       ("base-mainnet", "ETH", 18),
    "arbitrum":   ("arb-mainnet", "ETH", 18),
    "optimism":   ("opt-mainnet", "ETH", 18),
    "polygon":    ("polygon-mainnet", "POL", 18),
    "bnb":        ("bnb-mainnet", "BNB", 18),
    "avalanche":  ("avax-mainnet", "AVAX", 18),
    "zora":       ("zora-mainnet", "ETH", 18),
    "blast":      ("blast-mainnet", "ETH", 18),
    "linea":      ("linea-mainnet", "ETH", 18),
    "scroll":     ("scroll-mainnet", "ETH", 18),
    "zksync":     ("zksync-mainnet", "ETH", 18),
    "unichain":   ("unichain-mainnet", "ETH", 18),
    "worldchain": ("worldchain-mainnet", "ETH", 18),
    "ape_chain":  ("apechain-mainnet", "APE", 18),
}
EVM_ALIASES = {"matic": "polygon"}
SOLANA_NETWORK = "solana-mainnet"
SPL_TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGqPXTTTSe4Hs1yb8ZnqLsnh",   # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",    # Token-2022
)
DEFAULTS = {"min_usd": 1.0, "report_path": "balances_report.json"}


@dataclass
class Asset:
    chain: str
    wallet: str
    wallet_label: str
    symbol: str
    name: str
    amount: float
    contract: str = ""           # "" = the chain's native coin
    usd_price: float | None = None
    usd_value: float | None = None
    spam: bool = False
    home_chain: bool = True      # False = found on a chain you didn't list


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

class AlchemySource:
    def __init__(self, http: Http, api_key: str):
        if not api_key:
            raise ConfigError("Set ALCHEMY_API_KEY to scan balances "
                              "(free at https://dashboard.alchemy.com).")
        self.http, self.key = http, api_key
        self._meta: dict[tuple[str, str], dict] = {}

    def _url(self, network: str) -> str:
        return f"https://{network}.g.alchemy.com/v2/{self.key}"

    def rpc(self, network: str, method: str, params: list) -> Any:
        resp = self.http.get_json(self._url(network), body={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if resp.get("error"):
            raise RuntimeError(resp["error"].get("message", resp["error"]))
        return resp["result"]

    def rpc_batch(self, network: str, method: str, params_list: list[list]) -> list:
        out: list = []
        for i in range(0, len(params_list), 50):
            chunk = params_list[i:i + 50]
            body = [{"jsonrpc": "2.0", "id": n, "method": method, "params": p}
                    for n, p in enumerate(chunk)]
            resp = self.http.get_json(self._url(network), body=body)
            by_id = {r.get("id"): r.get("result") for r in resp}
            out += [by_id.get(n) for n in range(len(chunk))]
        return out

    def evm_assets(self, chain: str, address: str, label: str) -> list[Asset]:
        network, native, decimals = EVM_NETWORKS[chain]
        wei = int(self.rpc(network, "eth_getBalance", [address, "latest"]), 16)
        assets = []
        if wei:
            assets.append(Asset(chain, address, label, native, native, wei / 10 ** decimals))

        balances, page_key = [], None
        while True:
            opts = {"pageKey": page_key} if page_key else {}
            res = self.rpc(network, "alchemy_getTokenBalances",
                           [address, "erc20", opts] if opts else [address, "erc20"])
            balances += [b for b in res.get("tokenBalances", [])
                         if b.get("tokenBalance") and int(b["tokenBalance"], 16) > 0]
            page_key = res.get("pageKey")
            if not page_key:
                break

        need = [b["contractAddress"] for b in balances
                if (chain, b["contractAddress"]) not in self._meta]
        for contract, meta in zip(need, self.rpc_batch(
                network, "alchemy_getTokenMetadata", [[c] for c in need])):
            self._meta[(chain, contract)] = meta or {}

        for b in balances:
            meta = self._meta.get((chain, b["contractAddress"]), {})
            dec = meta.get("decimals")
            if dec is None:
                continue  # can't size it; almost always junk
            symbol, name = meta.get("symbol") or "?", meta.get("name") or ""
            assets.append(Asset(
                chain, address, label, symbol, name,
                int(b["tokenBalance"], 16) / 10 ** dec, contract=b["contractAddress"],
                spam=bool(SCAM_NAME_RE.search(f"{symbol} {name}")),
            ))
        return assets

    def solana_assets(self, address: str, label: str) -> list[Asset]:
        lamports = self.rpc(SOLANA_NETWORK, "getBalance", [address])["value"]
        assets = []
        if lamports:
            assets.append(Asset("solana", address, label, "SOL", "Solana", lamports / 1e9))
        for program in SPL_TOKEN_PROGRAMS:
            res = self.rpc(SOLANA_NETWORK, "getTokenAccountsByOwner",
                           [address, {"programId": program}, {"encoding": "jsonParsed"}])
            for acct in res.get("value", []):
                info = acct["account"]["data"]["parsed"]["info"]
                amt = info["tokenAmount"]
                if int(amt.get("decimals", 0)) == 0 or not float(amt.get("uiAmount") or 0):
                    continue  # NFTs (0 decimals) and empty accounts
                mint = info["mint"]
                assets.append(Asset("solana", address, label, mint[:4] + "…" + mint[-4:],
                                    "", float(amt["uiAmount"]), contract=mint))
        return assets

    def price(self, assets: list[Asset]) -> None:
        base = f"https://api.g.alchemy.com/prices/v1/{self.key}/tokens"
        symbols = sorted({a.symbol for a in assets if not a.contract})
        sym_price: dict[str, float] = {}
        for i in range(0, len(symbols), 25):
            q = "&".join(f"symbols={s}" for s in symbols[i:i + 25])
            for row in self.http.get_json(f"{base}/by-symbol?{q}").get("data", []):
                p = _usd(row)
                if p is not None:
                    sym_price[row["symbol"]] = p

        tokens = sorted({(network_for(a.chain), a.contract) for a in assets
                         if a.contract and not a.spam})
        addr_price: dict[tuple[str, str], float] = {}
        for i in range(0, len(tokens), 25):
            body = {"addresses": [{"network": n, "address": c} for n, c in tokens[i:i + 25]]}
            for row in self.http.get_json(f"{base}/by-address", body=body).get("data", []):
                p = _usd(row)
                if p is not None:
                    addr_price[(row["network"], row["address"].lower())] = p

        for a in assets:
            if a.contract:
                a.usd_price = addr_price.get((network_for(a.chain), a.contract.lower()))
            else:
                a.usd_price = sym_price.get(a.symbol)
            if a.usd_price is not None:
                a.usd_value = a.amount * a.usd_price


class MempoolSource:
    BASE = "https://mempool.space/api"

    def __init__(self, http: Http):
        self.http = http

    def bitcoin_assets(self, address: str, label: str) -> list[Asset]:
        d = self.http.get_json(f"{self.BASE}/address/{address}")
        sats = sum(d[k]["funded_txo_sum"] - d[k]["spent_txo_sum"]
                   for k in ("chain_stats", "mempool_stats"))
        return [Asset("bitcoin", address, label, "BTC", "Bitcoin", sats / 1e8)] if sats else []


class DemoBalanceSource:
    """Offline source fed from fixtures/demo_balances.json."""

    def __init__(self, path: Path):
        self.data = json.loads(path.read_text())

    def _assets(self, chain: str, address: str, label: str) -> list[Asset]:
        rows = self.data["balances"].get(address, {}).get(chain, [])
        if isinstance(rows, dict) and rows.get("error"):
            raise RuntimeError(rows["error"])
        return [Asset(chain, address, label, **r) for r in rows]

    def evm_assets(self, chain, address, label):
        return self._assets(chain, address, label)

    def solana_assets(self, address, label):
        return self._assets("solana", address, label)

    def bitcoin_assets(self, address, label):
        return self._assets("bitcoin", address, label)

    def price(self, assets: list[Asset]) -> None:
        prices = self.data["prices"]
        for a in assets:
            a.usd_price = prices.get(a.contract or a.symbol)
            if a.usd_price is not None:
                a.usd_value = a.amount * a.usd_price


def _usd(row: dict) -> float | None:
    if row.get("error"):
        return None
    for p in row.get("prices") or []:
        if p.get("currency", "").lower() == "usd":
            return float(p["value"])
    return None


def network_for(chain: str) -> str:
    return SOLANA_NETWORK if chain == "solana" else EVM_NETWORKS[chain][0]


# --------------------------------------------------------------------------- #
# Scan + report
# --------------------------------------------------------------------------- #

def evm_chains(cfg: dict[str, Any]) -> list[str]:
    chosen = cfg.get("balances", {}).get("evm_chains")
    return [EVM_ALIASES.get(c, c) for c in chosen] if chosen else list(EVM_NETWORKS)


def scan_balances(cfg: dict[str, Any], chain_src, btc_src) -> dict[str, Any]:
    opts = {**DEFAULTS, **cfg.get("balances", {})}
    min_usd = float(opts["min_usd"])
    assets: list[Asset] = []
    skipped: list[str] = []
    seen_evm: set[str] = set()

    for w in cfg["wallets"]:
        addr, label, home = w["address"], w["label"], EVM_ALIASES.get(w["chain"], w["chain"])
        if home == "solana":
            jobs = [("solana", lambda: chain_src.solana_assets(addr, label))]
        elif home == "bitcoin":
            jobs = [("bitcoin", lambda: btc_src.bitcoin_assets(addr, label))]
        else:
            if addr.lower() in seen_evm:  # same 0x address listed twice
                continue
            seen_evm.add(addr.lower())
            jobs = [(c, lambda c=c: chain_src.evm_assets(c, addr, label))
                    for c in evm_chains(cfg) if c in EVM_NETWORKS]
        for chain, job in jobs:
            try:
                found = job()
            except Exception as exc:
                skipped.append(f"{label} on {chain}: {exc}")
                continue
            for a in found:
                a.home_chain = chain == home
            assets += found

    try:
        chain_src.price(assets)
    except Exception as exc:
        skipped.append(f"pricing: {exc}")

    native_on = {(a.wallet.lower(), a.chain) for a in assets if not a.contract and a.amount > 0}
    found, dust, unpriced, spam = [], [], [], []
    for a in assets:
        if a.spam:
            spam.append(a)
        elif a.usd_value is None:
            unpriced.append(a)
        elif a.usd_value >= min_usd:
            found.append(a)
        else:
            dust.append(a)
    found.sort(key=lambda a: -(a.usd_value or 0))

    def row(a: Asset) -> dict:
        d = asdict(a)
        d["needs_gas"] = bool(a.contract) and (a.wallet.lower(), a.chain) not in native_on
        return d

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "min_usd": min_usd,
        "total_usd": round(sum(a.usd_value or 0 for a in found + dust), 2),
        "found": [row(a) for a in found],
        "dust": {"count": len(dust), "usd": round(sum(a.usd_value or 0 for a in dust), 2)},
        "unpriced": [row(a) for a in unpriced],
        "spam_hidden": len(spam),
        "skipped": skipped,
    }


def print_balances(r: dict[str, Any]) -> None:
    print(f"\n=== Balance scan  {r['generated_at']} ===")
    print(f"Total value found: ${r['total_usd']:,.2f}\n")
    if r["found"]:
        print(f"{'Wallet':12} {'Chain':11} {'Amount':>16} {'Token':10} {'USD':>12}  Notes")
        for a in r["found"]:
            notes = []
            if not a["home_chain"]:
                notes.append("FORGOTTEN? not on the chain you listed")
            if a["needs_gas"]:
                notes.append(f"needs {a['chain']} gas coin to move")
            amount = f"{a['amount']:,.6f}".rstrip("0").rstrip(".")
            print(f"{a['wallet_label'][:12]:12} {a['chain'][:11]:11} {amount:>16} "
                  f"{a['symbol'][:10]:10} {a['usd_value']:>12,.2f}  {'; '.join(notes)}")
    else:
        print(f"Nothing worth ${r['min_usd']:g}+ found.")
    d = r["dust"]
    if d["count"]:
        print(f"\nDust: {d['count']} small balance(s) under ${r['min_usd']:g}, "
              f"${d['usd']:,.2f} total.")
    if r["unpriced"]:
        print(f"Unpriced: {len(r['unpriced'])} token(s) with no market price - often "
              f"worthless or scam airdrops. Details in the JSON report.")
    if r["spam_hidden"]:
        print(f"Hidden: {r['spam_hidden']} token(s) that look like scam airdrops. "
              f"Never visit their links or try to sell them.")
    for s in r["skipped"]:
        print(f"  ! skipped {s}", file=sys.stderr)
    print("\nTo move funds, use your own wallet app. Never enter your seed phrase "
          "on a website to 'claim' or 'recover' anything.")


def run_balances(cfg: dict[str, Any], http: Http, demo: Path | None, quiet: bool) -> int:
    if demo:
        chain_src = btc_src = DemoBalanceSource(demo)
    else:
        # Alchemy also prices Bitcoin, so it's needed even for BTC-only configs.
        chain_src = AlchemySource(http, os.environ.get("ALCHEMY_API_KEY", ""))
        btc_src = MempoolSource(http)
    report = scan_balances(cfg, chain_src, btc_src)
    out = Path(cfg.get("balances", {}).get("report_path", DEFAULTS["report_path"]))
    if not out.is_absolute():
        out = cfg["base_dir"] / out
    out.write_text(json.dumps(report, indent=2))
    if not quiet:
        print_balances(report)
    return 0
