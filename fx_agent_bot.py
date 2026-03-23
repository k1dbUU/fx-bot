"""
FX Agent Bot v5.4 — Online
Strategy: SMC Daily Sweep (2R) + ICT Silver Bullet (3R)
Instruments: GOLD#, EURUSD, GBPUSD
Runs on: GitHub Actions (cron) via MetaAPI cloud

v5.4 fixes vs v5.3:
- BUG 5 FIXED: get_historical_candles not available on RPC connection
  Use api.history_storage to fetch candles — works with RPC connection
  RPC handles: prices, specs, positions, account info, trade execution
  History API handles: OHLCV candle data
"""

import os
import json
import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

from metaapi_cloud_sdk import MetaApi
try:
    from metaapi_cloud_sdk.clients.metaApi.tradeException import TradeException
except ImportError:
    try:
        from metaapi_cloud_sdk import TradeException
    except ImportError:
        TradeException = Exception

META_API_TOKEN  = os.environ["META_API_TOKEN"]
META_ACCOUNT_ID = os.environ["META_ACCOUNT_ID"]
STATE_FILE      = "state.json"
STATUS_FILE     = "status.json"

RISK_PERCENT     = 5.0
MAX_DAILY_LOSSES = 2
MAX_OPEN_TRADES  = 3
OB_BUFFER_GOLD   = 1.0
OB_BUFFER_FX     = 0.0003

GOLD_SL=200; GOLD_TP=400; GOLD_SB_SL=150; GOLD_SB_TP=450
FX_SL=200;   FX_TP=400;   FX_SB_SL=150;   FX_SB_TP=450
MAX_SPREAD_SMC=200; MAX_SPREAD_SB=150

SYMBOLS = ["GOLD#", "EURUSD", "GBPUSD"]
AGENT_ID = 202605
SYNC_TIMEOUT_SECONDS = 300

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("FX-AGENT")

def write_heartbeat(status, balance=0, open_trades=0, daily_losses=None,
                    server_time=None, last_prices=None, error=None):
    data = {
        "agent_version": "5.4",
        "status": status,
        "last_seen_utc": datetime.now(timezone.utc).isoformat(),
        "server_time_utc": server_time or datetime.now(timezone.utc).isoformat(),
        "balance": round(float(balance), 2) if balance else 0,
        "open_trades": open_trades,
        "daily_losses": daily_losses or {s: 0 for s in SYMBOLS},
        "last_prices": last_prices or {},
        "error": error or None,
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "run_number": os.environ.get("GITHUB_RUN_NUMBER", "0"),
    }
    Path(STATUS_FILE).write_text(json.dumps(data, indent=2))
    log.info(f"[HEARTBEAT] Written — status={status} balance={balance:.2f}")

def load_state():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default = {"date": today,
        "daily_losses": {s: 0 for s in SYMBOLS},
        "swept_high":   {s: False for s in SYMBOLS},
        "swept_low":    {s: False for s in SYMBOLS},
        "sb_london":    {s: False for s in SYMBOLS},
        "sb_ny_am":     {s: False for s in SYMBOLS},
        "sb_ny_pm":     {s: False for s in SYMBOLS},
    }
    if Path(STATE_FILE).exists():
        try:
            saved = json.loads(Path(STATE_FILE).read_text())
            if saved.get("date") == today:
                log.info(f"[STATE] Loaded — {today}")
                return saved
        except Exception as e:
            log.warning(f"[STATE] Load error: {e} — reset")
    log.info("[STATE] New day — reset")
    return default

def save_state(state):
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))
    log.info("[STATE] Saved")

def get_ny_hour():
    utc = datetime.now(timezone.utc)
    offset = -4 if 3 <= utc.month <= 11 else -5
    return (utc + timedelta(hours=offset)).hour

def is_sweep_session():
    utc = datetime.now(timezone.utc)
    h, m = utc.hour, utc.minute
    return (h == 13 and m >= 30) or (14 <= h <= 19)

def is_friday():
    return datetime.now(timezone.utc).weekday() == 4

