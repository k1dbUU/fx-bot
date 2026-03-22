"""
╔══════════════════════════════════════════════════════════════════╗
║  NUCLEUS SUPERVISOR v4.0 — K-I-D-B-U-U                          ║
║  Runs every 5 minutes — always online, always ready             ║
║  Monitors: FX + Job + Shopify agents                            ║
║  Builds: Shopify Agent (Priority Task 1)                        ║
║  Responds: War Room commands from nucleus_command.json          ║
║  Deadline: End of March 2026 — revenue or reset                 ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, asyncio, smtplib, httpx, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.mime.text import MIMEText

# ── IDENTITY ──────────────────────────────────────────────────────
OPERATOR  = os.getenv("OPERATOR_ALIAS", "K-I-D-B-U-U")
VERSION   = "4.0"

# ── SECRETS ───────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
GMAIL_FROM         = os.getenv("GMAIL_FROM", "")
GMAIL_TO           = os.getenv("GMAIL_TO", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# ── SHOPIFY BUILD PHASES ──────────────────────────────────────────
# Each phase is one Claude API call worth of work.
# Supervisor works through phases sequentially across runs.
SHOPIFY_PHASES = [
    {"id": 1,  "name": "Architecture design",          "pct": 5},
    {"id": 2,  "name": "Shopify API auth + connection", "pct": 12},
    {"id": 3,  "name": "Product catalogue CRUD",        "pct": 22},
    {"id": 4,  "name": "Image upload handler",          "pct": 30},
    {"id": 5,  "name": "Inventory management",          "pct": 40},
    {"id": 6,  "name": "Pricing engine",                "pct": 48},
    {"id": 7,  "name": "Order processing",              "pct": 58},
    {"id": 8,  "name": "Voice command parser",          "pct": 68},
    {"id": 9,  "name": "Voice ordering integration",    "pct": 78},
    {"id": 10, "name": "Autonomous monitoring loop",    "pct": 88},
    {"id": 11, "name": "Security audit + leak scan",    "pct": 94},
    {"id": 12, "name": "Final integration + deploy",    "pct": 100},
]

# ── AGENT REGISTRY ────────────────────────────────────────────────
AGENTS = [
    {"name": "FX Agent",      "status_file": "status.json",           "stale_hours": 0.2,  "critical": True},
    {"name": "Job Agent",     "status_file": "job_agent_status.json", "stale_hours": 0.2,  "critical": False},
    {"name": "Shopify Agent", "status_file": "shopify_status.json",   "stale_hours": 24,   "critical": False},
]

# ── FILE PATHS ────────────────────────────────────────────────────
CORTEX_FILE   = "cortex_log.json"
MEMORY_FILE   = "NUCLEUS_MEMORY.json"
SHOPIFY_BUILD = "shopify_build_log.json"
CMD_FILE      = "nucleus_command.json"
MAX_CORTEX    = 100
MAX_LESSONS   = 10
CLAUDE_MODEL  = "claude-sonnet-4-20250514"

# ── LEAK SCANNER ──────────────────────────────────────────────────
_LEAK_PATTERNS = ["sk-ant-", "shpat_", "app_password", "hhkps"]

def is_clean(text: str) -> bool:
    for p in _LEAK_PATTERNS:
        if p.lower() in text.lower():
            print(f"[SECURITY] ⚠ LEAK BLOCKED — pattern '{p}' found")
            return False
    return True

# ── HELPERS ───────────────────────────────────────────────────────
def load_json(path: str, default):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return default

def save_json(path: str, data):
    Path(path).write_text(json.dumps(data, indent=2))

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def sast_now() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M SAST")

def hours_since(iso_str: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return 999.0

def days_until_end_of_march() -> int:
    now = datetime.now(timezone.utc)
    eom = datetime(2026, 3, 31, 23, 59, 59, tzinfo=timezone.utc)
    return max(0, (eom - now).days)

def is_fx_session() -> bool:
    u = datetime.now(timezone.utc)
    return u.weekday() in (0, 1, 2, 3) and 13 <= u.hour < 20

def is_overnight() -> bool:
    return 0 <= datetime.now(timezone.utc).hour < 12

# ── CLAUDE API ────────────────────────────────────────────────────
async def call_claude(system: str, user: str, max_tokens: int = 800) -> str:
    if not ANTHROPIC_API_KEY:
        return "[No ANTHROPIC_API_KEY]"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                }
            )
            return r.json()["content"][0]["text"]
    except Exception as e:
        return f"[Claude error: {e}]"

# ── EMAIL ─────────────────────────────────────────────────────────
def send_alert(subject: str, body: str):
    if not all([GMAIL_FROM, GMAIL_TO, GMAIL_APP_PASSWORD]):
        print(f"[ALERT] Gmail not configured — skipping: {subject}")
        return
    if not is_clean(body):
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = f"[Nucleus · {OPERATOR}] {subject}"
        msg["From"]    = GMAIL_FROM
        msg["To"]      = GMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        print(f"[ALERT] ✉ Sent: {subject}")
    except Exception as e:
        print(f"[ALERT] Failed: {e}")

# ── COMMAND READER ────────────────────────────────────────────────
def read_command() -> dict:
    """Reads War Room command. Returns parsed dict or empty."""
    try:
        if not Path(CMD_FILE).exists():
            return {}
        data = load_json(CMD_FILE, {})
        if data.get("status") == "executed":
            return {}
        cmd = (data.get("command") or "").lower().strip()
        if not cmd:
            return {}
        print(f"[COMMAND] War Room: '{cmd}'")
        return {"raw": cmd, "issued_at": data.get("issued_at", "")}
    except Exception as e:
        print(f"[COMMAND] Read error: {e}")
        return {}

def clear_command():
    """Marks command as executed."""
    try:
        data = load_json(CMD_FILE, {})
        data["status"] = "executed"
        data["executed_at"] = utc_now()
        save_json(CMD_FILE, data)
    except Exception:
        pass

# ── WAR ROOM RESPONSE ─────────────────────────────────────────────
async def handle_war_room_command(cmd: dict):
    """
    Handles natural language commands from the War Room.
    Writes response back to nucleus_command.json for dashboard to read.
    """
    raw = cmd.get("raw", "")
    if not raw:
        return

    # Status queries — respond with live data
    status_keywords = ["busy", "doing", "status", "update", "how far", "progress",
                       "what are you", "what's everyone", "whats everyone", "report"]
    if any(k in raw for k in status_keywords):
        await generate_status_report()
        return

    # Shopify build queries
    if "shopify" in raw and any(k in raw for k in ["how far", "progress", "status", "update"]):
        await generate_shopify_report()
        return

    # Generic — let Claude interpret
    response = await call_claude(
        system=f"""You are Nucleus Supervisor for {OPERATOR}.
