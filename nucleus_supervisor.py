"""
╔══════════════════════════════════════════════════════════════╗
║  NUCLEUS SUPERVISOR v2.0                                     ║
║  Operator: KidBoo                                            ║
║  Runs headless on GitHub Actions — 24/7, $0 cost            ║
║  Manages: FX Agent + Shopify Agent                          ║
╚══════════════════════════════════════════════════════════════╝

Secrets: ALL via environment variables / GitHub Secrets
Never hardcode. Never expose. KidBoo Privacy Firewall active.
"""

import os
import json
import asyncio
import smtplib
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.mime.text import MIMEText

# ── IDENTITY ─────────────────────────────────────────────────────
OPERATOR   = os.getenv("OPERATOR_ALIAS", "KidBoo")

# ── SECRETS (all from env / GitHub Secrets) ──────────────────────
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
GMAIL_FROM           = os.getenv("GMAIL_FROM", "")
GMAIL_TO             = os.getenv("GMAIL_TO", "")
GMAIL_APP_PASSWORD   = os.getenv("GMAIL_APP_PASSWORD", "")

# ── FILES ─────────────────────────────────────────────────────────
CORTEX_FILE  = "cortex_log.json"
MEMORY_FILE  = "NUCLEUS_MEMORY.json"
STATUS_FILE  = "status.json"
STATE_FILE   = "state.json"

MAX_CORTEX   = 50
MAX_LESSONS  = 10
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# ── HELPERS ───────────────────────────────────────────────────────
def load_json(path, default):
    try:
        if Path(path).exists():
            return json.loads(Path(path).read_text())
    except Exception:
        pass
    return default

def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2))

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def sast_now():
    return (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M SAST")

def is_market_hours():
    utc = datetime.now(timezone.utc)
    return utc.weekday() in (0, 1, 2, 3) and 13 <= utc.hour < 20

# ── SECURITY: LEAK CHECK ─────────────────────────────────────────
def leak_check(text: str) -> list:
    """
    Scans any string for potential secret patterns.
    Returns list of warnings. Call before any git commit or log write.
    """
    warnings = []
    patterns = ["sk-ant-", "shpat_", "hhkps", "@gmail.com", "password", "api_key"]
    for p in patterns:
        if p.lower() in text.lower():
            warnings.append(f"POTENTIAL LEAK DETECTED: pattern '{p}' found in output")
    return warnings

# ── CLAUDE API ────────────────────────────────────────────────────
async def call_claude(system: str, user: str, max_tokens: int = 400) -> str:
    if not ANTHROPIC_API_KEY:
        return "[Supervisor] No ANTHROPIC_API_KEY in secrets — skipping AI pass."
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}]
                }
            )
            data = r.json()
            return data["content"][0]["text"]
    except Exception as e:
        return f"[Claude error: {e}]"

# ── EMAIL ALERT ───────────────────────────────────────────────────
def send_alert(subject: str, body: str):
    """Send email alert via Gmail SMTP. Secrets from env only."""
    if not all([GMAIL_FROM, GMAIL_TO, GMAIL_APP_PASSWORD]):
        print(f"[ALERT] Email skipped — GMAIL secrets not set. Subject: {subject}")
        return
    # Leak check on outgoing email
    warnings = leak_check(body)
    if warnings:
        print(f"[SECURITY] {warnings[0]} — email blocked")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = f"[Nucleus · {OPERATOR}] {subject}"
        msg["From"]    = GMAIL_FROM
        msg["To"]      = GMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        print(f"[ALERT] Sent: {subject}")
    except Exception as e:
        print(f"[ALERT] Failed: {e}")

# ── CORTEX: LIVE THINKING LOG ─────────────────────────────────────
async def write_cortex(status: dict, state: dict):
    prices   = status.get("last_prices", {})
    losses   = status.get("daily_losses", {})
    balance  = status.get("balance", 0)
    s        = status.get("status", "unknown")
    run_num  = status.get("run_number", "?")

    lines = [
        f"Run #{run_num} — {sast_now()} — {s.upper()}",
        f"Balance: ZAR {balance:.2f} (~${balance/18.5:.2f})",
    ]
    for sym, p in prices.items():
        lines.append(f"{sym}: {p.get('bid')} bid | {p.get('spread')}pt spread")
    for sym, v in losses.items():
        if v > 0:
            lines.append(f"⚠ {sym}: {v}/2 daily losses")
    lines.append(f"Session: {'ACTIVE' if is_market_hours() else 'OFFLINE — monitoring'}")

    # AI insight during live session
    if is_market_hours() and s == "completed" and prices and ANTHROPIC_API_KEY:
        px = ", ".join([f"{s}: {p.get('bid')}" for s, p in prices.items()])
        insight = await call_claude(
            system="You are the Nucleus FX Supervisor for KidBoo. One sentence market read. Direct, no fluff.",
            user=f"Prices: {px}. Losses: {losses}. Current situation?",
            max_tokens=80
        )
        lines.append(f"Supervisor: {insight}")

    entry = {
        "timestamp": utc_now(),
        "sast": sast_now(),
        "run": run_num,
        "status": s,
        "balance_zar": round(float(balance), 2),
        "in_session": is_market_hours(),
        "full": "\n".join(lines)
    }

    log = load_json(CORTEX_FILE, [])
    if not isinstance(log, list): log = []
    log.insert(0, entry)
    save_json(CORTEX_FILE, log[:MAX_CORTEX])
    print(f"[CORTEX] Written — {entry['sast']}")

