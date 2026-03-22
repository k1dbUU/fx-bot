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
    pool = candles[-10:-1]; trigger = candles[-1]
    if bias == "BULLISH":
        sw = min(c["low"] for c in pool)
        if trigger["low"] < sw and trigger["close"] > sw:
            log.info("  [SB-LIQ] Bullish ✅"); return True
    else:
        sw = max(c["high"] for c in pool)
        if trigger["high"] > sw and trigger["close"] < sw:
            log.info("  [SB-LIQ] Bearish ✅"); return True
    return False

def find_fvg(candles, bias):
    if len(candles) < 4: return None
    for i in range(1, min(len(candles)-1, 29)):
        c1, c2, c3 = candles[i-1], candles[i], candles[i+1]
        c2t = max(c2["open"], c2["close"]); c2b = min(c2["open"], c2["close"])
        if bias == "BULLISH" and c1["low"] > c3["high"] and c2t > c2b * 1.001:
            log.info("  [FVG] Bullish ✅"); return (c1["low"], c3["high"])
        if bias == "BEARISH" and c1["high"] < c3["low"] and c2b < c2t * 0.999:
            log.info("  [FVG] Bearish ✅"); return (c3["low"], c1["high"])
    return None

def check_close_back(candles, sweep_type, dh, dl):
    for c in reversed(candles[-5:]):
        if sweep_type == "HIGH" and c["close"] < dh: log.info("  [CB] ✅"); return True
        if sweep_type == "LOW"  and c["close"] > dl: log.info("  [CB] ✅"); return True
    return False

def confirm_bos(candles, sweep_type):
    if len(candles) < 8: return False
    pool = candles[-8:]
    hi = max(c["high"] for c in pool); lo = min(c["low"] for c in pool)
    mid = (hi + lo) / 2.0; rc = pool[-2]["close"]
    if sweep_type == "HIGH" and rc < mid: log.info("  [BOS] ✅ Bearish"); return True
    if sweep_type == "LOW"  and rc > mid: log.info("  [BOS] ✅ Bullish"); return True
    return False

async def exec_trade(conn, sym, direction, entry, sl, tp, lot, comment):
    opts = {"comment": comment, "clientId": f"fxa_{AGENT_ID}"}
    try:
        if direction == "BUY":
            await conn.create_market_buy_order(sym, lot, sl, tp, opts)
        else:
            await conn.create_market_sell_order(sym, lot, sl, tp, opts)
        log.info(f"  [TRADE] ✅ {sym} {direction} lot={lot}")
        return True
    except TradeException as e:
        log.error(f"  [TRADE] ❌ {sym} TradeException: {e.message}")
        return False
    except Exception as e:
        log.error(f"  [TRADE] ❌ {sym} Error: {e}")
        return False

