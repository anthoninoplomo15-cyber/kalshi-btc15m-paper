"""
Kalshi KXBTC15M paper-trading demo — FAKE money only.
Public market data; never places real orders; no API keys.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"
OKX_TICKER = "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT"
DB_PATH = Path(__file__).resolve().parent / "paper.db"
START_CASH = 100.0
HTTP_TIMEOUT = 12.0

app = FastAPI(title="Kalshi BTC 15m Paper", docs_url="/docs")
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def dollars_field(m: dict, *keys: str, default: Optional[float] = None) -> Optional[float]:
    """Prefer *_dollars string fields; fall back to integer cents / 100."""
    for k in keys:
        if k in m and m[k] is not None:
            v = m[k]
            if isinstance(v, str):
                try:
                    return float(v)
                except ValueError:
                    continue
            if isinstance(v, (int, float)):
                # yes_ask / yes_bid historically in cents (0-100) or dollars
                if k.endswith("_dollars"):
                    return float(v)
                # integer cents style
                if float(v) > 1.5:  # likely cents
                    return float(v) / 100.0
                return float(v)
    return default


def fee_cents_per_contract(p: float) -> float:
    """Omega fee: 7.0 * p * (1-p) cents per contract. p in 0..1."""
    p = max(0.0, min(1.0, float(p)))
    return 7.0 * p * (1.0 - p)


def fee_dollars(contracts: int, p: float) -> float:
    return contracts * (fee_cents_per_contract(p) / 100.0)


# ---------------------------------------------------------------------------
# SQLite paper store
# ---------------------------------------------------------------------------
def init_db() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                avg_cost REAL NOT NULL,
                PRIMARY KEY (ticker, side)
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                action TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                price REAL NOT NULL,
                fee REAL NOT NULL,
                total REAL NOT NULL,
                note TEXT
            );
            """
        )
        row = conn.execute("SELECT value FROM meta WHERE key='cash'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('cash', ?)",
                (str(START_CASH),),
            )


@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_cash(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT value FROM meta WHERE key='cash'").fetchone()
    return float(row["value"]) if row else START_CASH


def set_cash(conn: sqlite3.Connection, cash: float) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('cash', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"{cash:.6f}",),
    )


def list_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT ticker, side, contracts, avg_cost FROM positions WHERE contracts > 0"
    ).fetchall()
    return [dict(r) for r in rows]


def list_trades(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ts, action, ticker, side, contracts, price, fee, total, note "
        "FROM trades ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_trade(
    conn: sqlite3.Connection,
    action: str,
    ticker: str,
    side: str,
    contracts: int,
    price: float,
    fee: float,
    total: float,
    note: str = "",
) -> None:
    conn.execute(
        "INSERT INTO trades(ts, action, ticker, side, contracts, price, fee, total, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            utc_now().isoformat(),
            action,
            ticker,
            side,
            contracts,
            price,
            fee,
            total,
            note,
        ),
    )


def upsert_position(
    conn: sqlite3.Connection, ticker: str, side: str, add_contracts: int, fill_price: float
) -> None:
    row = conn.execute(
        "SELECT contracts, avg_cost FROM positions WHERE ticker=? AND side=?",
        (ticker, side),
    ).fetchone()
    if row is None or row["contracts"] <= 0:
        conn.execute(
            "INSERT INTO positions(ticker, side, contracts, avg_cost) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(ticker, side) DO UPDATE SET contracts=excluded.contracts, "
            "avg_cost=excluded.avg_cost",
            (ticker, side, add_contracts, fill_price),
        )
    else:
        old_c = int(row["contracts"])
        old_avg = float(row["avg_cost"])
        new_c = old_c + add_contracts
        new_avg = ((old_avg * old_c) + (fill_price * add_contracts)) / new_c if new_c else 0.0
        conn.execute(
            "UPDATE positions SET contracts=?, avg_cost=? WHERE ticker=? AND side=?",
            (new_c, new_avg, ticker, side),
        )


