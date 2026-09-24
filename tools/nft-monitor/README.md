# NFT Monitor

A read-only script that watches the NFTs and art tokens in all your wallets and
scans the market for price gaps you can act on.

- **No private keys, ever.** It only needs your *public* wallet addresses. It
  never signs, buys, sells or approves anything. If you paste something that
  looks like a private key or seed phrase into the config, it refuses to start.
- **Nothing to install.** Python 3.11+ standard library only.
- **Chains:** Ethereum and EVM L2s (Base, Polygon, Arbitrum, Optimism, Zora, …)
  via the OpenSea API v2; Solana via the Magic Eden API v2.

## What it finds

| Alert | What it means | What you might do |
|---|---|---|
| 🚨 `own_below_offer` | One of **your** listings is priced *below* the best offer. Bots can buy it and flip it instantly. | Raise or cancel the listing, or just accept the offer. |
| 💰 `offer_over_floor` | The best collection offer, after fees and gas, is higher than the cheapest listing. | Buy the floor item, then accept the offer. |
| 💰 `underpriced` | A listing is far below the next listings (default: 15% under the 3rd-cheapest). | Buy it and relist near the market price. |
| 💰 `take_profit` | The best offer beats your cost basis by your target %. | Consider selling. |
| ⚠️ `own_underpriced` | Your listing is far under the market. | Reprice it. |
| ⚠️ / ℹ️ `floor_move` | The floor moved more than N% since the last scan. | Review your position. |
| ⚠️ `scam_airdrop` | A token in your wallet looks like a phishing "claim" airdrop. | **Don't touch it.** Hide it. Never visit its links or sign anything. |

Every profit estimate already subtracts the marketplace fee, creator royalties
and gas. Each report also shows your portfolio value two ways: **floor value**
(what the market lists it at) and **sell-now** (what you'd get from the best
offers today, after fees).

## Quick start

```bash
cd tools/nft-monitor

# 1. See it working, with sample data and no network or keys needed
python3 nft_monitor.py --demo

# 2. Set up your own config
cp config.example.toml config.toml     # add your PUBLIC addresses
export OPENSEA_API_KEY=...              # free key from docs.opensea.io

# 3. Scan once, or keep watching
python3 nft_monitor.py
python3 nft_monitor.py --watch --interval 300
```

Optional alerts on your phone:

```bash
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
export TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...
```

Run it on a schedule with cron instead of `--watch` (every 10 minutes):

```cron
*/10 * * * * cd /path/to/tools/nft-monitor && OPENSEA_API_KEY=... python3 nft_monitor.py --quiet
```

Each run writes `nft_monitor_report.json` (full portfolio and alerts) and keeps
floor history plus alert de-duplication in `nft_monitor_state.db`. Both files,
and your `config.toml`, are git-ignored so your addresses stay local.

## Tuning

Everything is in `config.toml`: thresholds under `[settings]`, fees, gas and
minimum profit per chain under `[chains.<name>]`, your purchase prices under
`[cost_basis]`, and collections to scan even if you don't hold them under
`[[watch]]`. The defaults are deliberately conservative: fees are assumed high
so that estimated profits err on the low side.

## Before you act on an alert

These are leads, not guarantees. Always check on the marketplace itself:

1. **Prices move in seconds.** Another buyer may already have taken the item.
2. **Check the contract address** matches the real collection. Copycat
   collections with the same name are common.
3. **Check the offer currency and expiry.** Collection offers are usually in
   WETH and can be cancelled at any time.
4. **Only ever sign transactions on the marketplace's real domain**, and read
   what you're approving. No legitimate opportunity requires sending your seed
   phrase to anyone.
5. Flipping NFTs is taxable in most places. Keep the JSON reports as records.

## Tests

```bash
python3 -m unittest -v test_nft_monitor
```
