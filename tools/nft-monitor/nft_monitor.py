#!/usr/bin/env python3
"""
nft_monitor.py - read-only NFT portfolio monitor and market discrepancy scanner.

What it does
  * Pulls every NFT held by the wallets listed in your config (EVM chains via
    the OpenSea API v2, Solana via the Magic Eden API v2).
  * Values the portfolio two ways: at floor (what it's "worth") and at best
    offer after fees (what you could get right now).
  * Scans each held/watched collection for money-making discrepancies:
      - offer_over_floor   buy the floor listing, sell into a higher collection
                           offer, net of fees and gas
      - underpriced        a listing sits well below the next few listings
      - own_below_offer    one of YOUR listings is priced under the best bid
                           (someone can buy yours and flip it instantly)
      - own_underpriced    one of YOUR listings is far below the market
      - take_profit        best offer beats your cost basis by your target
      - floor_move         floor moved more than N% since the last scan
      - scam_airdrop       held token looks like a phishing / "claim" airdrop
  * Sends alerts to the console, a JSON report, and optionally Discord and
    Telegram. Alerts are de-duplicated with a cooldown.

What it never does
  * It never asks for, reads, stores or uses private keys or seed phrases.
    It only needs PUBLIC wallet addresses and never signs or sends anything.
    Every opportunity is a lead for you to verify and execute by hand.

Usage
  python3 nft_monitor.py --config config.toml            # one scan
  python3 nft_monitor.py --config config.toml --watch    # scan forever
  python3 nft_monitor.py --demo                          # offline sample run
  python3 nft_monitor.py --config config.toml --balances # find forgotten crypto

Requires Python 3.11+ and nothing outside the standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
USER_AGENT = "nft-monitor/1.0 (+read-only)"

NATIVE_SYMBOL = {
    "ethereum": "ETH", "base": "ETH", "arbitrum": "ETH", "optimism": "ETH",
    "zora": "ETH", "blast": "ETH", "shape": "ETH", "abstract": "ETH",
    "matic": "POL", "polygon": "POL", "avalanche": "AVAX", "ape_chain": "APE",
    "solana": "SOL",
}
# OpenSea's API uses "matic" for Polygon.
OPENSEA_CHAIN_ALIAS = {"polygon": "matic"}
LAMPORTS_PER_SOL = 1_000_000_000

SCAM_NAME_RE = re.compile(
    r"(claim|reward|airdrop|voucher|giveaway|redeem|free\s*mint|visit|"
    r"https?://|www\.|\.(com|io|xyz|org|net|app|site|live)\b|\$\s?\d)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Holding:
    chain: str
    wallet: str
    wallet_label: str
    collection: str
    token_id: str
    name: str = ""
    contract: str = ""
    flagged_spam: bool = False


@dataclass
class Listing:
    token_id: str
    price: float          # native units, per item
    seller: str = ""
    marketplace: str = ""
    url: str = ""


@dataclass
class MarketSnapshot:
    chain: str
    collection: str
    name: str = ""
    floor: float | None = None
    best_offer: float | None = None     # per item, native units
    listings: list[Listing] = field(default_factory=list)  # cheapest first
    creator_fee_pct: float = 0.0
    verified: bool = False
    source: str = ""


@dataclass
class Alert:
    kind: str
    severity: str         # info | opportunity | warning | critical
    chain: str
    collection: str
    message: str
    est_profit: float | None = None
    token_id: str = ""
    url: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.chain}:{self.collection}:{self.token_id}"


# --------------------------------------------------------------------------- #
# Config and safety checks
# --------------------------------------------------------------------------- #

DEFAULT_SETTINGS = {
    "listings_depth": 20,         # how many cheapest listings to pull
    "reference_rank": 2,          # check the N cheapest vs the (N+1)th listing
    "underpriced_pct": 15.0,      # flag listings this % below the reference
    "floor_move_pct": 10.0,
    "take_profit_pct": 25.0,
    "alert_cooldown_minutes": 60,
    "interval_seconds": 300,
    "request_delay_seconds": 0.35,
    "state_db": "nft_monitor_state.db",
    "report_path": "nft_monitor_report.json",
}
DEFAULT_CHAIN = {
    # Conservative defaults. Tune per chain in [chains.<name>].
    "platform_fee_pct": 2.5,
    "gas": 0.0005,
    "min_profit": 0.01,
}
DEFAULT_CHAIN_OVERRIDES = {
    "ethereum": {"gas": 0.003, "min_profit": 0.02},
    "solana": {"platform_fee_pct": 2.0, "gas": 0.00002, "min_profit": 0.2},
    "matic": {"gas": 0.05, "min_profit": 10.0},
    "polygon": {"gas": 0.05, "min_profit": 10.0},
}

_HEX_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_EVM_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOL_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_BTC_ADDR_RE = re.compile(
    r"^(bc1[02-9ac-hj-np-z]{11,71}|[13][1-9A-HJ-NP-Za-km-z]{25,34})$")
# Chains tracked for balances only (no NFT marketplace scan).
BALANCE_ONLY_CHAINS = {"bitcoin"}


class ConfigError(Exception):
    pass


def looks_like_secret(value: str) -> bool:
    """True for anything shaped like a private key or a seed phrase."""
    v = value.strip()
    if _HEX_KEY_RE.match(v):
        return True
    words = v.split()
    if len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() for w in words):
        return True
    # Base58 Solana secret keys are ~87-88 chars; addresses are 32-44.
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{80,90}", v):
        return True
    # Bitcoin WIF private keys and extended private keys.
    if re.fullmatch(r"[5KL][1-9A-HJ-NP-Za-km-z]{50,51}", v):
        return True
    if re.match(r"^[xyzt]prv[1-9A-HJ-NP-Za-km-z]{100,}$", v):
        return True
    return False


def validate_address(chain: str, address: str) -> None:
    if looks_like_secret(address):
        raise ConfigError(
            "A wallet entry looks like a PRIVATE KEY or SEED PHRASE. Remove it "
            "now and treat that wallet as compromised if it was ever shared. "
            "This tool only needs public addresses."
        )
    if chain == "solana":
        if not _SOL_ADDR_RE.match(address):
            raise ConfigError(f"Not a valid Solana address: {address!r}")
    elif chain == "bitcoin":
        if not _BTC_ADDR_RE.match(address):
            raise ConfigError(f"Not a valid Bitcoin address: {address!r}")
    elif not _EVM_ADDR_RE.match(address):
        raise ConfigError(f"Not a valid EVM address for {chain}: {address!r}")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    cfg: dict[str, Any] = {"settings": {**DEFAULT_SETTINGS, **raw.get("settings", {})}}

    wallets = raw.get("wallets", [])
    if not wallets:
        raise ConfigError("Add at least one [[wallets]] entry to the config.")
    for w in wallets:
        w["chain"] = w.get("chain", "ethereum").lower()
        validate_address(w["chain"], w["address"])
        w.setdefault("label", w["address"][:8])
    cfg["wallets"] = wallets

    cfg["watch"] = [
        {"chain": x.get("chain", "ethereum").lower(), "collection": x["collection"]}
        for x in raw.get("watch", [])
    ]
    cfg["cost_basis"] = raw.get("cost_basis", {})
    cfg["chains"] = raw.get("chains", {})
    cfg["balances"] = raw.get("balances", {})
    cfg["base_dir"] = path.resolve().parent
    return cfg


def chain_params(cfg: dict[str, Any], chain: str) -> dict[str, float]:
    return {
        **DEFAULT_CHAIN,
        **DEFAULT_CHAIN_OVERRIDES.get(chain, {}),
        **cfg.get("chains", {}).get(chain, {}),
    }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Http:
    def __init__(self, delay: float = 0.35):
        self.delay = delay
        self._last = 0.0

    def get_json(self, url: str, headers: dict[str, str] | None = None,
                 body: dict | None = None) -> Any:
        hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        for attempt in range(5):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            req = urllib.request.Request(url, data=data, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode() or "null")
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError("unreachable")


def _units(value: str | int | float, decimals: int) -> float:
    return int(value) / (10 ** decimals)


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

class OpenSeaProvider:
    """EVM chains through the OpenSea API v2 (free key: docs.opensea.io)."""

    BASE = "https://api.opensea.io/api/v2"

    def __init__(self, http: Http, api_key: str, depth: int):
        if not api_key:
            raise ConfigError("Set OPENSEA_API_KEY to scan EVM wallets.")
        self.http, self.depth = http, depth
        self.headers = {"X-API-KEY": api_key}
        self._collections: dict[str, dict] = {}

    def _get(self, path: str, **params: Any) -> Any:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self.http.get_json(f"{self.BASE}{path}{'?' + q if q else ''}", self.headers)

    def holdings(self, chain: str, address: str, label: str) -> list[Holding]:
        api_chain = OPENSEA_CHAIN_ALIAS.get(chain, chain)
        out, cursor = [], None
        while True:
            data = self._get(f"/chain/{api_chain}/account/{address}/nfts",
                             limit=200, next=cursor)
            for n in data.get("nfts", []):
                out.append(Holding(
                    chain=chain, wallet=address, wallet_label=label,
                    collection=n.get("collection") or n.get("contract", ""),
                    token_id=str(n.get("identifier", "")),
                    name=n.get("name") or "", contract=n.get("contract", ""),
                    flagged_spam=bool(n.get("is_disabled")),
                ))
            cursor = data.get("next")
            if not cursor:
                return out

    def _collection(self, slug: str) -> dict:
        if slug not in self._collections:
            try:
                self._collections[slug] = self._get(f"/collections/{slug}")
            except urllib.error.HTTPError:
                self._collections[slug] = {}
        return self._collections[slug]

    def snapshot(self, chain: str, slug: str) -> MarketSnapshot:
        meta = self._collection(slug)
        snap = MarketSnapshot(
            chain=chain, collection=slug, name=meta.get("name", slug),
            creator_fee_pct=sum(float(f.get("fee", 0)) for f in meta.get("fees", [])
                                if f.get("required")),
            verified=meta.get("safelist_status") in ("verified", "approved"),
            source="opensea",
        )
        stats = self._get(f"/collections/{slug}/stats")
        snap.floor = (stats.get("total") or {}).get("floor_price")

        listings = self._get(f"/listings/collection/{slug}/best", limit=self.depth)
        for item in listings.get("listings", []):
            cur = item["price"]["current"]
            params = item.get("protocol_data", {}).get("parameters", {})
            offer = (params.get("offer") or [{}])[0]
            token_id = str(offer.get("identifierOrCriteria", ""))
            snap.listings.append(Listing(
                token_id=token_id,
                price=_units(cur["value"], cur["decimals"]),
                seller=params.get("offerer", "").lower(),
                marketplace="opensea",
                url=f"https://opensea.io/assets/{OPENSEA_CHAIN_ALIAS.get(chain, chain)}/"
                    f"{offer.get('token', '')}/{token_id}",
            ))
        snap.listings.sort(key=lambda l: l.price)

        offers = self._get(f"/offers/collection/{slug}")
        best = None
        for o in offers.get("offers", []):
            price = o.get("price", {})
            qty = 1
            for c in o.get("protocol_data", {}).get("parameters", {}).get("consideration", []):
                if c.get("itemType") in (4, 5):  # ERC721/1155 with criteria
                    qty = max(1, int(c.get("startAmount", 1)))
            per_unit = _units(price.get("value", 0), price.get("decimals", 18)) / qty
            best = per_unit if best is None else max(best, per_unit)
        snap.best_offer = best
        return snap


class MagicEdenProvider:
    """Solana through the Magic Eden public API v2."""

    BASE = "https://api-mainnet.magiceden.dev/v2"

    def __init__(self, http: Http, api_key: str, depth: int):
        self.http, self.depth = http, depth
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def _get(self, path: str, **params: Any) -> Any:
        q = urllib.parse.urlencode(params)
        return self.http.get_json(f"{self.BASE}{path}{'?' + q if q else ''}", self.headers)

    def holdings(self, chain: str, address: str, label: str) -> list[Holding]:
        out, offset = [], 0
        while True:
            page = self._get(f"/wallets/{address}/tokens", offset=offset, limit=500)
            for t in page or []:
                out.append(Holding(
                    chain="solana", wallet=address, wallet_label=label,
                    collection=t.get("collection") or "",
                    token_id=t.get("mintAddress", ""), name=t.get("name", ""),
                ))
            if not page or len(page) < 500:
                return out
            offset += 500

    def snapshot(self, chain: str, symbol: str) -> MarketSnapshot:
        stats = self._get(f"/collections/{symbol}/stats")
        snap = MarketSnapshot(chain="solana", collection=symbol, name=symbol,
                              source="magiceden")
        if stats.get("floorPrice") is not None:
            snap.floor = stats["floorPrice"] / LAMPORTS_PER_SOL
        for l in self._get(f"/collections/{symbol}/listings", offset=0,
                           limit=min(self.depth, 20)) or []:
            snap.listings.append(Listing(
                token_id=l.get("tokenMint", ""), price=float(l.get("price", 0)),
                seller=l.get("seller", ""), marketplace="magiceden",
                url=f"https://magiceden.io/item-details/{l.get('tokenMint', '')}",
            ))
        snap.listings.sort(key=lambda l: l.price)
        return snap


class DemoProvider:
    """Offline provider fed from fixtures/demo.json (no network, no keys)."""

    def __init__(self, path: Path):
        self.data = json.loads(path.read_text())

    def holdings(self, chain: str, address: str, label: str) -> list[Holding]:
        return [Holding(chain=chain, wallet=address, wallet_label=label, **h)
                for h in self.data["holdings"].get(address, [])]

    def snapshot(self, chain: str, collection: str) -> MarketSnapshot:
        s = dict(self.data["markets"][f"{chain}:{collection}"])
        s["listings"] = [Listing(**l) for l in s.get("listings", [])]
        return MarketSnapshot(chain=chain, collection=collection, source="demo", **s)


# --------------------------------------------------------------------------- #
# State (floor history + alert de-duplication)
# --------------------------------------------------------------------------- #

class State:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path))
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS floors (
                chain TEXT, collection TEXT, ts REAL, floor REAL);
            CREATE INDEX IF NOT EXISTS floors_idx ON floors (chain, collection, ts);
            CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, ts REAL);
        """)

    def last_floor(self, chain: str, collection: str) -> float | None:
        row = self.db.execute(
            "SELECT floor FROM floors WHERE chain=? AND collection=? "
            "ORDER BY ts DESC LIMIT 1", (chain, collection)).fetchone()
        return row[0] if row else None

    def record_floor(self, chain: str, collection: str, floor: float) -> None:
        self.db.execute("INSERT INTO floors VALUES (?,?,?,?)",
                        (chain, collection, time.time(), floor))
        self.db.commit()

    def should_alert(self, key: str, cooldown_s: float) -> bool:
        row = self.db.execute("SELECT ts FROM alerts WHERE key=?", (key,)).fetchone()
        if row and time.time() - row[0] < cooldown_s:
            return False
        self.db.execute("INSERT OR REPLACE INTO alerts VALUES (?,?)", (key, time.time()))
        self.db.commit()
        return True