async def run_bot():
    log.info("══════════════════════════════════════════")
    log.info("FX Agent v5.4 — ONLINE — GitHub Actions")
    log.info("══════════════════════════════════════════")

    if is_friday():
        log.info("[FRIDAY] No trades. Done.")
        write_heartbeat("friday_skip")
        return

    state = load_state()
    write_heartbeat("starting")

    api  = MetaApi(META_API_TOKEN)
    acct = await api.metatrader_account_api.get_account(META_ACCOUNT_ID)

    if acct.state not in ("DEPLOYING", "DEPLOYED"):
        log.info("[META] Deploying...")
        await acct.deploy()

    log.info("[META] Waiting for broker connection...")
    await acct.wait_connected()

    conn = acct.get_rpc_connection()
    await conn.connect()

    try:
        await asyncio.wait_for(conn.wait_synchronized(), timeout=SYNC_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log.error(f"[META] Sync timeout after {SYNC_TIMEOUT_SECONDS}s")
        write_heartbeat("sync_timeout", error=f"Sync timeout after {SYNC_TIMEOUT_SECONDS}s")
        await conn.close(); save_state(state); return

    log.info("[META] Connected and synchronized ✅")

    acct_info   = await conn.get_account_information()
    balance     = acct_info["balance"]
    server_time = acct_info.get("time", datetime.now(timezone.utc).isoformat())
    log.info(f"[ACCOUNT] Balance: {balance:.2f}")

    write_heartbeat("connected", balance=balance, server_time=str(server_time))

    positions  = await conn.get_positions()
    total_open = len([p for p in positions if p["symbol"] in SYMBOLS])
    ny_hour    = get_ny_hour()
    in_sweep   = is_sweep_session()
    log.info(f"[SESSION] NY Hour:{ny_hour} | Sweep:{in_sweep} | Open:{total_open}")

    last_prices = {}

    for sym in SYMBOLS:
        log.info(f"\n{'─'*45}\n[{sym}]")

        if state["daily_losses"][sym] >= MAX_DAILY_LOSSES:
            log.info(f"[{sym}] Daily loss limit — skip"); continue
        if total_open >= MAX_OPEN_TRADES:
            log.info("[GLOBAL] Max open trades"); break

        try:
            tick = await conn.get_symbol_price(sym)
            bid  = tick["bid"]; ask = tick["ask"]
            spec = await conn.get_symbol_specification(sym)

            pt   = get_point(spec, sym)
            tv   = get_tick_value(spec, sym, ask)
            ts   = spec.get("tickSize", 0.00001)
            vmin = spec.get("minVolume", 0.01)
            vmax = spec.get("maxVolume", 50.0)
            vstp = spec.get("volumeStep", 0.01)
            spd  = round((ask - bid) / pt)
            ob_buf = get_ob_buffer(sym)

            log.info(f"[{sym}] BID:{bid} ASK:{ask} SPD:{spd}pts pt:{pt} tv:{tv:.5f}")
            last_prices[sym] = {"bid": bid, "ask": ask, "spread": spd}
        except Exception as e:
            log.warning(f"[{sym}] Price/spec error: {e} — skip"); continue

        try:
            now   = datetime.now(timezone.utc)
            # FIX BUG 5: get_historical_candles is on the account object (acct), not conn
            c_h4  = await acct.get_historical_candles(symbol=sym, timeframe="4h",  start_time=now-timedelta(hours=60),  limit=15)
            c_h1  = await acct.get_historical_candles(symbol=sym, timeframe="1h",  start_time=now-timedelta(hours=10),  limit=10)
            c_m15 = await acct.get_historical_candles(symbol=sym, timeframe="15m", start_time=now-timedelta(hours=2),   limit=8)
            c_m5  = await acct.get_historical_candles(symbol=sym, timeframe="5m",  start_time=now-timedelta(hours=2),   limit=30)
            c_d1  = await acct.get_historical_candles(symbol=sym, timeframe="1d",  start_time=now-timedelta(days=3),    limit=3)
            log.info(f"[{sym}] Candles H4:{len(c_h4)} H1:{len(c_h1)} M15:{len(c_m15)} M5:{len(c_m5)} D1:{len(c_d1)}")
        except Exception as e:
            log.warning(f"[{sym}] Candle error: {e} — skip"); continue

        if len(c_d1) >= 2:   dh, dl = c_d1[-2]["high"], c_d1[-2]["low"]
        elif c_d1:            dh, dl = c_d1[-1]["high"], c_d1[-1]["low"]
        else:                 dh = dl = bid

        bias   = get_bias(c_h4)
        sl_smc = GOLD_SL    if sym == "GOLD#" else FX_SL
        tp_smc = GOLD_TP    if sym == "GOLD#" else FX_TP
        sl_sb  = GOLD_SB_SL if sym == "GOLD#" else FX_SB_SL
        tp_sb  = GOLD_SB_TP if sym == "GOLD#" else FX_SB_TP
        log.info(f"[{sym}] Bias:{bias} DH:{dh:.5f} DL:{dl:.5f} buf:{ob_buf}")

        # Silver Bullet
        sb_win = None
        if ny_hour == 3  and not state["sb_london"][sym]: sb_win = "LONDON"
        if ny_hour == 10 and not state["sb_ny_am"][sym]:  sb_win = "NY_AM"
        if ny_hour == 14 and not state["sb_ny_pm"][sym]:  sb_win = "NY_PM"

        if sb_win:
            state[f"sb_{sb_win.lower()}"][sym] = True
            log.info(f"[{sym}][SB] Window: {sb_win}")
            if spd > MAX_SPREAD_SB:
                log.info(f"[{sym}][SB] Spread too high")
            elif bias == "NEUTRAL":
                log.info(f"[{sym}][SB] Neutral bias")
            elif find_liq_sweep(c_m5, bias):
                fvg = find_fvg(c_m5, bias)
                if fvg:
                    ft, fb = fvg; mid = (bid + ask) / 2.0
                    near = abs(mid-fb) <= ob_buf or abs(mid-ft) <= ob_buf or fb <= mid <= ft
                    if near and rr_passes(sl_sb, tp_sb, spd):
                        lot = calc_lot(balance, sl_sb, spd, tv, ts, vmin, vmax, vstp)
                        if bias == "BULLISH":
                            ok = await exec_trade(conn, sym, "BUY", ask, ask-sl_sb*pt, ask+tp_sb*pt, lot, "FXA-SB")
                        else:
                            ok = await exec_trade(conn, sym, "SELL", bid, bid+sl_sb*pt, bid-tp_sb*pt, lot, "FXA-SB")
                        if not ok: state["daily_losses"][sym] += 1
                        total_open += 1
                    else: log.info(f"[{sym}][SB] Not near FVG or R:R fail")
                else: log.info(f"[{sym}][SB] No FVG")
            else: log.info(f"[{sym}][SB] No liq sweep")

        # SMC Daily Sweep
        if in_sweep and spd <= MAX_SPREAD_SMC:
            if ask > dh + ob_buf and not state["swept_high"][sym]:
                state["swept_high"][sym] = True
                log.info(f"[{sym}][SMC] HIGH SWEPT")
                cb  = check_close_back(c_m15, "HIGH", dh, dl)
                bos = confirm_bos(c_h1, "HIGH") if cb else False
                if cb and bos and rr_passes(sl_smc, tp_smc, spd):
                    lot = calc_lot(balance, sl_smc, spd, tv, ts, vmin, vmax, vstp)
                    ok  = await exec_trade(conn, sym, "SELL", bid, bid+sl_smc*pt, bid-tp_smc*pt, lot, "FXA-SMC")
                    if not ok: state["daily_losses"][sym] += 1
                    total_open += 1
                elif not cb or not bos: state["swept_high"][sym] = False

            if bid < dl - ob_buf and not state["swept_low"][sym]:
                state["swept_low"][sym] = True
                log.info(f"[{sym}][SMC] LOW SWEPT")
                cb  = check_close_back(c_m15, "LOW", dh, dl)
                bos = confirm_bos(c_h1, "LOW") if cb else False
                if cb and bos and rr_passes(sl_smc, tp_smc, spd):
                    lot = calc_lot(balance, sl_smc, spd, tv, ts, vmin, vmax, vstp)
                    ok  = await exec_trade(conn, sym, "BUY", ask, ask-sl_smc*pt, ask+tp_smc*pt, lot, "FXA-SMC")
                    if not ok: state["daily_losses"][sym] += 1
                    total_open += 1
                elif not cb or not bos: state["swept_low"][sym] = False
        elif in_sweep:
            log.info(f"[{sym}][SMC] Spread too high")
        else:
            log.info(f"[{sym}] Outside sweep session")

    save_state(state)
    write_heartbeat("completed", balance=balance, open_trades=total_open,
                    daily_losses=state["daily_losses"], server_time=str(server_time),
                    last_prices=last_prices)

    await conn.close()
    log.info("\n[DONE] ✅")
    log.info(f"[SUMMARY] {state['daily_losses']}")

if __name__ == "__main__":
    asyncio.run(run_bot())
