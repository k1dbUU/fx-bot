"""
FX Agent Bot v5.1 — Online
Strategy: SMC Daily Sweep (2R) + ICT Silver Bullet (3R)
Instruments: GOLD#, EURUSD, GBPUSD
Runs on: GitHub Actions (cron) via MetaAPI cloud

Fixes vs v5.0:
- H4 timeframe "1h4" -> "4h" (was breaking all bias reads)
- Connection sync timeout (prevents silent hang killing the job)
- Daily level uses previous completed day, not partial current day
- Indentation bug from heredoc approach eliminated (proper .py file)
- Candle count logging added for verification
- Loss counter only increments on failed order execution
- Rebranded as FX Agent throughout
"""

import os
import json
import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

from metaapi_cloud_sdk import MetaApi
from metaapi_cloud_sdk.clients.metaApi.tradeException import TradeException

# ── CONFIG ────────────────────────────────────────────────────────
META_API_TOKEN  = os.environ["META_API_TOKEN"]
META_ACCOUNT_ID = os.environ["META_ACCOUNT_ID"]
STATE_FILE      = "state.json"

RISK_PERCENT     = 5.0
MAX_DAILY_LOSSES = 2
MAX_OPEN_TRADES  = 3
OB_BUFFER        = 0.50

GOLD_SL    = 200;  GOLD_TP    = 400
GOLD_SB_SL = 150;  GOLD_SB_TP = 450
FX_SL      = 200;  FX_TP      = 400
FX_SB_SL   = 150;  FX_SB_TP   = 450

MAX_SPREAD_SMC = 200
MAX_SPREAD_SB  = 150

SYMBOLS = ["GOLD#", "EURUSD", "GBPUSD"]
AGENT_ID = 202605
SYNC_TIMEOUT_SECONDS = 300

# ── LOGGING ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("FX-AGENT")

# ── STATE ─────────────────────────────────────────────────────────
def load_state() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default = {
        "date": today,
        "daily_losses": {s: 0     for s in SYMBOLS},
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
            log.warning(f"[STATE] Load error: {e} — using fresh state")
    log.info("[STATE] New day or no state — reset")
    return default

def save_state(state: dict):
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))
    log.info("[STATE] Saved")

# ── TIME ──────────────────────────────────────────────────────────
def get_ny_hour() -> int:
    utc = datetime.now(timezone.utc)
    offset = -4 if 3 <= utc.month <= 11 else -5
    return (utc + timedelta(hours=offset)).hour

def is_sweep_session() -> bool:
    utc = datetime.now(timezone.utc)
    h, m = utc.hour, utc.minute
    return (h == 13 and m >= 30) or (14 <= h <= 19)

def is_friday() -> bool:
    return datetime.now(timezone.utc).weekday() == 4

# ── BIAS — H4 ─────────────────────────────────────────────────────
def get_bias(candles: list) -> str:
    if len(candles) < 13:
        log.warning("  [BIAS] Insufficient H4 candles — NEUTRAL")
        return "NEUTRAL"
    recent = candles[-13:]
    hi  = max(c["high"]  for c in recent)
    lo  = min(c["low"]   for c in recent)
    mid = (hi + lo) / 2.0
    rc  = recent[-2]["close"]
    if rc > mid * 1.001: return "BULLISH"
    if rc < mid * 0.999: return "BEARISH"
    return "NEUTRAL"

# ── R:R GUARD ─────────────────────────────────────────────────────
def rr_passes(sl: int, tp: int, spd: int) -> bool:
    esl = sl + spd
    etp = tp - spd
    if etp <= 0:
        log.info("  [RR] ❌ TP wiped by spread")
        return False
    rr = etp / esl
    log.info(f"  [RR] {rr:.2f} | eff-SL:{esl}pts eff-TP:{etp}pts")
    if rr < 1.0:
        log.info("  [RR] ❌ R:R below 1.0 — blocked")
        return False
    return True