# --------------------------------------------------------------------------- #
# Discrepancy detectors (pure functions, easy to test)
# --------------------------------------------------------------------------- #

def net_sale(price: float, fee_pct: float) -> float:
    return price * (1 - fee_pct / 100)


def detect(snap: MarketSnapshot, holdings: list[Holding], my_wallets: set[str],
           settings: dict[str, Any], chain_cfg: dict[str, float],
           cost_basis: float | None, previous_floor: float | None) -> list[Alert]:
    alerts: list[Alert] = []
    sym = NATIVE_SYMBOL.get(snap.chain, "")
    fee = chain_cfg["platform_fee_pct"] + snap.creator_fee_pct
    gas, min_profit = chain_cfg["gas"], chain_cfg["min_profit"]
    label = snap.name or snap.collection

    def mk(kind, sev, msg, profit=None, token_id="", url=""):
        alerts.append(Alert(kind, sev, snap.chain, snap.collection, msg,
                            None if profit is None else round(profit, 6), token_id, url))

    mine = [l for l in snap.listings if l.seller.lower() in my_wallets]
    others = [l for l in snap.listings if l.seller.lower() not in my_wallets]

    # 1. Buy the floor, sell into a collection offer.
    if others and snap.best_offer:
        cheapest = others[0]
        profit = net_sale(snap.best_offer, fee) - cheapest.price - 2 * gas
        if profit >= min_profit:
            mk("offer_over_floor", "opportunity",
               f"{label}: buy #{cheapest.token_id} at {cheapest.price:g} {sym}, accept "
               f"collection offer {snap.best_offer:g} {sym} -> ~{profit:.4f} {sym} net "
               f"after {fee:.1f}% fees + gas", profit, cheapest.token_id, cheapest.url)

    # 2. Listings far below the market. The reference is the (rank+1)-th
    #    cheapest listing, so one dumped item can't drag the reference down.
    rank = int(settings["reference_rank"])
    if len(others) > rank:
        ref = others[rank].price
        for l in others[:rank]:
            gap = (ref - l.price) / ref * 100 if ref else 0
            profit = net_sale(ref, fee) - l.price - gas
            if gap >= settings["underpriced_pct"] and profit >= min_profit:
                mk("underpriced", "opportunity",
                   f"{label}: #{l.token_id} listed {l.price:g} {sym}, {gap:.0f}% under the "
                   f"next listings (~{ref:g} {sym}); relist est. ~{profit:.4f} {sym} net",
                   profit, l.token_id, l.url)

    # 3/4. Your own listings priced too low.
    for l in mine:
        if snap.best_offer and l.price < snap.best_offer:
            mk("own_below_offer", "critical",
               f"{label}: YOUR listing #{l.token_id} at {l.price:g} {sym} is BELOW the best "
               f"offer {snap.best_offer:g} {sym}. Anyone can buy it and flip it instantly - "
               f"raise or cancel it.", snap.best_offer - l.price, l.token_id, l.url)
        elif others:
            ref = others[min(rank, len(others)) - 1].price
            gap = (ref - l.price) / ref * 100 if ref else 0
            if gap >= settings["underpriced_pct"]:
                mk("own_underpriced", "warning",
                   f"{label}: YOUR listing #{l.token_id} at {l.price:g} {sym} is {gap:.0f}% "
                   f"under the market (~{ref:g} {sym}).", ref - l.price, l.token_id, l.url)

    # 5. Take profit on held items.
    held = [h for h in holdings if h.collection == snap.collection]
    if held and cost_basis and snap.best_offer:
        instant = net_sale(snap.best_offer, fee)
        target = cost_basis * (1 + settings["take_profit_pct"] / 100)
        if instant >= target:
            mk("take_profit", "opportunity",
               f"{label}: best offer nets {instant:.4f} {sym} vs cost basis {cost_basis:g} "
               f"{sym} (+{(instant / cost_basis - 1) * 100:.0f}%) on {len(held)} held item(s)",
               (instant - cost_basis) * len(held))

    # 6. Floor moves.
    if snap.floor and previous_floor:
        move = (snap.floor - previous_floor) / previous_floor * 100
        if abs(move) >= settings["floor_move_pct"]:
            mk("floor_move", "info" if move > 0 else "warning",
               f"{label}: floor {'up' if move > 0 else 'down'} {move:+.1f}% "
               f"({previous_floor:g} -> {snap.floor:g} {sym})")
    return alerts