# ── OVERNIGHT LEARNING ────────────────────────────────────────────
async def learn_overnight(status: dict, state: dict):
    if is_market_hours():
        print("[MEMORY] In session — skip overnight learning")
        return

    cortex = load_json(CORTEX_FILE, [])
    if len(cortex) < 3:
        print("[MEMORY] Not enough data yet")
        return

    memory = load_json(MEMORY_FILE, {"lessons": [], "last_lesson_utc": None, "evolution_log": []})
    last = memory.get("last_lesson_utc")
    if last:
        mins = (datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z","+00:00"))).total_seconds() / 60
        if mins < 55:
            print(f"[MEMORY] Last lesson {mins:.0f}m ago — skip (hourly cadence)")
            return

    if not ANTHROPIC_API_KEY:
        print("[MEMORY] No API key — skip")
        return

    recent_text = "\n---\n".join([e.get("full","") for e in cortex[:10]])

    lesson = await call_claude(
        system=f"""You are the Nucleus Supervisor for {OPERATOR}.
Extract ONE specific, actionable lesson from these FX session logs.
Format: 1 sentence. Pattern observed → recommended adjustment.
No generic advice. Specific to the data.""",
        user=f"Recent logs:\n{recent_text}\n\nWhat is the single most important lesson?",
        max_tokens=150
    )

    lessons = memory.get("lessons", [])
    lessons.insert(0, {"timestamp": utc_now(), "lesson": lesson})
    memory["lessons"] = lessons[:MAX_LESSONS]
    memory["last_lesson_utc"] = utc_now()
    memory["evolution_log"] = ([{"timestamp": utc_now(),
        "event": "Overnight lesson by Nucleus Supervisor v2.0"}]
        + memory.get("evolution_log", []))[:20]
    save_json(MEMORY_FILE, memory)
    print(f"[MEMORY] Lesson: {lesson[:80]}...")

    # Email alert for overnight lesson
    send_alert("Overnight Lesson Written", f"Nucleus learned:\n\n{lesson}\n\n— Supervisor v2.0")

# ── SHOPIFY AGENT STATUS CHECK ────────────────────────────────────
async def check_shopify_agent():
    """
    Checks if shopify_agent.py has run and reads its status file.
    Supervisor monitors all agents — not just FX.
    """
    shopify_status = load_json("shopify_status.json", {})
    if not shopify_status:
        print("[SHOPIFY] No shopify_status.json — agent not deployed yet")
        return
    last = shopify_status.get("last_run", "never")
    actions = shopify_status.get("last_actions", [])
    print(f"[SHOPIFY] Last run: {last} | Actions: {len(actions)}")

    # Alert on any price changes (high-level store changes)
    price_changes = [a for a in actions if "price" in str(a).lower()]
    if price_changes:
        send_alert(
            "Shopify Price Change Executed",
            f"The Shopify Agent made {len(price_changes)} price change(s):\n\n" +
            "\n".join(str(p) for p in price_changes)
        )

# ── MAIN ──────────────────────────────────────────────────────────
async def run_supervisor():
    print("═" * 50)
    print(f"Nucleus Supervisor v2.0 — {OPERATOR} — ONLINE")
    print("═" * 50)

    status = load_json(STATUS_FILE, {})
    state  = load_json(STATE_FILE, {})

    if not status:
        print("[SUPERVISOR] No status.json — FX agent not yet run")
        return

    await write_cortex(status, state)
    await learn_overnight(status, state)
    await check_shopify_agent()

    print(f"[SUPERVISOR] Done ✅ — {sast_now()}")

if __name__ == "__main__":
    asyncio.run(run_supervisor())