# ── LOT CALC ──────────────────────────────────────────────────────
def calc_lot(balance: float, sl_pts: int, spd: int,
             tv: float, ts: float,
             vmin: float, vmax: float, vstp: float) -> float:
    risk = balance * (RISK_PERCENT / 100.0)
    ppv  = tv / ts if ts > 0 else 0
    esl  = sl_pts + spd
    if ppv <= 0 or esl <= 0:
        return vmin
    raw = risk / (esl * ppv)
    lot = math.floor(raw / vstp) * vstp
    lot = round(max(min(lot, vmax), vmin), 2)
    actual = lot * esl * ppv
    while actual > risk and lot > vmin:
        lot    = round(lot - vstp, 2)
        actual = lot * esl * ppv
    log.info(f"  [LOT] {lot} lots | Budget:{risk:.2f} | Actual:{actual:.2f}")
    return lot

# ── LIQUIDITY SWEEP — M5 ──────────────────────────────────────────
def find_liq_sweep(candles: list, bias: str) -> bool:
    if len(candles) < 12:
        return False
    pool    = candles[-10:-1]
    trigger = candles[-1]
    if bias == "BULLISH":
        sw = min(c["low"] for c in pool)
        if trigger["low"] < sw and trigger["close"] > sw:
            log.info("  [SB-LIQ] Bullish sweep ✅")
            return True
    else:
        sw = max(c["high"] for c in pool)
        if trigger["high"] > sw and trigger["close"] < sw:
            log.info("  [SB-LIQ] Bearish sweep ✅")
            return True
    return False

# ── FVG — M5 ──────────────────────────────────────────────────────
def find_fvg(candles: list, bias: str):
    if len(candles) < 4:
        return None
    for i in range(1, min(len(candles) - 1, 29)):
        c1, c2, c3 = candles[i - 1], candles[i], candles[i + 1]
        c2t = max(c2["open"], c2["close"])
        c2b = min(c2["open"], c2["close"])
        if bias == "BULLISH" and c1["low"] > c3["high"] and c2t > c2b * 1.001:
            log.info("  [FVG] Bullish ✅")
            return (c1["low"], c3["high"])
        if bias == "BEARISH" and c1["high"] < c3["low"] and c2b < c2t * 0.999:
            log.info("  [FVG] Bearish ✅")
            return (c3["low"], c1["high"])
    return None

# ── CLOSE-BACK — M15 ──────────────────────────────────────────────
def check_close_back(candles: list, sweep_type: str, dh: float, dl: float) -> bool:
    for c in reversed(candles[-5:]):
        if sweep_type == "HIGH" and c["close"] < dh:
            log.info("  [CB] ✅ Closed back below DH")
            return True
        if sweep_type == "LOW" and c["close"] > dl:
            log.info("  [CB] ✅ Closed back above DL")
            return True
    return False

# ── H1 BOS ────────────────────────────────────────────────────────
def confirm_bos(candles: list, sweep_type: str) -> bool:
    if len(candles) < 8:
        return False
    pool = candles[-8:]
    hi   = max(c["high"] for c in pool)
    lo   = min(c["low"]  for c in pool)
    mid  = (hi + lo) / 2.0
    rc   = pool[-2]["close"]
    if sweep_type == "HIGH" and rc < mid:
        log.info("  [BOS] ✅ H1 bearish BOS confirmed")
        return True
    if sweep_type == "LOW" and rc > mid:
        log.info("  [BOS] ✅ H1 bullish BOS confirmed")
        return True
    return False

# ── EXECUTE TRADE ─────────────────────────────────────────────────
async def exec_trade(conn, sym: str, direction: str,
                     price: float, sl: float, tp: float,
                     lot: float, comment: str) -> bool:
    try:
        opts = {"comment": f"{comment} {sym}", "clientId": f"FXA{AGENT_ID}"}
        if direction == "BUY":
            await conn.create_market_buy_order(sym, lot, sl, tp, opts)
        else:
            await conn.create_market_sell_order(sym, lot, sl, tp, opts)
        log.info(f"  [TRADE] ✅ {sym} {direction} Lot:{lot} SL:{sl:.5f} TP:{tp:.5f}")
        return True
    except TradeException as e:
        log.error(f"  [TRADE] ❌ {sym} | TradeException: {e.message}")
        return False
    except Exception as e:
        log.error(f"  [TRADE] ❌ {sym} | Error: {e}")
        return False