def detect_scams(holdings: Iterable[Holding], markets: dict[str, MarketSnapshot]) -> list[Alert]:
    alerts = []
    for h in holdings:
        snap = markets.get(f"{h.chain}:{h.collection}")
        suspicious_name = bool(SCAM_NAME_RE.search(f"{h.name} {h.collection}"))
        dead_market = snap is None or (not snap.floor and not snap.best_offer)
        if h.flagged_spam or (suspicious_name and dead_market and not (snap and snap.verified)):
            alerts.append(Alert(
                "scam_airdrop", "warning", h.chain, h.collection,
                f"'{h.name or h.collection}' in {h.wallet_label} looks like a phishing "
                f"airdrop. Do NOT visit its links, sign, approve or try to sell it - just "
                f"hide it.", token_id=h.token_id))
    return alerts


# --------------------------------------------------------------------------- #
# Portfolio valuation
# --------------------------------------------------------------------------- #

def value_portfolio(holdings: list[Holding], markets: dict[str, MarketSnapshot],
                    cfg: dict[str, Any]) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    totals: dict[str, dict[str, float]] = {}
    for h in holdings:
        key = f"{h.chain}:{h.collection}"
        snap = markets.get(key)
        row = rows.setdefault(key, {
            "chain": h.chain, "collection": h.collection,
            "name": snap.name if snap else h.collection,
            "count": 0, "floor": snap.floor if snap else None,
            "best_offer": snap.best_offer if snap else None,
            "wallets": set(),
        })
        row["count"] += 1
        row["wallets"].add(h.wallet_label)
    for row in rows.values():
        snap = markets.get(f"{row['chain']}:{row['collection']}")
        fee = chain_params(cfg, row["chain"])["platform_fee_pct"] + (snap.creator_fee_pct if snap else 0)
        row["floor_value"] = (row["floor"] or 0) * row["count"]
        row["instant_value"] = net_sale(row["best_offer"] or 0, fee) * row["count"]
        row["wallets"] = sorted(row["wallets"])
        sym = NATIVE_SYMBOL.get(row["chain"], row["chain"])
        t = totals.setdefault(sym, {"floor_value": 0.0, "instant_value": 0.0})
        t["floor_value"] += row["floor_value"]
        t["instant_value"] += row["instant_value"]
    ordered = sorted(rows.values(), key=lambda r: -r["floor_value"])
    return {"collections": ordered, "totals": totals}


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