def get_point(spec, sym):
    """FIX BUG 1: MetaAPI returns 'points' (plural), not 'point'."""
    pt = spec.get("points") or spec.get("point") or spec.get("tickSize")
    if not pt:
        digits = spec.get("digits", 5)
        pt = 10 ** (-digits)
    log.info(f"  [SPEC] {sym} point={pt}")
    return float(pt)

def get_tick_value(spec, sym, ask):
    """FIX BUGS 2+3: MetaAPI uses capital 'TickValue'; may be 0 for forex."""
    tv = spec.get("TickValue") or spec.get("tickValue") or 0
    if tv and float(tv) > 0:
        log.info(f"  [SPEC] {sym} tickValue={tv} (from spec)")
        return float(tv)
    # Derive from contract size when TickValue=0
    contract_size = spec.get("contractSize", 100000)
    tick_size = spec.get("tickSize", 0.00001)
    tv = (tick_size / ask) * contract_size if ask and ask > 0 else tick_size * contract_size
    log.info(f"  [SPEC] {sym} tickValue={tv:.6f} (calculated, contractSize={contract_size})")
    return float(tv)

def get_ob_buffer(sym):
    """FIX BUG 4: Symbol-aware OB buffer."""
    return OB_BUFFER_GOLD if sym == "GOLD#" else OB_BUFFER_FX

def get_bias(candles):
    if len(candles) < 13: return "NEUTRAL"
    recent = candles[-13:]
    hi = max(c["high"] for c in recent)
    lo = min(c["low"]  for c in recent)
    mid = (hi + lo) / 2.0
    rc = recent[-2]["close"]
    if rc > mid * 1.001: return "BULLISH"
    if rc < mid * 0.999: return "BEARISH"
    return "NEUTRAL"

def rr_passes(sl, tp, spd):
    esl = sl + spd; etp = tp - spd
    if etp <= 0: log.info("  [RR] TP wiped"); return False
    rr = etp / esl
    log.info(f"  [RR] {rr:.2f}")
    return rr >= 1.0

def calc_lot(balance, sl_pts, spd, tv, ts, vmin, vmax, vstp):
    risk = balance * (RISK_PERCENT / 100.0)
    ppv  = tv / ts if ts > 0 else 0
    esl  = sl_pts + spd
    if ppv <= 0 or esl <= 0:
        log.warning(f"  [LOT] ppv={ppv} — using vmin")
        return vmin
    raw = risk / (esl * ppv)
    lot = math.floor(raw / vstp) * vstp
    lot = round(max(min(lot, vmax), vmin), 2)
    actual = lot * esl * ppv
    while actual > risk and lot > vmin:
        lot = round(lot - vstp, 2)
        actual = lot * esl * ppv
    log.info(f"  [LOT] {lot} lots | risk={risk:.2f} actual={actual:.2f}")
    return lot

def find_liq_sweep(candles, bias):
    if len(candles) < 12: return False
    recent = candles[-12:]
    high_levels = [c["high"] for c in recent[:-1]]
    low_levels  = [c["low"]  for c in recent[:-1]]
    curr = recent[-1]
    if bias == "BULLISH" and low_levels:
        min_low = min(low_levels)
        if curr["low"] < min_low:
            log.info(f"  [SWEEP] Low swept: {curr['low']:.5f} < {min_low:.5f}")
            return True
    elif bias == "BEARISH" and high_levels:
        max_high = max(high_levels)
        if curr["high"] > max_high:
            log.info(f"  [SWEEP] High swept: {curr['high']:.5f} > {max_high:.5f}")
            return True
    return False

def find_order_block(candles, bias):
    if len(candles) < 4: return None
    recent = candles[-4:]
    if bias == "BULLISH":
        for i, c in enumerate(recent):
            if c["close"] < c["open"]:
                return {"high": c["high"], "low": c["low"]}
    elif bias == "BEARISH":
        for i, c in enumerate(recent):
            if c["close"] > c["open"]:
                return {"high": c["high"], "low": c["low"]}
    return None

def price_in_ob(price, ob, bias, buffer):
    if not ob: return False
    if bias == "BULLISH":
        return ob["low"] - buffer <= price <= ob["high"] + buffer
    else:
        return ob["low"] - buffer <= price <= ob["high"] + buffer