def reduce_position(
    conn: sqlite3.Connection, ticker: str, side: str, sell_contracts: int
) -> tuple[int, float]:
    row = conn.execute(
        "SELECT contracts, avg_cost FROM positions WHERE ticker=? AND side=?",
        (ticker, side),
    ).fetchone()
    if not row or row["contracts"] <= 0:
        raise HTTPException(400, "No open position for that ticker/side")
    have = int(row["contracts"])
    avg = float(row["avg_cost"])
    if sell_contracts > have:
        raise HTTPException(400, f"Only {have} contracts open")
    left = have - sell_contracts
    if left == 0:
        conn.execute("DELETE FROM positions WHERE ticker=? AND side=?", (ticker, side))
    else:
        conn.execute(
            "UPDATE positions SET contracts=? WHERE ticker=? AND side=?",
            (left, ticker, side),
        )
    return sell_contracts, avg


# ---------------------------------------------------------------------------
# External data
# ---------------------------------------------------------------------------
def fetch_open_markets() -> list[dict]:
    url = f"{KALSHI_BASE}/markets"
    params = {"series_ticker": SERIES, "status": "open", "limit": 20}
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    return data.get("markets") or []


def fetch_market_by_ticker(ticker: str) -> Optional[dict]:
    url = f"{KALSHI_BASE}/markets/{ticker}"
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("market") or r.json()


def pick_soonest(markets: list[dict]) -> Optional[dict]:
    timed = []
    for m in markets:
        ct = parse_iso(m.get("close_time"))
        if ct:
            timed.append((ct, m))
    if not timed:
        return markets[0] if markets else None
    timed.sort(key=lambda x: x[0])
    return timed[0][1]


def fetch_btc_spot() -> Optional[float]:
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            r = client.get(OKX_TICKER)
            r.raise_for_status()
            data = r.json()
        arr = data.get("data") or []
        if not arr:
            return None
        return float(arr[0]["last"])
    except Exception:
        return None


def normalize_market(m: dict, spot: Optional[float] = None) -> dict[str, Any]:
    yes_ask = dollars_field(m, "yes_ask_dollars", "yes_ask", default=None)
    no_ask = dollars_field(m, "no_ask_dollars", "no_ask", default=None)
    yes_bid = dollars_field(m, "yes_bid_dollars", "yes_bid", default=None)
    no_bid = dollars_field(m, "no_bid_dollars", "no_bid", default=None)

    # If only one ask side present, derive the other as ~1 - opposite bid/ask
    if yes_ask is None and no_bid is not None:
        yes_ask = max(0.01, min(0.99, 1.0 - no_bid))
    if no_ask is None and yes_bid is not None:
        no_ask = max(0.01, min(0.99, 1.0 - yes_bid))

    close_time = m.get("close_time")
    ct = parse_iso(close_time)
    now = utc_now()
    countdown_sec = max(0, int((ct - now).total_seconds())) if ct else None

    floor_strike = m.get("floor_strike")
    try:
        strike = float(floor_strike) if floor_strike is not None else None
    except (TypeError, ValueError):
        strike = None

    spot_hint = None
    if spot is not None and strike is not None:
        diff = spot - strike
        if abs(diff) < 1:
            spot_hint = "Spot ≈ strike"
        elif diff > 0:
            spot_hint = f"Spot ${spot:,.2f} ABOVE strike (${diff:+,.2f})"
        else:
            spot_hint = f"Spot ${spot:,.2f} BELOW strike (${diff:+,.2f})"

    fee_examples = []
    for n in (1, 5, 10):
        for label, p in (("YES", yes_ask), ("NO", no_ask)):
            if p is None:
                continue
            fc = fee_cents_per_contract(p)
            fee_examples.append(
                {
                    "side": label,
                    "contracts": n,
                    "price": p,
                    "fee_cents_each": round(fc, 4),
                    "fee_total_dollars": round(n * fc / 100.0, 6),
                    "notional": round(n * p, 4),
                    "total_cost": round(n * p + n * fc / 100.0, 6),
                }
            )

    return {
        "ticker": m.get("ticker"),
        "title": m.get("title") or m.get("subtitle") or SERIES,
        "status": m.get("status"),
        "result": m.get("result"),
        "floor_strike": strike,
        "close_time": close_time,
        "countdown_sec": countdown_sec,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "yes_bid": yes_bid,
        "no_bid": no_bid,
        "yes_ask_cents": round(yes_ask * 100, 1) if yes_ask is not None else None,
        "no_ask_cents": round(no_ask * 100, 1) if no_ask is not None else None,
        "yes_bid_cents": round(yes_bid * 100, 1) if yes_bid is not None else None,
        "no_bid_cents": round(no_bid * 100, 1) if no_bid is not None else None,
        "btc_spot": spot,
        "spot_hint": spot_hint,
        "fee_examples": fee_examples,
        "raw_partial": {
            "yes_ask_dollars": m.get("yes_ask_dollars"),
            "no_ask_dollars": m.get("no_ask_dollars"),
            "yes_bid_dollars": m.get("yes_bid_dollars"),
            "no_bid_dollars": m.get("no_bid_dollars"),
        },
    }