SEV_ICON = {"critical": "🚨", "opportunity": "💰", "warning": "⚠️", "info": "ℹ️"}


def notify(alerts: list[Alert], http: Http) -> None:
    if not alerts:
        return
    text = "\n".join(f"{SEV_ICON.get(a.severity, '')} [{a.kind}] {a.message}"
                     + (f"\n{a.url}" if a.url else "") for a in alerts)
    discord = os.environ.get("DISCORD_WEBHOOK_URL")
    if discord:
        for chunk in _chunks(text, 1900):
            _post(http, discord, {"content": chunk})
    tg_token, tg_chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        for chunk in _chunks(text, 3900):
            _post(http, f"https://api.telegram.org/bot{tg_token}/sendMessage",
                  {"chat_id": tg_chat, "text": chunk, "disable_web_page_preview": True})


def _chunks(text: str, size: int) -> Iterable[str]:
    while text:
        yield text[:size]
        text = text[size:]


def _post(http: Http, url: str, body: dict) -> None:
    try:
        http.get_json(url, body=body)
    except Exception as exc:  # never let a notifier kill the scan
        print(f"  ! notification failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Scan orchestration
# --------------------------------------------------------------------------- #

def build_providers(cfg: dict[str, Any], http: Http, demo: Path | None) -> dict[str, Any]:
    if demo:
        p = DemoProvider(demo)
        return {"evm": p, "solana": p}
    depth = int(cfg["settings"]["listings_depth"])
    providers: dict[str, Any] = {}
    chains = ({w["chain"] for w in cfg["wallets"]} | {w["chain"] for w in cfg["watch"]}) \
        - BALANCE_ONLY_CHAINS
    if any(c != "solana" for c in chains):
        providers["evm"] = OpenSeaProvider(http, os.environ.get("OPENSEA_API_KEY", ""), depth)
    if "solana" in chains:
        providers["solana"] = MagicEdenProvider(http, os.environ.get("MAGICEDEN_API_KEY", ""), depth)
    return providers


def provider_for(providers: dict[str, Any], chain: str):
    return providers["solana" if chain == "solana" else "evm"]


def scan(cfg: dict[str, Any], providers: dict[str, Any], state: State) -> dict[str, Any]:
    s = cfg["settings"]
    errors: list[str] = []
    holdings: list[Holding] = []

    for w in cfg["wallets"]:
        if w["chain"] in BALANCE_ONLY_CHAINS:
            continue
        try:
            holdings += provider_for(providers, w["chain"]).holdings(
                w["chain"], w["address"], w["label"])
        except Exception as exc:
            errors.append(f"holdings {w['label']} ({w['chain']}): {exc}")

    my_wallets = {w["address"].lower() for w in cfg["wallets"]} | \
                 {w["address"] for w in cfg["wallets"]}
    targets = {(h.chain, h.collection) for h in holdings
               if h.collection and not h.flagged_spam}
    targets |= {(w["chain"], w["collection"]) for w in cfg["watch"]}

    markets: dict[str, MarketSnapshot] = {}
    alerts: list[Alert] = []
    for chain, coll in sorted(targets):
        key = f"{chain}:{coll}"
        try:
            snap = provider_for(providers, chain).snapshot(chain, coll)
        except Exception as exc:
            errors.append(f"market {key}: {exc}")
            continue
        markets[key] = snap
        prev = state.last_floor(chain, coll)
        alerts += detect(snap, holdings, my_wallets, s, chain_params(cfg, chain),
                         cfg["cost_basis"].get(key), prev)
        if snap.floor:
            state.record_floor(chain, coll, snap.floor)

    alerts += detect_scams(holdings, markets)
    order = {"critical": 0, "opportunity": 1, "warning": 2, "info": 3}
    alerts.sort(key=lambda a: (order.get(a.severity, 9), -(a.est_profit or 0)))

    cooldown = float(s["alert_cooldown_minutes"]) * 60
    fresh = [a for a in alerts if state.should_alert(a.key, cooldown)]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wallets": len(cfg["wallets"]), "items_held": len(holdings),
        "portfolio": value_portfolio(holdings, markets, cfg),
        "alerts": [asdict(a) for a in alerts],
        "new_alerts": [asdict(a) for a in fresh],
        "errors": errors,
    }


