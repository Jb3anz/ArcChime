# ArcChime

**Signed webhooks for USDC payments on Arc.** Register an address and a URL; ArcChime watches Arc for USDC arriving at that address and POSTs a signed, normalized `payment.received` event to your server, with retries, replay, and a reorg check.

> Status: unaudited proof of concept. Tested on Arc testnet. See [Limitations](#limitations).

## Why this exists (and why not just use a hosted webhook provider?)

Hosted providers such as QuickNode already offer address webhooks for Arc, and they are a fine choice if you want a managed service. ArcChime is for the cases where you want something you can **self-host in one command, with no account or API key**, and that speaks in **payments, not raw logs**:

- **One event per payment, in plain USDC.** Arc moves USDC two ways and logs them differently (below). Handling that by hand is an easy way to double-count or mis-scale a payment by 10^12.
- **Open source and small.** About 900 lines of Python (including the demo shop), SQLite, no web3 dependency.
- **Delivery semantics you can reason about:** at-least-once, stable event ids, signed with a timestamp, retries with backoff, manual replay, and a check that the block is still canonical before the first delivery.

## What we learned about Arc (verified on testnet)

USDC is Arc's gas token, so the same balance is reachable through two interfaces, and they emit different logs:

| How USDC moved | Logs emitted | Raw decimals |
|---|---|---|
| Native send (value transfer) | one `Transfer` from `0xFFFF…FFFE` | 18 |
| ERC-20 `transfer()` on `0x3600…0000` | **two** `Transfer` logs: `0xFFFF…FFFE` (18 dec) **and** `0x3600…0000` (6 dec) | 18 and 6 |

Arc's own docs describe the system-address event (EIP-7708) as the way to capture every native USDC movement in one stream, and also describe an earlier event behavior from before a network upgrade, so ArcChime watches both addresses and merges, which stays correct either way. (The earlier behavior is not tested here.)

A listener that watches only the token contract misses every native payment; one that watches both double-counts every ERC-20 payment. ArcChime watches both, merges the pair into a single event, and reports `amount` in plain USDC (`amount_raw` and `raw_decimals` are included for auditing).

Evidence on Arc Testnet (chain 5042002):

- Native send of 4 USDC: `0xdaabd41bb44312dcbc7f46c1ad91f1d17a91bce64b65deebd6171f3142451fb7` — one log, from the system address.
- ERC-20 `transfer()` of 1 USDC: `0x02ab357b81aeacd6f73343155ec8d16ef452508fd70a0bf5f9a274ce10dd6e32` — two logs.

Mainnet run: _add your mainnet transaction hashes here after testing on chain 5042._

## Quick start

```bash
pip install -r requirements.txt
# Windows cmd:   set ARC_RPC=...   (use `export` on Linux/macOS)
export ARC_RPC=https://rpc.testnet.arc.network
export CHAIN_ID=5042002
export ADMIN_TOKEN=some-long-random-string
uvicorn arcchime.main:app --port 8000
```

Interactive API docs are at `http://localhost:8000/docs`. Or with curl:

```bash
# 1. register (the signing secret is shown ONCE)
curl -X POST localhost:8000/v1/endpoints \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"address":"0xYourArcAddress","url":"https://your-server.example/webhook"}'

# 2. check your receiver with a signed test event
curl -X POST localhost:8000/v1/endpoints/ep_xxx/test -H "Authorization: Bearer $ADMIN_TOKEN"

# 3. send USDC to that address on Arc; the webhook arrives after CONFIRMATIONS blocks
```

Run the example receiver with `ARCCHIME_SECRET=whsec_... python examples/receiver.py`. Docker: `docker build -t arcchime . && docker run -p 8000:8000 -v arcchime-data:/data --env-file .env arcchime`.

## Demo shop (try the whole loop)

`/shop` is a small page that uses ArcChime itself: you create an order, send the exact amount shown (for example `0.010347` USDC) to the shop address on Arc, and the page unlocks within seconds. Under the hood: payment on Arc -> ArcChime event -> signed webhook to `/shop/webhook` -> order marked paid. Orders are matched by a **unique amount** (0.010001 to 0.010999 USDC), so one address can serve many visitors; an order expires after 30 minutes and orders are kept in the database only.

Enable it by setting `DEMO_ADDRESS` (a mainnet address you control, which receives the payments), `DEMO_SECRET`, and `DEMO_URL` (this server's own `/shop/webhook`). Locally:

```bash
export ARC_RPC=https://rpc.mainnet.arc.io CHAIN_ID=5042
export DEMO_ADDRESS=0xYourReceivingAddress DEMO_SECRET=some-long-random-string
export DEMO_URL=http://localhost:8000/shop/webhook
export ALLOW_PRIVATE_URLS=1 ALLOW_HTTP_URLS=1      # local testing only
uvicorn arcchime.main:app --port 8000               # then open http://localhost:8000/shop
```

On a public host use `DEMO_URL=https://<your-host>/shop/webhook` and leave the two `ALLOW_*` flags unset. Anyone can create orders (capped at 500 open orders), but unlocking requires a real payment, and `/shop/webhook` rejects anything without a valid signature.

## Deploying

ArcChime is one process (API + scanner + delivery worker) with a SQLite file. Any host that runs a long-lived Python or Docker service works.

Extra settings for hosts with a **non-persistent disk or that sleep when idle** (e.g. free tiers):

| Variable | Purpose |
|---|---|
| `BACKFILL_BLOCKS` | on a fresh start, also scan this many recent blocks (about 2.2 blocks/s, so `20000` is roughly 2.5 hours) so payments made while the service slept are not missed |
| `DEMO_ADDRESS`, `DEMO_URL`, `DEMO_SECRET` | re-register one endpoint with a fixed secret at every startup, so a restart does not lose it |
| `ADMIN_TOKEN` | **set this on any public deployment** |

Caveats on free hosting: a sleeping service does not scan, and a wiped disk loses event history. `BACKFILL_BLOCKS` covers recent gaps only. For real use, run it on an always-on host with a persistent volume (see the Dockerfile).

**On a VPS** (recommended: always on, persistent disk), follow `deploy/INSTALL.md`: a systemd unit, an env template, and Caddy and nginx examples for HTTPS behind an existing reverse proxy. `DEMO_URL` may point at the server itself (`http://127.0.0.1:<port>/shop/webhook`); it is set by the operator, so it is trusted, while webhook URLs registered through the API remain guarded against internal addresses.

A `render.yaml` blueprint is included as a starting point for free hosts. Never commit secrets: set `ADMIN_TOKEN`, `DEMO_SECRET`, and any private values in the host's dashboard.

## Event payload

```json
{
  "id": "evt_231c57ab983d5729408d241f",
  "type": "payment.received",
  "chain": "eip155:5042002",
  "token": "USDC",
  "source": "erc20",
  "from": "0xce3b…", "to": "0x8e2d…",
  "amount": "1.000000",
  "amount_raw": "1000000", "raw_decimals": 6,
  "tx_hash": "0x…", "block_number": 65920939, "block_hash": "0x…", "log_index": 1
}
```

Zero-value transfers are ignored. Addresses are lowercase.

## Verifying signatures

Every request carries `X-ArcChime-Signature: t=<unix>,v1=<hex>`, where `v1 = HMAC-SHA256(secret, "<t>." + rawBody)`. Verify against the **raw bytes**, reject timestamps older than 5 minutes, and dedupe on `id`. Working examples: `examples/receiver.py` (Python, stdlib only) and `examples/verify.js` (Node).

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/endpoints` | register `{address, url}`; returns the signing secret once |
| GET / DELETE | `/v1/endpoints[/{id}]` | list / remove |
| POST | `/v1/endpoints/{id}/test` | queue a signed `endpoint.test` event |
| GET | `/v1/events?address=` · `/v1/events/{id}` | events, deliveries, and every attempt |
| POST | `/v1/events/{id}/replay` | re-deliver (same id and payload) |
| GET | `/health` | scan cursor, lag, last error (no auth) |

## Delivery semantics

- **At-least-once.** Your receiver must dedupe on `id`.
- Success is any `2xx`. Otherwise retries after 10 s, 30 s, 2 min, 10 min, 1 h, then the delivery is marked `failed`. Replay resets it.
- Redirects are not followed.
- **Reorg check:** before the first delivery attempt, ArcChime confirms the event's block hash is still the canonical one; if not, the delivery is marked `reorged` and never sent. If the RPC is unreachable it waits instead of guessing. Since Arc's docs say reorgs cannot occur, this check is defensive.
- Ordering across events is not guaranteed.
- On first start the scanner begins at the chain tip (no backfill); after that the cursor is persisted, so a restart resumes where it stopped.

## Security notes

- **SSRF guard:** webhook URLs must be `https` and resolve to public IPs; loopback, private, link-local, and cloud-metadata ranges are rejected at registration and again before every delivery. Remaining gap: DNS is resolved once by the guard and once by the HTTP client, so DNS rebinding is not fully closed.
- Set `ADMIN_TOKEN` on any public deployment. Without it the API is open.
- Signing secrets are stored in plaintext in SQLite (they are needed to sign). Protect the database file.
- `ALLOW_PRIVATE_URLS` / `ALLOW_HTTP_URLS` exist for local testing only.

## Limitations

- **Finality.** Arc's documentation states that blocks are final once committed (BFT consensus, no reorganizations), so a single block is enough. ArcChime still waits `CONFIRMATIONS` blocks (default 2), but for a different reason: public RPC endpoints are often load-balanced across nodes at slightly different heights, and scanning a block that one node has not yet indexed could silently skip its logs. That guarantee rests on Arc's permissioned validator set (more than two thirds must be honest); ArcChime adds nothing to it and does not independently verify it.
- **Tested paths:** native sends and ERC-20 `transfer()`. **Untested:** `transferFrom`, EIP-3009 signed transfers, payments made from smart contracts (internal native transfers), mint/burn. These may or may not emit the logs ArcChime watches.
- Single process, SQLite. Not built for high availability or very large numbers of watched addresses (the address list is sent to the RPC as a log filter; providers may cap it).
- **Memos are not extracted yet.** Arc has a Memo contract that wraps a transfer to attach an order or invoice reference, but it must be called from an externally owned account (not a smart-contract wallet), the docs list different contract addresses and event shapes on different pages, and I have not verified its mainnet availability. The demo shop matches orders by unique amount instead.
- Unaudited.

## Tests

```bash
python -m unittest discover -s tests -v
```
Covers decimals handling, dedupe of the two-log case, signatures, the SSRF guard, retry/replay, the reorg check, and a full scan-to-delivery run against a fake RPC and a local receiver. The FastAPI layer is a thin wrapper over tested functions.