def get_live_market() -> dict[str, Any]:
    markets = fetch_open_markets()
    m = pick_soonest(markets)
    if not m:
        raise HTTPException(404, "No open KXBTC15M markets found")
    spot = fetch_btc_spot()
    return normalize_market(m, spot)


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
def try_settle_positions() -> list[dict]:
    """Settle closed markets with a result. Winners get $1/contract."""
    settled_log: list[dict] = []
    with _lock:
        with _db() as conn:
            positions = list_positions(conn)
            if not positions:
                return settled_log
            tickers = {p["ticker"] for p in positions}
            for ticker in tickers:
                try:
                    m = fetch_market_by_ticker(ticker)
                except Exception:
                    continue
                if not m:
                    continue
                result = (m.get("result") or "").lower()
                status = (m.get("status") or "").lower()
                if result not in ("yes", "no"):
                    # also try finalized markets that still have no result string
                    if status not in ("finalized", "determined", "settled"):
                        continue
                    continue
                cash = get_cash(conn)
                for p in [x for x in positions if x["ticker"] == ticker]:
                    side = p["side"].upper()
                    contracts = int(p["contracts"])
                    avg = float(p["avg_cost"])
                    win = side == result.upper()
                    payout = contracts * 1.0 if win else 0.0
                    pnl = payout - contracts * avg
                    cash += payout
                    conn.execute(
                        "DELETE FROM positions WHERE ticker=? AND side=?",
                        (ticker, side),
                    )
                    note = f"SETTLE result={result} win={win} pnl={pnl:.4f}"
                    add_trade(
                        conn,
                        "SETTLE",
                        ticker,
                        side,
                        contracts,
                        1.0 if win else 0.0,
                        0.0,
                        payout,
                        note,
                    )
                    settled_log.append(
                        {
                            "ticker": ticker,
                            "side": side,
                            "contracts": contracts,
                            "result": result,
                            "payout": payout,
                            "pnl": pnl,
                        }
                    )
                set_cash(conn, cash)
    return settled_log


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class BuyRequest(BaseModel):
    side: str = Field(..., pattern="^(YES|NO|yes|no)$")
    contracts: int = Field(..., ge=1, le=500)


class SellRequest(BaseModel):
    side: str = Field(..., pattern="^(YES|NO|yes|no)$")
    contracts: int = Field(..., ge=1, le=500)
    ticker: Optional[str] = None


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    try_settle_positions()
    with _db() as conn:
        cash = get_cash(conn)
    return {"ok": True, "paper": True, "cash": round(cash, 4), "warning": "FAKE MONEY ONLY"}


@app.get("/api/market")
def api_market():
    try_settle_positions()
    try:
        return get_live_market()
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Kalshi fetch failed: {e}") from e


@app.get("/api/paper/state")
def paper_state():
    settled = try_settle_positions()
    market = None
    try:
        market = get_live_market()
    except Exception:
        pass

    with _db() as conn:
        cash = get_cash(conn)
        positions = list_positions(conn)
        trades = list_trades(conn)

    mtm = 0.0
    enriched = []
    for p in positions:
        side = p["side"].upper()
        mark = None
        if market and market.get("ticker") == p["ticker"]:
            if side == "YES":
                mark = market.get("yes_bid")
            else:
                mark = market.get("no_bid")
        contracts = int(p["contracts"])
        avg = float(p["avg_cost"])
        mark_val = (mark if mark is not None else avg) * contracts
        cost_basis = avg * contracts
        unreal = mark_val - cost_basis
        mtm += mark_val
        enriched.append(
            {
                **p,
                "side": side,
                "mark": mark,
                "mark_value": round(mark_val, 4),
                "cost_basis": round(cost_basis, 4),
                "unrealized": round(unreal, 4),
            }
        )

    realized = 0.0
    for t in trades:
        if t["action"] == "SETTLE":
            # payout - rough cost not stored separately; use note pnl if present
            pass
        if t["action"] == "SELL":
            realized += float(t["total"])  # proceeds; incomplete without cost — skip precise

    equity = cash + mtm
    return {
        "cash": round(cash, 4),
        "positions_mtm": round(mtm, 4),
        "equity": round(equity, 4),
        "start_cash": START_CASH,
        "pnl_vs_start": round(equity - START_CASH, 4),
        "positions": enriched,
        "trades": trades,
        "just_settled": settled,
        "paper": True,
    }