def print_report(report: dict[str, Any]) -> None:
    p = report["portfolio"]
    print(f"\n=== NFT Monitor  {report['generated_at']} ===")
    print(f"{report['items_held']} items across {report['wallets']} wallet(s)\n")
    print(f"{'Collection':32} {'Chain':9} {'Qty':>4} {'Floor':>10} {'BestOffer':>10} "
          f"{'FloorValue':>11} {'SellNow':>10}")
    for r in p["collections"]:
        f = lambda v: "-" if v is None else f"{v:.4g}"
        print(f"{(r['name'] or r['collection'])[:32]:32} {r['chain'][:9]:9} {r['count']:>4} "
              f"{f(r['floor']):>10} {f(r['best_offer']):>10} {r['floor_value']:>11.4f} "
              f"{r['instant_value']:>10.4f}")
    for sym, t in p["totals"].items():
        print(f"  TOTAL {sym}: floor value {t['floor_value']:.4f} | sell-now (after fees) "
              f"{t['instant_value']:.4f}")

    print(f"\n--- Alerts ({len(report['alerts'])}, {len(report['new_alerts'])} new) ---")
    for a in report["alerts"]:
        print(f"{SEV_ICON.get(a['severity'], '')} [{a['kind']}] {a['message']}")
        if a["url"]:
            print(f"     {a['url']}")
    for e in report["errors"]:
        print(f"  ! {e}", file=sys.stderr)
    print("\nLeads only - verify on the marketplace before buying, selling or signing.")