# ── MAIN ──────────────────────────────────────────────────────────
async def run_bot():
    log.info("══════════════════════════════════════════")
    log.info("FX Agent v5.1 — ONLINE — GitHub Actions")
    log.info("══════════════════════════════════════════")

    if is_friday():
        log.info("[FRIDAY] No trades per rules. Done.")
        return

    state = load_state()

    api  = MetaApi(META_API_TOKEN)
    acct = await api.metatrader_account_api.get_account(META_ACCOUNT_ID)

    if acct.state not in ("DEPLOYING", "DEPLOYED"):
        log.info("[META] Deploying account...")
        await acct.deploy()

    log.info("[META] Waiting for broker connection...")
    await acct.wait_connected()

    conn = acct.get_rpc_connection()
    await conn.connect()

    try:
        await asyncio.wait_for(
            conn.wait_synchronized(),
            timeout=SYNC_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        log.error(f"[META] Sync timeout after {SYNC_TIMEOUT_SECONDS}s — aborting")
        await conn.close()
        save_state(state)
        return

    log.info("[META] Connected and synchronized ✅")

    acct_info  = await conn.get_account_information()
    balance    = acct_info["balance"]
    log.info(f"[ACCOUNT] Balance: {balance:.2f}")

    positions  = await conn.get_positions()
    total_open = len([p for p in positions if p["symbol"] in SYMBOLS])
    ny_hour    = get_ny_hour()
    in_sweep   = is_sweep_session()
    log.info(f"[SESSION] NY Hour:{ny_hour} | Sweep:{in_sweep} | Open:{total_open}")

    for sym in SYMBOLS:
        log.info(f"\n{'─'*45}")
        log.info(f"[{sym}]")

        if state["daily_losses"][sym] >= MAX_DAILY_LOSSES:
            log.info(f"[{sym}] Daily loss limit reached — skip")
            continue

        if total_open >= MAX_OPEN_TRADES:
            log.info("[GLOBAL] Max open trades reached — stopping")
            break

        try:
            tick = await conn.get_symbol_price(sym)
            bid  = tick["bid"]
            ask  = tick["ask"]
            spec = await conn.get_symbol_specification(sym)
            pt   = spec["point"]
            spd  = round((ask - bid) / pt)
            tv   = spec["tickValue"]
            ts   = spec["tickSize"]
            vmin = spec["minVolume"]
            vmax = spec["maxVolume"]
            vstp = spec["volumeStep"]
            log.info(f"[{sym}] BID:{bid} ASK:{ask} Spread:{spd}pts")
        except Exception as e:
            log.warning(f"[{sym}] Price/spec error: {e} — skip")
            continue

        try:
            now   = datetime.now(timezone.utc)
            c_h4  = await conn.get_historical_candles(sym, "4h",  now - timedelta(hours=60),  15)
            c_h1  = await conn.get_historical_candles(sym, "1h",  now - timedelta(hours=10),  10)
            c_m15 = await conn.get_historical_candles(sym, "15m", now - timedelta(hours=2),    8)
            c_m5  = await conn.get_historical_candles(sym, "5m",  now - timedelta(hours=2),   30)
            c_d1  = await conn.get_historical_candles(sym, "1d",  now - timedelta(days=3),     3)
            log.info(f"[{sym}] Candles H4:{len(c_h4)} H1:{len(c_h1)} M15:{len(c_m15)} M5:{len(c_m5)} D1:{len(c_d1)}")
        except Exception as e:
            log.warning(f"[{sym}] Candle error: {e} — skip")
            continue

        if len(c_d1) >= 2:
            dh = c_d1[-2]["high"]
            dl = c_d1[-2]["low"]
        elif c_d1:
            dh = c_d1[-1]["high"]
            dl = c_d1[-1]["low"]
        else:
            dh = bid;  dl = bid

        bias   = get_bias(c_h4)
        sl_smc = GOLD_SL    if sym == "GOLD#" else FX_SL
        tp_smc = GOLD_TP    if sym == "GOLD#" else FX_TP
        sl_sb  = GOLD_SB_SL if sym == "GOLD#" else FX_SB_SL
        tp_sb  = GOLD_SB_TP if sym == "GOLD#" else FX_SB_TP

        log.info(f"[{sym}] Bias:{bias} | DH:{dh:.5f} DL:{dl:.5f}")

        # ── ICT SILVER BULLET ─────────────────────────────────────
        sb_win = None
        if ny_hour == 3  and not state["sb_london"][sym]:  sb_win = "LONDON"
        if ny_hour == 10 and not state["sb_ny_am"][sym]:   sb_win = "NY_AM"
        if ny_hour == 14 and not state["sb_ny_pm"][sym]:   sb_win = "NY_PM"

        if sb_win:
            state[f"sb_{sb_win.lower()}"][sym] = True
            log.info(f"[{sym}][SB] Window: {sb_win}")

            if spd > MAX_SPREAD_SB:
                log.info(f"[{sym}][SB] Spread {spd}pts exceeds cap — skip")
            elif bias == "NEUTRAL":
                log.info(f"[{sym}][SB] Bias neutral — no trade")
            elif find_liq_sweep(c_m5, bias):
                fvg = find_fvg(c_m5, bias)
                if fvg:
                    ft, fb = fvg
                    mid    = (bid + ask) / 2.0
                    near   = (abs(mid - fb) <= OB_BUFFER or
                              abs(mid - ft) <= OB_BUFFER or
                              fb <= mid <= ft)
                    if near and rr_passes(sl_sb, tp_sb, spd):
                        lot = calc_lot(balance, sl_sb, spd, tv, ts, vmin, vmax, vstp)
                        if bias == "BULLISH":
                            ok = await exec_trade(conn, sym, "BUY",
                                                  ask, ask - sl_sb * pt, ask + tp_sb * pt,
                                                  lot, "FXA-SB")
                        else:
                            ok = await exec_trade(conn, sym, "SELL",
                                                  bid, bid + sl_sb * pt, bid - tp_sb * pt,
                                                  lot, "FXA-SB")
                        if not ok:
                            state["daily_losses"][sym] += 1
                        total_open += 1
                    else:
                        log.info(f"[{sym}][SB] Price not near FVG or R:R failed")
                else:
                    log.info(f"[{sym}][SB] No FVG found after sweep")
            else:
                log.info(f"[{sym}][SB] No liquidity sweep")

        # ── SMC DAILY SWEEP ───────────────────────────────────────
        if in_sweep and spd <= MAX_SPREAD_SMC:

            if ask > dh + OB_BUFFER and not state["swept_high"][sym]:
                state["swept_high"][sym] = True
                log.info(f"[{sym}][SMC] HIGH SWEPT — checking SHORT")
                cb  = check_close_back(c_m15, "HIGH", dh, dl)
                bos = confirm_bos(c_h1, "HIGH") if cb else False
                if cb and bos and rr_passes(sl_smc, tp_smc, spd):
                    lot = calc_lot(balance, sl_smc, spd, tv, ts, vmin, vmax, vstp)
                    ok  = await exec_trade(conn, sym, "SELL",
                                           bid, bid + sl_smc * pt, bid - tp_smc * pt,
                                           lot, "FXA-SMC")
                    if not ok:
                        state["daily_losses"][sym] += 1
                    total_open += 1
                elif not cb or not bos:
                    state["swept_high"][sym] = False

            if bid < dl - OB_BUFFER and not state["swept_low"][sym]:
                state["swept_low"][sym] = True
                log.info(f"[{sym}][SMC] LOW SWEPT — checking LONG")
                cb  = check_close_back(c_m15, "LOW", dh, dl)
                bos = confirm_bos(c_h1, "LOW") if cb else False
                if cb and bos and rr_passes(sl_smc, tp_smc, spd):
                    lot = calc_lot(balance, sl_smc, spd, tv, ts, vmin, vmax, vstp)
                    ok  = await exec_trade(conn, sym, "BUY",
                                           ask, ask - sl_smc * pt, ask + tp_smc * pt,
                                           lot, "FXA-SMC")
                    if not ok:
                        state["daily_losses"][sym] += 1
                    total_open += 1
                elif not cb or not bos:
                    state["swept_low"][sym] = False

        elif in_sweep:
            log.info(f"[{sym}][SMC] Spread {spd}pts exceeds cap — skip")
        else:
            log.info(f"[{sym}] Outside sweep session")

    save_state(state)
    await conn.close()
    log.info("\n[DONE] ✅")
    log.info(f"[SUMMARY] Losses today: { {s: state['daily_losses'][s] for s in SYMBOLS} }")


if __name__ == "__main__":
    asyncio.run(run_bot())