@app.post("/api/paper/buy")
def paper_buy(body: BuyRequest):
    try_settle_positions()
    side = body.side.upper()
    n = body.contracts
    try:
        market = get_live_market()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Kalshi fetch failed: {e}") from e

    ask = market.get("yes_ask") if side == "YES" else market.get("no_ask")
    if ask is None:
        raise HTTPException(400, f"No live {side} ask available")
    if ask <= 0 or ask >= 1:
        raise HTTPException(400, f"Invalid ask price {ask}")

    fee = fee_dollars(n, ask)
    cost = n * ask + fee
    ticker = market["ticker"]

    with _lock:
        with _db() as conn:
            cash = get_cash(conn)
            if cost > cash + 1e-9:
                raise HTTPException(
                    400,
                    f"Insufficient cash: need ${cost:.4f}, have ${cash:.4f}",
                )
            cash -= cost
            set_cash(conn, cash)
            upsert_position(conn, ticker, side, n, ask)
            add_trade(conn, "BUY", ticker, side, n, ask, fee, cost, "paper fill @ ask")
            return {
                "ok": True,
                "ticker": ticker,
                "side": side,
                "contracts": n,
                "price": ask,
                "fee": round(fee, 6),
                "total": round(cost, 6),
                "cash": round(cash, 4),
            }


@app.post("/api/paper/sell")
def paper_sell(body: SellRequest):
    try_settle_positions()
    side = body.side.upper()
    n = body.contracts
    try:
        market = get_live_market()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Kalshi fetch failed: {e}") from e

    ticker = body.ticker or market["ticker"]
    bid = None
    if ticker == market["ticker"]:
        bid = market.get("yes_bid") if side == "YES" else market.get("no_bid")
    if bid is None:
        # try refresh that ticker
        m = fetch_market_by_ticker(ticker)
        if m:
            nm = normalize_market(m)
            bid = nm.get("yes_bid") if side == "YES" else nm.get("no_bid")
    if bid is None:
        raise HTTPException(400, f"No live {side} bid available")

    fee = fee_dollars(n, bid)
    proceeds = n * bid - fee
    if proceeds < 0:
        proceeds = 0.0

    with _lock:
        with _db() as conn:
            reduce_position(conn, ticker, side, n)
            cash = get_cash(conn) + proceeds
            set_cash(conn, cash)
            add_trade(
                conn,
                "SELL",
                ticker,
                side,
                n,
                bid,
                fee,
                proceeds,
                "paper close @ bid",
            )
            return {
                "ok": True,
                "ticker": ticker,
                "side": side,
                "contracts": n,
                "price": bid,
                "fee": round(fee, 6),
                "total": round(proceeds, 6),
                "cash": round(cash, 4),
            }