def run_once(cfg, providers, state, http, quiet=False) -> dict[str, Any]:
    report = scan(cfg, providers, state)
    out = Path(cfg["settings"]["report_path"])
    if not out.is_absolute():
        out = cfg["base_dir"] / out
    out.write_text(json.dumps(report, indent=2, default=list))
    if not quiet:
        print_report(report)
    notify([Alert(**a) for a in report["new_alerts"]], http)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=HERE / "config.toml")
    ap.add_argument("--watch", action="store_true", help="scan continuously")
    ap.add_argument("--interval", type=int, help="seconds between scans in --watch mode")
    ap.add_argument("--demo", action="store_true", help="offline run using fixtures/")
    ap.add_argument("--quiet", action="store_true", help="only write the JSON report")
    ap.add_argument("--balances", action="store_true",
                    help="find coins and tokens (incl. forgotten ones) in every wallet")
    args = ap.parse_args(argv)

    demo = None
    if args.demo:
        args.config = HERE / "fixtures" / "demo_config.toml"
        demo = HERE / "fixtures" / "demo.json"
    try:
        cfg = load_config(args.config)
    except (ConfigError, FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    http = Http(float(cfg["settings"]["request_delay_seconds"]))
    if args.balances:
        from balances import run_balances
        try:
            return run_balances(cfg, http, demo and HERE / "fixtures" / "demo_balances.json",
                                args.quiet)
        except ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            return 2
    try:
        providers = build_providers(cfg, http, demo)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    db = Path(cfg["settings"]["state_db"])
    state = State(":memory:" if demo else (db if db.is_absolute() else cfg["base_dir"] / db))

    interval = args.interval or int(cfg["settings"]["interval_seconds"])
    while True:
        run_once(cfg, providers, state, http, args.quiet)
        if not args.watch:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    # Let balances.py's `import nft_monitor` reuse this module instead of
    # loading a second copy with its own ConfigError class.
    sys.modules.setdefault("nft_monitor", sys.modules[__name__])
    sys.exit(main())