You received a command from the War Room (operator dashboard).
Read it and respond in 2-3 sentences as the Supervisor.
Be direct, specific, honest. No fluff.""",
        user=f"Command: {raw}\n\nCurrent time: {sast_now()}\nFX session: {is_fx_session()}\nDays until March deadline: {days_until_end_of_march()}",
        max_tokens=200,
    )

    if is_clean(response):
        data = load_json(CMD_FILE, {})
        data["supervisor_response"] = response
        data["response_at"] = utc_now()
        data["status"] = "responded"
        save_json(CMD_FILE, data)
        print(f"[COMMAND] Responded: {response[:80]}...")

async def generate_status_report():
    """Generates live status report for all agents."""
    fx     = load_json("status.json", {})
    jobs   = load_json("job_agent_status.json", {})
    shop   = load_json("shopify_status.json", {})
    build  = load_json(SHOPIFY_BUILD, {})
    cortex = load_json(CORTEX_FILE, [])

    fx_summary = "No data yet"
    if fx:
        bal = fx.get("balance", 0)
        prices = fx.get("last_prices", {})
        session = "IN SESSION — watching for setups" if is_fx_session() else "OFFLINE — markets closed"
        gold = prices.get("GOLD#", {}).get("bid", "—")
        fx_summary = f"Balance ZAR {bal:.2f}. GOLD# at {gold}. {session}."

    jobs_summary = "No data yet"
    if jobs:
        sent = jobs.get("sent", 0)
        skip = jobs.get("skipped", 0)
        lr   = jobs.get("last_run", "")
        hrs  = hours_since(lr) if lr else 999
        jobs_summary = f"Last run {hrs:.0f}h ago. {sent} sent, {skip} skipped this run."

    build_pct  = build.get("percent_complete", 0)
    build_phase = build.get("current_phase", "Not started")
    shop_summary = f"Build {build_pct}% complete. Current phase: {build_phase}."

    days_left = days_until_end_of_march()

    report = await call_claude(
        system=f"""You are Nucleus Supervisor for {OPERATOR}.