@app.post("/api/paper/reset")
def paper_reset():
    with _lock:
        with _db() as conn:
            conn.execute("DELETE FROM positions")
            conn.execute("DELETE FROM trades")
            set_cash(conn, START_CASH)
    return {"ok": True, "cash": START_CASH, "message": "Bankroll reset to $100 (paper)"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Kalshi BTC 15m — Paper</title>
<style>
  :root {
    --bg: #0b0f14;
    --panel: #121821;
    --panel2: #1a2230;
    --border: #243044;
    --text: #e8eef7;
    --muted: #8b9bb4;
    --green: #3dd68c;
    --red: #ff6b7a;
    --amber: #f5c542;
    --blue: #5b9dff;
    --yes: #3dd68c;
    --no: #ff6b7a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: radial-gradient(1200px 600px at 20% -10%, #152033 0%, var(--bg) 55%);
    color: var(--text); min-height: 100vh;
  }
  .banner {
    background: linear-gradient(90deg, #5a1a1a, #7a3a10 40%, #5a1a1a);
    color: #ffe8c8; text-align: center; padding: 10px 16px; font-weight: 700;
    letter-spacing: 0.04em; border-bottom: 1px solid #a85;
  }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 20px 16px 48px; }
  h1 { font-size: 1.35rem; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 0.9rem; margin-bottom: 18px; }
  .grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 14px; }
  @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 16px 18px; box-shadow: 0 8px 24px rgba(0,0,0,.25);
  }
  .card h2 { margin: 0 0 12px; font-size: 0.95rem; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
  .row { display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: baseline; }
  .stat { min-width: 110px; }
  .stat .lbl { display:block; color: var(--muted); font-size: 0.72rem; text-transform: uppercase; }
  .stat .val { font-size: 1.25rem; font-weight: 700; font-variant-numeric: tabular-nums; }
  .ticker { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--blue); }
  .countdown { font-size: 1.8rem; font-weight: 800; color: var(--amber); font-variant-numeric: tabular-nums; }
  .prices { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
  .px {
    background: var(--panel2); border-radius: 10px; padding: 12px; border: 1px solid var(--border);
  }
  .px.yes { border-color: #255f45; }
  .px.no { border-color: #6b2a35; }
  .px .side { font-weight: 700; margin-bottom: 4px; }
  .px.yes .side { color: var(--yes); }
  .px.no .side { color: var(--no); }
  .px .ask { font-size: 1.6rem; font-weight: 800; font-variant-numeric: tabular-nums; }
  .hint { margin-top: 10px; color: var(--muted); font-size: 0.9rem; }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 14px; }
  input[type=number] {
    width: 88px; background: var(--panel2); border: 1px solid var(--border); color: var(--text);
    border-radius: 8px; padding: 10px 12px; font-size: 1rem;
  }
  button {
    border: 0; border-radius: 8px; padding: 10px 14px; font-weight: 700; cursor: pointer;
    font-size: 0.95rem;
  }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .btn-yes { background: #1f8f5f; color: #fff; }
  .btn-no { background: #c43c4e; color: #fff; }
  .btn-sell { background: #3a4a66; color: #fff; }
  .btn-reset { background: transparent; color: var(--muted); border: 1px solid var(--border); }
  .btn-reset:hover { color: var(--text); border-color: var(--muted); }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
  th { color: var(--muted); font-weight: 600; font-size: 0.72rem; text-transform: uppercase; }
  .fee { font-size: 0.85rem; color: var(--muted); margin-top: 8px; }
  .err { color: var(--red); margin-top: 8px; font-size: 0.9rem; }
  .okmsg { color: var(--green); margin-top: 8px; font-size: 0.9rem; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  footer { margin-top: 18px; color: var(--muted); font-size: 0.8rem; text-align: center; }
</style>
</head>
<body>
  <div class="banner">PAPER / DINERO FALSO — no órdenes reales · no depósitos · sin API keys</div>
  <div class="wrap">
    <h1>Kalshi BTC 15m · Paper Desk</h1>
    <div class="sub">Serie <span class="mono">KXBTC15M</span> · precios públicos en vivo · bankroll ficticio $100</div>

    <div class="grid">
      <div class="card">
        <h2>Mercado</h2>
        <div class="row">
          <div class="stat"><span class="lbl">Ticker</span><span class="val ticker" id="ticker">—</span></div>
          <div class="stat"><span class="lbl">Strike</span><span class="val" id="strike">—</span></div>
          <div class="stat"><span class="lbl">Countdown</span><span class="countdown" id="countdown">—</span></div>
          <div class="stat"><span class="lbl">BTC spot</span><span class="val" id="spot">—</span></div>
        </div>
        <div class="prices">
          <div class="px yes">
            <div class="side">YES ask</div>
            <div class="ask" id="yesAsk">—¢</div>
            <div class="hint" id="yesBid">bid —</div>
          </div>
          <div class="px no">
            <div class="side">NO ask</div>
            <div class="ask" id="noAsk">—¢</div>
            <div class="hint" id="noBid">bid —</div>
          </div>
        </div>
        <div class="hint" id="spotHint">—</div>
        <div class="fee" id="feeHint">Fee (Omega): 7 × p × (1−p) ¢ / contrato</div>

        <div class="controls">
          <label>Size <input type="number" id="size" value="5" min="1" max="500"/></label>
          <button class="btn-yes" id="buyYes" onclick="buy('YES')">Comprar YES</button>
          <button class="btn-no" id="buyNo" onclick="buy('NO')">Comprar NO</button>
          <button class="btn-sell" onclick="sell('YES')">Vender YES</button>
          <button class="btn-sell" onclick="sell('NO')">Vender NO</button>
          <button class="btn-reset" onclick="resetBank()">Reset bankroll</button>
        </div>
        <div class="err" id="err"></div>
        <div class="okmsg" id="ok"></div>
      </div>

      <div class="card">
        <h2>Paper account</h2>
        <div class="row">
          <div class="stat"><span class="lbl">Cash</span><span class="val" id="cash">—</span></div>
          <div class="stat"><span class="lbl">MTM positions</span><span class="val" id="mtm">—</span></div>
          <div class="stat"><span class="lbl">Equity</span><span class="val" id="equity">—</span></div>
          <div class="stat"><span class="lbl">PnL vs $100</span><span class="val" id="pnl">—</span></div>
        </div>
        <h2 style="margin-top:16px">Posiciones abiertas</h2>
        <div style="overflow-x:auto">
          <table>
            <thead><tr><th>Ticker</th><th>Side</th><th>Qty</th><th>Avg</th><th>Mark</th><th>uPnL</th></tr></thead>
            <tbody id="posBody"><tr><td colspan="6" style="color:var(--muted)">Ninguna</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <h2>Trade log</h2>
      <div style="overflow-x:auto; max-height:280px; overflow-y:auto">
        <table>
          <thead><tr><th>Time</th><th>Action</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Px</th><th>Fee</th><th>Total</th><th>Note</th></tr></thead>
          <tbody id="tradeBody"><tr><td colspan="9" style="color:var(--muted)">Vacío</td></tr></tbody>
        </table>
      </div>
    </div>

    <footer>Demo educativa · PAPER ONLY · Kalshi public API + OKX spot · no afiliado</footer>
  </div>

<script>
let lastMarket = null;
let localCountdown = null;
let countdownTimer = null;

function fmtUsd(x) {
  if (x == null || Number.isNaN(x)) return '—';
  return '$' + Number(x).toFixed(2);
}
function fmtCents(x) {
  if (x == null) return '—¢';
  return (Number(x)).toFixed(1) + '¢';
}
function fmtSec(s) {
  if (s == null) return '—';
  s = Math.max(0, Math.floor(s));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return String(m).padStart(2,'0') + ':' + String(r).padStart(2,'0');
}
function setMsg(err, ok) {
  document.getElementById('err').textContent = err || '';
  document.getElementById('ok').textContent = ok || '';
}

async function refreshMarket() {
  try {
    const r = await fetch('/api/market');
    if (!r.ok) throw new Error(await r.text());
    const m = await r.json();
    lastMarket = m;
    localCountdown = m.countdown_sec;
    document.getElementById('ticker').textContent = m.ticker || '—';
    document.getElementById('strike').textContent = m.floor_strike != null ? ('$' + Number(m.floor_strike).toLocaleString()) : '—';
    document.getElementById('countdown').textContent = fmtSec(localCountdown);
    document.getElementById('spot').textContent = m.btc_spot != null ? ('$' + Number(m.btc_spot).toLocaleString(undefined,{maximumFractionDigits:2})) : '—';
    document.getElementById('yesAsk').textContent = fmtCents(m.yes_ask_cents);
    document.getElementById('noAsk').textContent = fmtCents(m.no_ask_cents);
    document.getElementById('yesBid').textContent = 'bid ' + fmtCents(m.yes_bid_cents);
    document.getElementById('noBid').textContent = 'bid ' + fmtCents(m.no_bid_cents);
    document.getElementById('spotHint').textContent = m.spot_hint || '';
    const n = Number(document.getElementById('size').value) || 5;
    const parts = [];
    if (m.yes_ask != null) {
      const f = 7 * m.yes_ask * (1 - m.yes_ask);
      parts.push(`YES×${n}: fee $${(n*f/100).toFixed(4)} · total $${(n*m.yes_ask + n*f/100).toFixed(4)}`);
    }
    if (m.no_ask != null) {
      const f = 7 * m.no_ask * (1 - m.no_ask);
      parts.push(`NO×${n}: fee $${(n*f/100).toFixed(4)} · total $${(n*m.no_ask + n*f/100).toFixed(4)}`);
    }
    document.getElementById('feeHint').textContent = parts.join('  ·  ') || 'Fee Omega: 7×p×(1−p) ¢';
  } catch (e) {
    setMsg('Mercado: ' + e.message, '');
  }
}

async function refreshState() {
  try {
    const r = await fetch('/api/paper/state');
    if (!r.ok) throw new Error(await r.text());
    const s = await r.json();
    document.getElementById('cash').textContent = fmtUsd(s.cash);
    document.getElementById('mtm').textContent = fmtUsd(s.positions_mtm);
    document.getElementById('equity').textContent = fmtUsd(s.equity);
    const pnlEl = document.getElementById('pnl');
    pnlEl.textContent = (s.pnl_vs_start >= 0 ? '+' : '') + fmtUsd(s.pnl_vs_start);
    pnlEl.style.color = s.pnl_vs_start >= 0 ? 'var(--green)' : 'var(--red)';

    const pb = document.getElementById('posBody');
    if (!s.positions.length) {
      pb.innerHTML = '<tr><td colspan="6" style="color:var(--muted)">Ninguna</td></tr>';
    } else {
      pb.innerHTML = s.positions.map(p => `<tr>
        <td class="mono">${p.ticker}</td><td>${p.side}</td><td>${p.contracts}</td>
        <td>${(p.avg_cost*100).toFixed(1)}¢</td>
        <td>${p.mark!=null?(p.mark*100).toFixed(1)+'¢':'—'}</td>
        <td style="color:${p.unrealized>=0?'var(--green)':'var(--red)'}">${p.unrealized>=0?'+':''}${p.unrealized.toFixed(3)}</td>
      </tr>`).join('');
    }
    const tb = document.getElementById('tradeBody');
    if (!s.trades.length) {
      tb.innerHTML = '<tr><td colspan="9" style="color:var(--muted)">Vacío</td></tr>';
    } else {
      tb.innerHTML = s.trades.map(t => {
        const ts = (t.ts || '').replace('T',' ').slice(0,19);
        return `<tr>
          <td class="mono">${ts}</td><td>${t.action}</td><td class="mono">${t.ticker}</td>
          <td>${t.side}</td><td>${t.contracts}</td>
          <td>${(t.price*100).toFixed(1)}¢</td>
          <td>$${Number(t.fee).toFixed(4)}</td>
          <td>$${Number(t.total).toFixed(4)}</td>
          <td>${t.note||''}</td>
        </tr>`;
      }).join('');
    }
    if (s.just_settled && s.just_settled.length) {
      setMsg('', 'Liquidación: ' + JSON.stringify(s.just_settled));
    }
  } catch (e) {
    setMsg('Estado: ' + e.message, '');
  }
}

async function buy(side) {
  setMsg('', '');
  const contracts = Number(document.getElementById('size').value) || 5;
  try {
    const r = await fetch('/api/paper/buy', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({side, contracts})
    });
    const j = await r.json().catch(()=>({}));
    if (!r.ok) throw new Error(j.detail || JSON.stringify(j));
    setMsg('', `Comprado ${contracts} ${side} @ ${(j.price*100).toFixed(1)}¢ · fee $${j.fee} · total $${j.total}`);
    await refreshState();
  } catch (e) { setMsg(String(e.message || e), ''); }
}

async function sell(side) {
  setMsg('', '');
  const contracts = Number(document.getElementById('size').value) || 5;
  try {
    const r = await fetch('/api/paper/sell', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({side, contracts})
    });
    const j = await r.json().catch(()=>({}));
    if (!r.ok) throw new Error(j.detail || JSON.stringify(j));
    setMsg('', `Vendido ${contracts} ${side} @ ${(j.price*100).toFixed(1)}¢ · neto $${j.total}`);
    await refreshState();
  } catch (e) { setMsg(String(e.message || e), ''); }
}

async function resetBank() {
  if (!confirm('¿Resetear bankroll a $100 y borrar posiciones/trades?')) return;
  const r = await fetch('/api/paper/reset', {method:'POST'});
  const j = await r.json();
  setMsg('', j.message || 'Reset OK');
  await refreshState();
}

function tickCountdown() {
  if (localCountdown == null) return;
  localCountdown = Math.max(0, localCountdown - 1);
  document.getElementById('countdown').textContent = fmtSec(localCountdown);
}

refreshMarket();
refreshState();
setInterval(refreshMarket, 2000);
setInterval(refreshState, 2000);
setInterval(tickCountdown, 1000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML
