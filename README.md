# Kalshi BTC 15m — Paper Trading Demo

**⚠️ PAPER / DINERO FALSO ONLY** — This app never places real Kalshi orders, never deposits funds, and needs **no API keys**. It uses public market data only and keeps a fake bankroll in SQLite.

Practice `KXBTC15M` (Bitcoin up/down 15-minute markets) with live public prices and the Omega fee formula.

## Local run

```bash
cd kalshi-btc15m-paper
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Open http://127.0.0.1:8000

## Render deploy

1. New **Web Service** from this repo (or this folder).
2. Build: `pip install -r requirements.txt`
3. Start: uses `Procfile` → `uvicorn app:app --host 0.0.0.0 --port $PORT`
4. Runtime: `runtime.txt` (Python 3.12.x)
5. No env secrets required. Disk is ephemeral on free tier — `paper.db` resets on redeploy (expected for a demo).

## Smoke test note

With network access, `GET /api/market` should return the soonest open `KXBTC15M` market (ticker, strike, YES/NO asks, fee examples, optional BTC spot from OKX). Health: `GET /api/health`.

## API sketch

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Dark trading UI |
| GET | `/api/health` | ok + paper cash |
| GET | `/api/market` | live market + fees + spot + countdown |
| GET | `/api/paper/state` | cash, positions, trades, PnL |
| POST | `/api/paper/buy` | `{side, contracts}` fill at live ask |
| POST | `/api/paper/sell` | close at live bid (optional) |
| POST | `/api/paper/reset` | reset to $100 |

Fee (Omega): `fee_cents = 7.0 * p * (1-p)` per contract, `p` = fill price in 0..1.

**Never wire real Kalshi trading or deposits into this project.**

## Smoke test (verified)

With network: importing `app` and calling `get_live_market()` returned an open `KXBTC15M-*` market (YES/NO asks, strike, countdown, OKX BTC spot). `/api/health`, paper buy/reset, and the HTML UI also passed a local TestClient check. PAPER ONLY — no real orders.