Generate a short War Room status report. Speak as yourself (Supervisor).
Include brief status from each agent. Be honest. No fluff. Max 4 sentences total.
Format: one sentence per agent, then one sentence about the March deadline.""",
        user=f"""FX Agent: {fx_summary}
Job Agent: {jobs_summary}
Shopify Agent: {shop_summary}
Days until March 31 deadline: {days_left}
Current time: {sast_now()}""",
        max_tokens=250,
    )

    if is_clean(report):
        data = load_json(CMD_FILE, {})
        data["supervisor_response"] = report
        data["response_at"] = utc_now()
        data["status"] = "responded"
        data["agent_snapshots"] = {
            "fx":      fx_summary,
            "jobs":    jobs_summary,
            "shopify": shop_summary,
        }
        save_json(CMD_FILE, data)
        print(f"[STATUS REPORT] Written to command file")

async def generate_shopify_report():
    """Generates Shopify build progress report."""
    build = load_json(SHOPIFY_BUILD, {})
    pct   = build.get("percent_complete", 0)
    phase = build.get("current_phase", "Not started")
    log   = build.get("build_log", [])
    last_entry = log[-1]["note"] if log else "No entries yet"

    data = load_json(CMD_FILE, {})
    data["supervisor_response"] = f"Shopify Agent build is {pct}% complete. Currently on: {phase}. Latest: {last_entry}"
    data["response_at"] = utc_now()
    data["status"] = "responded"
    save_json(CMD_FILE, data)

# ── SHOPIFY AGENT BUILDER ─────────────────────────────────────────
async def build_shopify_agent():
    """
    Works through SHOPIFY_PHASES one phase per run.
    Generates real Python code for each phase via Claude.
    Commits progress to shopify_build_log.json.
    """
    build = load_json(SHOPIFY_BUILD, {
        "started_at":       utc_now(),
        "percent_complete": 0,
        "current_phase":    "Not started",
        "current_phase_id": 0,
        "build_log":        [],
        "shopify_agent_code": {},
    })

    completed_id = build.get("current_phase_id", 0)

    # Find next phase to work on
    next_phase = None
    for phase in SHOPIFY_PHASES:
        if phase["id"] > completed_id:
            next_phase = phase
            break

    if not next_phase:
        print("[SHOPIFY] ✅ All phases complete!")
        build["percent_complete"] = 100
        build["current_phase"] = "COMPLETE"
        build["completed_at"] = utc_now()
        save_json(SHOPIFY_BUILD, build)
        send_alert("Shopify Agent Build Complete", "All 12 phases done. shopify_agent.py is ready to deploy.")
        return

    print(f"[SHOPIFY] Building phase {next_phase['id']}: {next_phase['name']}")

    # Generate code for this phase
    code = await call_claude(
        system=f"""You are building a Shopify Agent for {OPERATOR} — a performance marketing operator based in Cape Town.
The agent manages a Shopify store autonomously. It will handle: catalogue management, pricing, inventory, orders, and voice commands.
Write production-quality Python code. Use environment variables for all secrets (SHOPIFY_API_KEY, SHOPIFY_STORE_URL).
No hardcoded credentials. Clean, modular, documented code.
Output ONLY the Python code for the requested phase. No explanation, no markdown fences.""",
        user=f"""Phase {next_phase['id']} of 12: {next_phase['name']}

Previously completed phases: {[p['name'] for p in SHOPIFY_PHASES if p['id'] < next_phase['id']]}

Write the Python code module for: {next_phase['name']}
This will be part of shopify_agent.py — write it as a standalone function or class.
Include docstring, error handling, and a brief comment on what it does.""",
        max_tokens=1200,
    )

    if not is_clean(code):
        print(f"[SHOPIFY] Phase {next_phase['id']} output failed leak check — skipping")
        return

    # Save progress
    build["current_phase_id"] = next_phase["id"]
    build["current_phase"]    = next_phase["name"]
    build["percent_complete"] = next_phase["pct"]
    build["last_updated"]     = utc_now()
    build["shopify_agent_code"][f"phase_{next_phase['id']}"] = code

    log_entry = {
        "phase_id":   next_phase["id"],
        "phase_name": next_phase["name"],
        "timestamp":  utc_now(),
        "pct":        next_phase["pct"],
        "note":       f"Phase {next_phase['id']} ({next_phase['name']}) generated — {len(code)} chars",
    }
    build["build_log"].append(log_entry)
    save_json(SHOPIFY_BUILD, build)

    print(f"[SHOPIFY] ✅ Phase {next_phase['id']} done — {next_phase['pct']}% complete")

    # Email milestone alerts at 25%, 50%, 75%, 100%
    if next_phase["pct"] in [25, 50, 75, 100]:
        send_alert(
            f"Shopify Agent {next_phase['pct']}% Built",
            f"Phase {next_phase['id']}: {next_phase['name']} complete.\n\nBuild log entry:\n{log_entry['note']}\n\n— Supervisor v{VERSION}"
        )

# ── AGENT MONITOR ─────────────────────────────────────────────────
async def monitor_all_agents() -> list:
    issues = []
    for agent in AGENTS:
        name  = agent["name"]
        fpath = agent["status_file"]
        stale = agent["stale_hours"]
        crit  = agent["critical"]
        status = load_json(fpath, {})
        if not status:
            print(f"[MONITOR] ℹ {name}: No status file yet")
            continue
        last_run = status.get("last_run") or status.get("last_seen_utc")
        error    = status.get("error")
        version  = status.get("version") or status.get("agent_version", "?")
        if error:
            msg = f"{name} — ERROR: {error}"
            print(f"[MONITOR] ⚠ {msg}")
            issues.append({"agent": name, "type": "error", "detail": error})
            if crit:
                send_alert(f"{name} Error", msg)
        elif last_run:
            hrs = hours_since(last_run)
            if hrs > stale and is_fx_session():
                msg = f"{name} — STALE: {hrs:.1f}h (limit {stale}h)"
                print(f"[MONITOR] ⚠ {msg}")
                issues.append({"agent": name, "type": "stale", "detail": msg})
                if crit:
                    send_alert(f"{name} Stale", msg)
            else:
                bal = status.get("balance", "")
                bal_str = f" | ZAR {bal}" if bal else ""
                print(f"[MONITOR] ✅ {name} v{version} — {hrs:.1f}h ago{bal_str}")
    return issues

# ── CORTEX LOG ────────────────────────────────────────────────────
async def write_cortex():
    fx      = load_json("status.json", {})
    jobs    = load_json("job_agent_status.json", {})
    build   = load_json(SHOPIFY_BUILD, {})
    prices  = fx.get("last_prices", {})
    balance = fx.get("balance", 0)
    run_num = fx.get("run_number", "?")
    fx_stat = fx.get("status", "unknown")
    days_left = days_until_end_of_march()

    lines = [
        f"Nucleus v{VERSION} — {sast_now()}",
        f"FX: Run #{run_num} | {fx_stat.upper()} | ZAR {balance:.2f} (~${balance/18.5:.2f})",
    ]
    for sym, p in prices.items():
        lines.append(f"  {sym}: bid {p.get('bid')} | spread {p.get('spread')}pts")

    if jobs:
        lines.append(f"Jobs: {jobs.get('sent',0)} sent | {jobs.get('skipped',0)} skipped")

    shop_pct = build.get("percent_complete", 0)
    shop_phase = build.get("current_phase", "not started")
    lines.append(f"Shopify build: {shop_pct}% — {shop_phase}")
    lines.append(f"March deadline: {days_left} days remaining")
    lines.append(f"Session: {'ACTIVE' if is_fx_session() else 'OFFLINE'}")

    if is_fx_session() and prices and ANTHROPIC_API_KEY:
        px_str = ", ".join(f"{s}: {p.get('bid')}" for s, p in prices.items())
        read = await call_claude(
            system=f"You are Nucleus Supervisor for {OPERATOR}. One sentence. Direct market read. No fluff.",
            user=f"Prices: {px_str}. Status: {fx_stat}. {days_left} days to March deadline. Quick read?",
            max_tokens=60,
        )
        if is_clean(read):
            lines.append(f"Supervisor: {read}")

    entry = {
        "timestamp":      utc_now(),
        "sast":           sast_now(),
        "run":            run_num,
        "fx_status":      fx_stat,
        "balance_zar":    round(float(balance), 2),
        "in_session":     is_fx_session(),
        "shopify_pct":    shop_pct,
        "shopify_phase":  shop_phase,
        "days_to_deadline": days_left,
        "full":           "\n".join(lines),
    }

    log = load_json(CORTEX_FILE, [])
    if not isinstance(log, list):
        log = []
    log.insert(0, entry)
    save_json(CORTEX_FILE, log[:MAX_CORTEX])
    print(f"[CORTEX] Written — {entry['sast']}")

# ── OVERNIGHT LEARNING ────────────────────────────────────────────
async def learn_overnight():
    if not is_overnight():
        return
    cortex = load_json(CORTEX_FILE, [])
    if len(cortex) < 3:
        return
    memory = load_json(MEMORY_FILE, {"lessons": [], "last_lesson_utc": None, "evolution_log": []})
    last = memory.get("last_lesson_utc")
    if last and hours_since(last) < 0.9:
        return
    if not ANTHROPIC_API_KEY:
        return

    recent = "\n---\n".join(e.get("full", "") for e in cortex[:8])
    days_left = days_until_end_of_march()

    lesson = await call_claude(
        system=f"""You are the Nucleus Supervisor for {OPERATOR}.