def find_silver_bullet(candles, hour):
    if len(candles) < 3: return None
    session_candles = []
    for c in candles[-10:]:
        ch = datetime.fromisoformat(c["time"].replace("Z", "+00:00")).hour
        if hour == 2 and ch in [2, 3]:
            session_candles.append(c)
        elif hour == 8 and ch in [8, 9]:
            session_candles.append(c)
        elif hour == 13 and ch in [13, 14]:
            session_candles.append(c)
    if len(session_candles) < 2: return None
    hi = max(c["high"] for c in session_candles)
    lo = min(c["low"] for c in session_candles)
    return {"high": hi, "low": lo}

def check_ict_confluences(candles, bias):
    confluences = []
    if len(candles) < 5: return confluences
    recent = candles[-5:]
    smas = [sum(c["close"] for c in recent[i:i+3])/3 for i in range(len(recent)-2)]
    if len(smas) >= 2:
        if bias == "BULLISH" and smas[-1] > smas[-2]:
            confluences.append("SMA_ALIGNED")
        elif bias == "BEARISH" and smas[-1] < smas[-2]:
            confluences.append("SMA_ALIGNED")
    curr = recent[-1]
    if len(recent) >= 3:
        if bias == "BULLISH" and curr["close"] > recent[-2]["high"]:
            confluences.append("BREAK_OF_STRUCTURE")
        elif bias == "BEARISH" and curr["close"] < recent[-2]["low"]:
            confluences.append("BREAK_OF_STRUCTURE")
    return confluences

async def execute_trade(connection, sym, side, lot, sl, tp, comment):
    try:
        result = await connection.create_market_buy_order(sym, lot, sl, tp, {"comment": comment}) if side == "BUY" else await connection.create_market_sell_order(sym, lot, sl, tp, {"comment": comment})
        log.info(f"  [TRADE] {side} {lot} {sym} — orderId={result.get('orderId', 'N/A')}")
        return True
    except TradeException as e:
        log.error(f"  [TRADE] Failed — {e}")
        return False
    except Exception as e:
        log.error(f"  [TRADE] Error — {e}")
        return False

async def main():
    api = MetaApi(META_API_TOKEN)
    account = None
    connection = None
    history_storage = None
    
    try:
        log.info("=== FX AGENT v5.4 START ===")
        write_heartbeat("STARTING")
        
        account = await api.metatrader_account_api.get_account(META_ACCOUNT_ID)
        if account.state != "DEPLOYED":
            log.error(f"Account not deployed: {account.state}")
            write_heartbeat("ERROR", error=f"Account state: {account.state}")
            return
        
        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized(timeout_in_seconds=SYNC_TIMEOUT_SECONDS)
        
        history_storage = connection.history_storage
        
        account_info = await connection.get_account_information()
        balance = account_info.get("balance", 0)
        positions = await connection.get_positions()
        open_trades = len(positions)
        
        log.info(f"[ACCOUNT] Balance={balance:.2f} Open={open_trades}")
        
        state = load_state()
        write_heartbeat("RUNNING", balance, open_trades, state["daily_losses"])
        
        if open_trades >= MAX_OPEN_TRADES:
            log.info("[LIMIT] Max open trades reached")
            write_heartbeat("MAX_TRADES", balance, open_trades)
            return
        
        for sym in SYMBOLS:
            if state["daily_losses"][sym] >= MAX_DAILY_LOSSES:
                log.info(f"[LIMIT] {sym} max daily losses")
                continue
            
            log.info(f"\n[{sym}] === ANALYZING ===")
            
            try:
                spec = await connection.get_symbol_specification(sym)
                price = await connection.get_symbol_price(sym)
                ask, bid = price.get("ask", 0), price.get("bid", 0)
                spread = (ask - bid) / get_point(spec, sym) if ask > bid else 999
                
                log.info(f"  [PRICE] ask={ask:.5f} bid={bid:.5f} spread={spread:.1f}")
                
                if spread > MAX_SPREAD_SMC:
                    log.info(f"  [SKIP] Spread too high: {spread:.1f}")
                    continue
                
                candles = await history_storage.get