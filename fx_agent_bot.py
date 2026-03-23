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

try:
    import requests
except ImportError:
    requests = None

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
    if len(candles) < 12:
        return False, None
    
    recent = candles[-12:]
    current = candles[-1]
    
    if bias == "BULLISH":
        prev_high = max(c["high"] for c in recent[:-1])
        if current["high"] > prev_high:
            return True, current["high"]
    elif bias == "BEARISH":
        prev_low = min(c["low"] for c in recent[:-1])
        if current["low"] < prev_low:
            return True, current["low"]
    
    return False, None

async def fetch_candles(api, sym):
    try:
        start_time = datetime.now(timezone.utc) - timedelta(days=2)
        candles = await api.history_storage.get_candles(sym, "1h", start_time)
        return candles[-24:] if len(candles) > 24 else candles
    except Exception as e:
        log.error(f"  [CANDLES] {sym} error: {e}")
        return []

async def process_symbol(api, conn, sym, state, specs, prices):
    log.info(f"[PROCESS] {sym} —————————")
    
    try:
        spec = specs.get(sym, {})
        price = prices.get(sym, {})
        
        if not price or not price.get("ask") or not price.get("bid"):
            log.warning(f"  [SKIP] No price data")
            return
        
        ask, bid = price["ask"], price["bid"]
        spread_raw = ask - bid
        point = get_point(spec, sym)
        spread = spread_raw / point
        
        log.info(f"  [PRICE] ask={ask:.5f} bid={bid:.5f} spread={spread:.1f}pts")
        
        candles = await fetch_candles(api, sym)
        if len(candles) < 13:
            log.warning(f"  [SKIP] Need 13+ candles, got {len(candles)}")
            return
        
        bias = get_bias(candles)
        log.info(f"  [BIAS] {bias}")
        
        if bias == "NEUTRAL":
            log.info("  [SKIP] Neutral bias")
            return
        
        # Check for liquidity sweep
        swept, sweep_price = find_liq_sweep(candles, bias)
        
        if not swept:
            log.info("  [SKIP] No liquidity sweep")
            return
        
        log.info(f"  [SWEEP] Detected at {sweep_price}")
        
        # Check spread for SMC strategy
        if spread > MAX_SPREAD_SMC:
            log.warning(f"  [SKIP] Spread too wide: {spread:.1f}pts")
            return
        
        # Order block logic (simplified)
        buffer = get_ob_buffer(sym)
        
        if bias == "BULLISH":
            entry = sweep_price + buffer
            sl_pts = GOLD_SL if sym == "GOLD#" else FX_SL
            tp_pts = GOLD_TP if sym == "GOLD#" else FX_TP
        else:
            entry = sweep_price - buffer
            sl_pts = GOLD_SL if sym == "GOLD#" else FX_SL
            tp_pts = GOLD_TP if sym == "GOLD#" else FX_TP
        
        if not rr_passes(sl_pts, tp_pts, spread):
            log.info("  [SKIP] R:R < 1.0")
            return
        
        # Position sizing
        balance = (await conn.get_account_information())["balance"]
        tick_value = get_tick_value(spec, sym, ask)
        tick_size = spec.get("tickSize", point)
        vol_min = spec.get("minVolume", 0.01)
        vol_max = spec.get("maxVolume", 100.0)
        vol_step = spec.get("volumeStep", 0.01)
        
        lot = calc_lot(balance, sl_pts, spread, tick_value, tick_size,
                      vol_min, vol_max, vol_step)
        
        # Execute trade
        if bias == "BULLISH":
            sl_price = entry - (sl_pts * point)
            tp_price = entry + (tp_pts * point)
            action = "ORDER_TYPE_BUY_LIMIT"
        else:
            sl_price = entry + (sl_pts * point)
            tp_price = entry - (tp_pts * point)
            action = "ORDER_TYPE_SELL_LIMIT"
        
        order = {
            "actionType": action,
            "symbol": sym,
            "volume": lot,
            "openPrice": entry,
            "stopLoss": sl_price,
            "takeProfit": tp_price,
            "comment": f"SMC-Sweep-{AGENT_ID}"
        }
        
        log.info(f"  [ORDER] {action} {lot} lots @ {entry:.5f}")
        
        result = await conn.create_limit_order(**order)
        log.info(f"  [RESULT] orderId={result.get('orderId', 'N/A')}")
        
    except Exception as e:
        log.error(f"  [ERROR] {sym}: {e}")

async def main():
    try:
        log.info(f"[START] FX Agent Bot v5.4 — {AGENT_ID}")
        write_heartbeat("starting")
        
        state = load_state()
        
        api = MetaApi(META_API_TOKEN)
        account = await api.metatrader_account_api.get_account(META_ACCOUNT_ID)
        
        if account.state != "DEPLOYED":
            log.warning(f"[ACCOUNT] State: {account.state} — deploying...")
            await account.deploy()
            await account.wait_deployed()
        
        log.info("[CONNECTION] Establishing...")
        conn = account.get_rpc_connection()
        await conn.connect()
        await conn.wait_synchronized(timeout_in_seconds=SYNC_TIMEOUT_SECONDS)
        
        # Get account info
        acc_info = await conn.get_account_information()
        balance = acc_info["balance"]
        server_time = acc_info.get("time", datetime.now(timezone.utc).isoformat())
        
        log.info(f"[ACCOUNT] Balance: ${balance:.2f}")
        
        # Get specifications for all symbols
        log.info("[SPECS] Loading...")
        specs = {}
        for sym in SYMBOLS:
            try:
                spec = await conn.get_symbol_specification(sym)
                specs[sym] = spec
                log.info(f"  [SPEC] {sym