Extract ONE specific, actionable lesson from these agent logs.
Format: pattern observed → recommended adjustment.
1 sentence. Specific. No generic advice.
Note: {days_left} days remain until the March 31 revenue deadline.""",
        user=f"Logs:\n{recent}\n\nSingle most important lesson?",
        max_tokens=120,
    )

    if not is_clean(lesson):
        return

    lessons = memory.get("lessons", [])
    lessons.insert(0, {"timestamp": utc_now(), "lesson": lesson})
    memory["lessons"]           = lessons[:MAX_LESSONS]
    memory["last_lesson_utc"]   = utc_now()
    evo = memory.get("evolution_log", [])
    evo.insert(0, {"timestamp": utc_now(), "event": f"Lesson by Supervisor v{VERSION}"})
    memory["evolution_log"] = evo[:20]
    save_json(MEMORY_FILE, memory)
    print(f"[MEMORY] Lesson: {lesson[:80]}...")
    send_alert("Overnight Lesson", f"{lesson}\n\n{days_left} days to March deadline.\n— Supervisor v{VERSION}")

# ── MAIN ──────────────────────────────────────────────────────────
async def run():
    print("═" * 56)
    print(f"  NUCLEUS SUPERVISOR v{VERSION} — {OPERATOR}")
    print(f"  {sast_now()}")
    print(f"  March deadline: {days_until_end_of_march()} days remaining")
    print("═" * 56)

    # 1. Read and handle any War Room command first
    cmd = read_command()
    if cmd:
        await handle_war_room_command(cmd)
        clear_command()

    # 2. Monitor all agents
    issues = await monitor_all_agents()

    # 3. Write cortex log
    await write_cortex()

    # 4. Build Shopify Agent (one phase per run — runs every 5 min)
    build = load_json(SHOPIFY_BUILD, {})
    if build.get("percent_complete", 0) < 100:
        await build_shopify_agent()
    else:
        print("[SHOPIFY] ✅ Build already complete")

    # 5. Overnight learning
    await learn_overnight()

    status_summary = "clean" if not issues else f"{len(issues)} issue(s)"
    print(f"\n[SUPERVISOR] Done ✅ — {status_summary} — {sast_now()}")

if __name__ == "__main__":
    asyncio.run(run())
