"""
╔══════════════════════════════════════════════════════════════════╗
║  NUCLEUS SUPERVISOR v5.0                                         ║
║  Runs every 5 minutes — always online, always ready             ║
║  NEW v5.0:                                                       ║
║    - Email inbox scanner (IMAP) every run                       ║
║    - Responds to emails with subject "Nucleus"                  ║
║    - Sends task transition emails with ETA                      ║
║    - Private info guard: only 2 trusted addresses               ║
║    - Task queue: Shopify → Design Agent → Vercel deploy         ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, asyncio, smtplib, imaplib, email, httpx, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header

# ── IDENTITY ──────────────────────────────────────────────────────
OPERATOR  = os.getenv("OPERATOR_ALIAS", "Nucleus Operator")
VERSION   = "5.0"

# ── SECRETS ───────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
GMAIL_FROM         = os.getenv("GMAIL_FROM", "")
GMAIL_TO           = os.getenv("GMAIL_TO", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# ── TRUSTED ADDRESSES (can request private info) ──────────────────
# All other addresses get public-safe responses only
TRUSTED_EMAILS = {
    "gunsalupanashe@gmail.com",
    "panashegunsalu@gmail.com",
}

# ── TASK QUEUE ────────────────────────────────────────────────────
# Supervisor works through this sequentially.
# Each task has: id, name, description, estimated_days, depends_on
TASK_QUEUE = [
    {
        "id":            "shopify_agent",
        "name":          "Shopify Agent Build",
        "description":   "Build 12-phase autonomous Shopify store manager. Voice ordering, catalogue, inventory, self-healing.",
        "est_days":      0.5,   # ~12 phases × 5min = ~1h
        "milestones":    ["API auth", "Product CRUD", "Image upload", "Inventory", "Pricing", "Orders", "Voice parser", "Voice ordering", "Monitoring", "Self-healing", "Security", "Deploy"],
        "status_file":   "shopify_build_log.json",
        "complete_key":  "percent_complete",
        "complete_val":  100,
    },
    {
        "id":            "design_agent",
        "name":          "Design Agent — Stitch Integration",
        "description":   "Build Design Agent: connects to Google Stitch, generates 4 dashboard themes (2 dark, 2 light) twice daily, deploys selected theme to dashboard automatically via GitHub commit.",
        "est_days":      1,
        "milestones":    ["Stitch API integration", "Theme generation loop", "Theme storage in repo", "Supervisor patch function", "Dashboard theme selector"],
        "status_file":   "design_agent_status.json",
        "complete_key":  "complete",
        "complete_val":  True,
    },
    {
        "id":            "vercel_deploy",
        "name":          "Vercel Deploy — Mobile Access",
        "description":   "Deploy Nucleus dashboard to Vercel for mobile access from any device. Auto-redeploy on every GitHub push.",
        "est_days":      0.5,
        "milestones":    ["Vercel project setup", "GitHub integration", "Environment variables", "Custom domain (optional)", "Mobile test"],
        "status_file":   "vercel_status.json",
        "complete_key":  "deployed",
        "complete_val":  True,
    },
]

# ── SHOPIFY BUILD PHASES ──────────────────────────────────────────
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
CORTEX_FILE    = "cortex_log.json"
MEMORY_FILE    = "NUCLEUS_MEMORY.json"
SHOPIFY_BUILD  = "shopify_build_log.json"
CMD_FILE       = "nucleus_command.json"
EMAIL_LOG_FILE = "nucleus_email_log.json"   # tracks processed email IDs
TASK_LOG_FILE  = "nucleus_task_log.json"    # tracks task transitions
MAX_CORTEX     = 100
MAX_LESSONS    = 10
CLAUDE_MODEL   = "claude-sonnet-4-20250514"

# ── LEAK SCANNER ──────────────────────────────────────────────────
_LEAK_PATTERNS = ["sk-ant-", "shpat_", "app_password", "hhkps", "ghp_", "github_pat_"]

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
    Path(path).write_text(json.dumps(data, indent=2, default=str))

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def sast_now() -> str:
    return datetime.now(timezone(timedelta(hours=2))).strftime("%Y-%m-%d %H:%M SAST")

def hours_since(iso: str) -> float:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except Exception:
        return 999

def is_fx_session() -> bool:
    now = datetime.now(timezone(timedelta(hours=2)))
    return now.weekday() < 4 and 15 <= now.hour < 22

def is_overnight() -> bool:
    h = datetime.now(timezone(timedelta(hours=2))).hour
    return 0 <= h < 6

def days_until_end_of_march() -> int:
    now  = datetime.now(timezone.utc)
    end  = datetime(now.year, 3, 31, 22, 0, tzinfo=timezone.utc)
    if now > end:
        end = datetime(now.year + 1, 3, 31, 22, 0, tzinfo=timezone.utc)
    return max(0, (end - now).days)

# ── CLAUDE API ────────────────────────────────────────────────────
async def call_claude(system: str, user: str, max_tokens: int = 800) -> str:
    if not ANTHROPIC_API_KEY:
        return "[No ANTHROPIC_API_KEY]"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":           ANTHROPIC_API_KEY,
                    "anthropic-version":   "2023-06-01",
                    "content-type":        "application/json",
                },
                json={
                    "model":    CLAUDE_MODEL,
                    "max_tokens": max_tokens,
                    "system":   system,
                    "messages": [{"role": "user", "content": user}],
                }
            )
            return r.json()["content"][0]["text"]
    except Exception as e:
        return f"[Claude error: {e}]"

# ── SEND EMAIL ────────────────────────────────────────────────────
def send_email(to: str, subject: str, body: str):
    """Send an email. Always runs through leak scanner before sending."""
    if not all([GMAIL_FROM, GMAIL_APP_PASSWORD]):
        print(f"[EMAIL] Gmail not configured — skipping: {subject}")
        return False
    if not is_clean(body):
        print(f"[EMAIL] BLOCKED — leak detected in body")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Nucleus Supervisor <{GMAIL_FROM}>"
        msg["To"]      = to
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        print(f"[EMAIL] ✉ Sent to {to}: {subject}")
        return True
    except Exception as e:
        print(f"[EMAIL] Failed: {e}")
        return False

def send_alert(subject: str, body: str):
    """Send alert to the operator's primary address."""
    send_email(GMAIL_TO, f"[Nucleus] {subject}", body)

# ═══════════════════════════════════════════════════════════════════
# ── EMAIL SCANNER ─────────────────────────────────────────────────
# Scans inbox for emails with subject "Nucleus"
# Responds with live system data
# Private info only shared with TRUSTED_EMAILS
# ═══════════════════════════════════════════════════════════════════

def decode_str(s) -> str:
    """Decode email header string (handles encoded UTF-8)."""
    if not s:
        return ""
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)

def get_email_body(msg) -> str:
    """Extract plain text body from email message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    break
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception:
            pass
    return body.strip()[:2000]  # Cap at 2000 chars for Claude

def build_system_context(is_trusted: bool) -> str:
    """Build live system context for Claude to answer from."""
    fx     = load_json("status.json", {})
    jobs   = load_json("job_agent_status.json", {})
    build  = load_json(SHOPIFY_BUILD, {})
    cortex = load_json(CORTEX_FILE, [])
    mem    = load_json(MEMORY_FILE, {})

    # Always-safe public info
    ctx = f"""NUCLEUS SYSTEM STATUS — {sast_now()}
FX Agent: balance ZAR {fx.get('balance', '—')}, status {fx.get('status', '—')}, last seen {fx.get('last_seen_utc', '—')}
FX Session: {'ACTIVE' if is_fx_session() else 'CLOSED'}
Job Agent: {jobs.get('sent', 0)} sent today, {jobs.get('skipped', 0)} skipped
Shopify Build: {build.get('percent_complete', 0)}% — {build.get('current_phase', '—')}
March deadline: {days_until_end_of_march()} days remaining
Latest cortex entry: {cortex[0].get('full', '—')[:300] if cortex else 'None'}"""

    if is_trusted:
        # Add private info only for trusted addresses
        prices = fx.get("last_prices", {})
        gold   = prices.get("GOLD#", {})
        eur    = prices.get("EURUSD", {})
        gbp    = prices.get("GBPUSD", {})
        losses = fx.get("daily_losses", {})
        ctx += f"""

--- PRIVATE (TRUSTED ACCESS) ---
GOLD# bid: {gold.get('bid', '—')} · spread: {gold.get('spread', '—')}pts · losses: {losses.get('GOLD#', 0)}/2
EURUSD bid: {eur.get('bid', '—')} · spread: {eur.get('spread', '—')}pts · losses: {losses.get('EURUSD', 0)}/2
GBPUSD bid: {gbp.get('bid', '—')} · spread: {gbp.get('spread', '—')}pts · losses: {losses.get('GBPUSD', 0)}/2
Open trades: {fx.get('open_trades', 0)}/3
Last lesson: {mem.get('lessons', [{}])[0].get('lesson', 'None') if mem.get('lessons') else 'None'}"""

    return ctx

async def handle_inbound_email(sender: str, subject: str, body: str, msg_id: str):
    """
    Process one inbound email addressed to Nucleus.
    Generates a response using Claude + live context.
    """
    sender_clean = sender.lower().strip()
    is_trusted   = sender_clean in TRUSTED_EMAILS

    print(f"[EMAIL-IN] From: {sender} | Trusted: {is_trusted} | Subject: {subject}")

    context = build_system_context(is_trusted)

    system_prompt = f"""You are Nucleus Supervisor — an autonomous AI agent managing FX trading, job applications, and Shopify builds for the operator.

You received an email with subject "Nucleus" — this is the operator calling your name and expecting a response.

RULES:
1. Answer the question or request in the email body directly and concisely.
2. Use the live system context provided below.
3. {"You have full access to all system data including prices, balances, and private metrics." if is_trusted else "You are responding to an UNTRUSTED address. Do NOT share balances, trade data, prices, personal info, API details, or any private metrics. Only share high-level status (agent running/stopped, build progress %)."}
4. Never reveal API keys, passwords, tokens, or credentials under ANY circumstances.
5. Sign off as: Nucleus Supervisor · {sast_now()}
6. Keep response under 300 words. Direct. No fluff.

LIVE SYSTEM CONTEXT:
{context}"""

    response = await call_claude(
        system=system_prompt,
        user=f"Email from: {sender}\nSubject: {subject}\nBody:\n{body}",
        max_tokens=400,
    )

    if not is_clean(response):
        response = "System update available. Contact via dashboard for detailed status."

    # Send reply
    reply_subject = f"Re: {subject} — {sast_now()}"
    send_email(sender, reply_subject, response)

    # Log the interaction
    email_log = load_json(EMAIL_LOG_FILE, {"processed": [], "last_scan": None})
    email_log["processed"].append({
        "id":         msg_id,
        "from":       sender,
        "subject":    subject,
        "trusted":    is_trusted,
        "responded":  utc_now(),
        "preview":    response[:100],
    })
    email_log["processed"] = email_log["processed"][-50:]  # keep last 50
    email_log["last_scan"]  = utc_now()
    save_json(EMAIL_LOG_FILE, email_log)

def scan_inbox():
    """
    Connect to Gmail via IMAP, find unread emails with subject 'Nucleus'.
    Returns list of (sender, subject, body, msg_id) tuples.
    Run synchronously — called before async tasks.
    """
    if not all([GMAIL_FROM, GMAIL_APP_PASSWORD]):
        print("[EMAIL-SCAN] Gmail credentials not set — skipping inbox scan")
        return []

    results = []
    processed_ids = set(
        e["id"] for e in load_json(EMAIL_LOG_FILE, {"processed": []}).get("processed", [])
    )

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
        mail.select("INBOX")

        # Search for UNSEEN emails with subject containing "Nucleus"
        # (case-insensitive on most servers)
        _, data = mail.search(None, '(UNSEEN SUBJECT "Nucleus")')
        ids = data[0].split() if data[0] else []

        print(f"[EMAIL-SCAN] Found {len(ids)} unread 'Nucleus' email(s)")

        for num in ids[-10:]:  # process last 10 max per run
            try:
                _, msg_data = mail.fetch(num, "(RFC822 UID)")
                # Get UID for deduplication
                uid_data = mail.fetch(num, "(UID)")[1][0].decode()
                uid_match = re.search(r"UID (\d+)", uid_data)
                uid = uid_match.group(1) if uid_match else num.decode()

                if uid in processed_ids:
                    continue

                raw  = msg_data[0][1]
                msg  = email.message_from_bytes(raw)
                sender  = decode_str(msg.get("From", ""))
                subject = decode_str(msg.get("Subject", ""))
                body    = get_email_body(msg)

                # Extract just the email address from "Name <email>" format
                email_match = re.search(r"<(.+?)>", sender)
                sender_addr = email_match.group(1) if email_match else sender

                # Subject must contain "Nucleus" (case-insensitive)
                if "nucleus" not in subject.lower():
                    continue

                results.append((sender_addr, subject, body, uid))

                # Mark as read so we don't re-process
                mail.store(num, "+FLAGS", "\\Seen")

            except Exception as e:
                print(f"[EMAIL-SCAN] Error processing email {num}: {e}")
                continue

        mail.logout()

    except Exception as e:
        print(f"[EMAIL-SCAN] IMAP connection failed: {e}")

    return results

# ═══════════════════════════════════════════════════════════════════
# ── TASK QUEUE & TRANSITION NOTIFICATIONS ─────────────────────────
# ═══════════════════════════════════════════════════════════════════

def get_current_task() -> dict:
    """Returns the first incomplete task in the queue."""
    for task in TASK_QUEUE:
        status = load_json(task["status_file"], {})
        val    = status.get(task["complete_key"])
        if val != task["complete_val"]:
            return task
    return None  # all done

def is_task_complete(task: dict) -> bool:
    """Check if a specific task is marked complete."""
    status = load_json(task["status_file"], {})
    return status.get(task["complete_key"]) == task["complete_val"]

def estimate_task_eta(task: dict) -> str:
    """
    Estimate completion time based on task type and progress.
    For Shopify: uses actual phase timing from build log.
    For others: uses est_days from task config.
    """
    if task["id"] == "shopify_agent":
        build = load_json(SHOPIFY_BUILD, {})
        pct   = build.get("percent_complete", 0)
        log   = build.get("build_log", [])
        if len(log) >= 2:
            # Calculate actual time per phase from real data
            try:
                t_start = datetime.fromisoformat(log[0]["timestamp"].replace("Z", "+00:00"))
                t_last  = datetime.fromisoformat(log[-1]["timestamp"].replace("Z", "+00:00"))
                phases_done = len(log)
                mins_per_phase = (t_last - t_start).total_seconds() / 60 / max(phases_done - 1, 1)
                phases_left = 12 - phases_done
                mins_left   = phases_left * max(mins_per_phase, 5)
                if mins_left < 60:
                    return f"~{int(mins_left)} minutes ({phases_left} phases remaining)"
                else:
                    return f"~{mins_left/60:.1f} hours ({phases_left} phases remaining)"
            except Exception:
                pass
        remaining_pct = 100 - pct
        return f"~{int(remaining_pct * 5 / 100 * 12)} minutes (est.)"
    else:
        days = task["est_days"]
        if days < 1:
            return f"~{int(days * 24)} hours (estimated)"
        return f"~{days} day{'s' if days != 1 else ''} (estimated)"

def build_task_email(task: dict, reason: str = "starting") -> str:
    """Build the task notification email body."""
    eta   = estimate_task_eta(task)
    prev  = None
    for i, t in enumerate(TASK_QUEUE):
        if t["id"] == task["id"] and i > 0:
            prev = TASK_QUEUE[i-1]
            break

    lines = [
        f"NUCLEUS SUPERVISOR — TASK TRANSITION",
        f"{'='*50}",
        f"",
        f"STATUS: {reason.upper()}",
        f"TASK:   {task['name']}",
        f"ETA:    {eta}",
        f"TIME:   {sast_now()}",
        f"",
        f"DESCRIPTION:",
        f"{task['description']}",
        f"",
        f"MILESTONES:",
    ]
    for i, m in enumerate(task["milestones"], 1):
        lines.append(f"  {i}. {m}")

    if prev:
        lines += [
            f"",
            f"PREVIOUS TASK COMPLETED: {prev['name']}",
        ]

    # Add next task preview
    current_idx = next((i for i, t in enumerate(TASK_QUEUE) if t["id"] == task["id"]), -1)
    if current_idx >= 0 and current_idx + 1 < len(TASK_QUEUE):
        next_task = TASK_QUEUE[current_idx + 1]
        lines += [
            f"",
            f"NEXT IN QUEUE: {next_task['name']} (~{next_task['est_days']}d)",
        ]

    # Current system snapshot
    fx    = load_json("status.json", {})
    build = load_json(SHOPIFY_BUILD, {})
    lines += [
        f"",
        f"{'='*50}",
        f"SYSTEM SNAPSHOT:",
        f"  FX Balance: ZAR {fx.get('balance', '—')}",
        f"  FX Session: {'ACTIVE' if is_fx_session() else 'CLOSED'}",
        f"  Shopify Build: {build.get('percent_complete', 0)}%",
        f"  March Deadline: {days_until_end_of_march()} days",
        f"",
        f"Reply to this email with subject 'Nucleus' to communicate with me.",
        f"",
        f"— Nucleus Supervisor v{VERSION}",
        f"  {sast_now()}",
    ]
    return "\n".join(lines)

async def check_task_transitions():
    """
    Check if the current task just completed.
    If yes: send completion email + start notification for next task.
    Uses nucleus_task_log.json to track which transitions already sent.
    """
    task_log = load_json(TASK_LOG_FILE, {"notified": []})
    notified = set(task_log.get("notified", []))

    for i, task in enumerate(TASK_QUEUE):
        task_id = task["id"]

        if is_task_complete(task) and f"{task_id}_complete" not in notified:
            # Task just completed — send completion email
            body = build_task_email(task, reason="COMPLETED")
            send_alert(
                f"✅ Task Complete: {task['name']}",
                body
            )
            notified.add(f"{task_id}_complete")
            print(f"[TASK] ✅ {task['name']} complete — email sent")

            # Notify about next task starting
            if i + 1 < len(TASK_QUEUE):
                next_task = TASK_QUEUE[i + 1]
                if f"{next_task['id']}_start" not in notified:
                    next_body = build_task_email(next_task, reason="STARTING NOW")
                    send_alert(
                        f"🚀 Starting: {next_task['name']} — {estimate_task_eta(next_task)}",
                        next_body
                    )
                    notified.add(f"{next_task['id']}_start")
                    print(f"[TASK] 🚀 {next_task['name']} starting — email sent")

        elif not is_task_complete(task) and f"{task_id}_start" not in notified:
            # First time we're seeing this task — send start notification
            body = build_task_email(task, reason="STARTING")
            send_alert(
                f"🚀 Starting: {task['name']} — {estimate_task_eta(task)}",
                body
            )
            notified.add(f"{task_id}_start")
            print(f"[TASK] 🚀 {task['name']} first run — start email sent")
            break  # Only notify about current task

    task_log["notified"] = list(notified)
    save_json(TASK_LOG_FILE, task_log)

# ═══════════════════════════════════════════════════════════════════
# ── EXISTING FUNCTIONS (unchanged) ────────────────────────────────
# ═══════════════════════════════════════════════════════════════════

async def call_claude_simple(system: str, user: str, max_tokens: int = 800) -> str:
    return await call_claude(system, user, max_tokens)

async def monitor_all_agents() -> list:
    issues = []
    for agent in AGENTS:
        name  = agent["name"]
        fpath = agent["status_file"]
        stale = agent["stale_hours"]
        status = load_json(fpath, {})
        if not status:
            continue
        last_run = status.get("last_run") or status.get("last_seen_utc")
        error    = status.get("error")
        if last_run and hours_since(last_run) > stale and is_fx_session():
            issues.append(f"{name}: stale {hours_since(last_run):.1f}h")
            if agent["critical"]:
                send_alert(f"⚠ {name} STALE", f"{name} last seen {hours_since(last_run):.1f}h ago. Check GitHub Actions.\n{error or ''}")
        if error:
            issues.append(f"{name}: {error}")
            print(f"[MONITOR] ⚠ {name}: {error}")
        else:
            print(f"[MONITOR] ✓ {name}: ok")
    return issues

async def build_shopify_agent():
    build = load_json(SHOPIFY_BUILD, {
        "started_at": utc_now(), "percent_complete": 0,
        "current_phase": "Not started", "current_phase_id": 0,
        "build_log": [], "shopify_agent_code": {},
    })
    completed_id = build.get("current_phase_id", 0)
    next_phase = next((p for p in SHOPIFY_PHASES if p["id"] > completed_id), None)
    if not next_phase:
        print("[SHOPIFY] ✅ All phases complete!")
        build["percent_complete"] = 100
        build["current_phase"]    = "COMPLETE"
        build["completed_at"]     = utc_now()
        save_json(SHOPIFY_BUILD, build)
        return
    print(f"[SHOPIFY] Building phase {next_phase['id']}: {next_phase['name']}")
    code = await call_claude(
        system=f"""You are building a Shopify Agent for the operator.
The agent manages a Shopify store autonomously.
Write production-quality Python. Use environment variables for all secrets.
No hardcoded credentials. Output ONLY the Python code. No markdown fences.""",
        user=f"""Phase {next_phase['id']} of 12: {next_phase['name']}
Previously completed: {[p['name'] for p in SHOPIFY_PHASES if p['id'] < next_phase['id']]}
Write the Python module for: {next_phase['name']}
Standalone function or class. Include docstring, error handling.""",
        max_tokens=1200,
    )
    if not is_clean(code):
        print(f"[SHOPIFY] Phase {next_phase['id']} blocked by leak scanner")
        return
    build["current_phase_id"]  = next_phase["id"]
    build["current_phase"]     = next_phase["name"]
    build["percent_complete"]  = next_phase["pct"]
    build["last_updated"]      = utc_now()
    build["shopify_agent_code"][f"phase_{next_phase['id']}"] = code
    log_entry = {
        "phase_id": next_phase["id"], "phase_name": next_phase["name"],
        "timestamp": utc_now(), "pct": next_phase["pct"],
        "note": f"Phase {next_phase['id']} ({next_phase['name']}) generated — {len(code)} chars",
    }
    build["build_log"].append(log_entry)
    save_json(SHOPIFY_BUILD, build)
    print(f"[SHOPIFY] ✅ Phase {next_phase['id']} done — {next_phase['pct']}%")
    if next_phase["pct"] in [25, 50, 75, 100]:
        send_alert(
            f"Shopify Agent {next_phase['pct']}% Built",
            f"Phase {next_phase['id']}: {next_phase['name']} complete.\n{log_entry['note']}\n— Supervisor v{VERSION}"
        )

async def write_cortex():
    fx    = load_json("status.json",          {})
    jobs  = load_json("job_agent_status.json",{})
    build = load_json(SHOPIFY_BUILD,          {})
    balance   = float(fx.get("balance", 0))
    prices    = fx.get("last_prices", {})
    gold_bid  = prices.get("GOLD#",  {}).get("bid", "—")
    eur_bid   = prices.get("EURUSD", {}).get("bid", "—")
    gbp_bid   = prices.get("GBPUSD", {}).get("bid", "—")
    shop_pct  = build.get("percent_complete", 0)
    shop_phase = build.get("current_phase", "—")
    days_left = days_until_end_of_march()
    now   = datetime.now(timezone(timedelta(hours=2)))
    run_n = os.environ.get("GITHUB_RUN_NUMBER", "0")
    lines = [
        f"Nucleus v{VERSION} — {now.strftime('%Y-%m-%d %H:%M SAST')} | Run #{run_n}",
        f"FX: ZAR {balance:.2f} | {'IN SESSION' if is_fx_session() else 'OFFLINE'}",
        f"GOLD# bid {gold_bid} | EURUSD {eur_bid} | GBPUSD {gbp_bid}",
        f"Jobs: {jobs.get('sent', 0)} sent · {jobs.get('skipped', 0)} skipped",
        f"Shopify: {shop_pct}% — {shop_phase}",
        f"Deadline: {days_left}d remaining",
    ]
    entry = {
        "run":         int(run_n) if run_n.isdigit() else 0,
        "sast":        now.strftime("%Y-%m-%d %H:%M SAST"),
        "timestamp":   utc_now(),
        "status":      "completed",
        "balance_zar": round(balance, 2),
        "in_session":  is_fx_session(),
        "shopify_pct": shop_pct,
        "shopify_phase": shop_phase,
        "days_to_deadline": days_left,
        "full":        "\n".join(lines),
    }
    log_data = load_json(CORTEX_FILE, [])
    if not isinstance(log_data, list):
        log_data = []
    log_data.insert(0, entry)
    save_json(CORTEX_FILE, log_data[:MAX_CORTEX])
    print(f"[CORTEX] Written — {entry['sast']}")

async def handle_war_room_command(cmd: dict):
    raw = cmd.get("raw", "")
    if not raw:
        return
    status_keywords = ["busy", "doing", "status", "update", "how far", "progress",
                       "what are you", "what's everyone", "whats everyone", "report"]
    if any(k in raw for k in status_keywords):
        await generate_status_report()
        return
    if "shopify" in raw and any(k in raw for k in ["how far", "progress", "status", "update"]):
        await generate_shopify_report()
        return
    response = await call_claude(
        system=f"""You are Nucleus Supervisor. You received a command from the dashboard.
Respond in 2-3 sentences. Direct, honest. No fluff.""",
        user=f"Command: {raw}\n\nTime: {sast_now()}\nFX session: {is_fx_session()}\nDays until deadline: {days_until_end_of_march()}",
        max_tokens=200,
    )
    if is_clean(response):
        data = load_json(CMD_FILE, {})
        data["supervisor_response"] = response
        data["response_at"]         = utc_now()
        data["status"]              = "responded"
        save_json(CMD_FILE, data)
        print(f"[COMMAND] Responded: {response[:80]}...")

async def generate_status_report():
    fx    = load_json("status.json", {})
    jobs  = load_json("job_agent_status.json", {})
    build = load_json(SHOPIFY_BUILD, {})
    fx_summary   = f"ZAR {fx.get('balance', 0):.2f}. GOLD# {fx.get('last_prices', {}).get('GOLD#', {}).get('bid', '—')}. {'IN SESSION' if is_fx_session() else 'OFFLINE'}."
    jobs_summary = f"{jobs.get('sent', 0)} sent, {jobs.get('skipped', 0)} skipped."
    shop_summary = f"Build {build.get('percent_complete', 0)}% — {build.get('current_phase', '—')}."
    report = await call_claude(
        system="You are Nucleus Supervisor. Generate a 4-sentence status report. One per agent. Direct.",
        user=f"FX: {fx_summary}\nJobs: {jobs_summary}\nShopify: {shop_summary}\nDays left: {days_until_end_of_march()}",
        max_tokens=250,
    )
    if is_clean(report):
        data = load_json(CMD_FILE, {})
        data["supervisor_response"] = report
        data["response_at"]         = utc_now()
        data["status"]              = "responded"
        save_json(CMD_FILE, data)

async def generate_shopify_report():
    build = load_json(SHOPIFY_BUILD, {})
    pct   = build.get("percent_complete", 0)
    phase = build.get("current_phase", "Not started")
    log_data = build.get("build_log", [])
    last  = log_data[-1]["note"] if log_data else "No entries yet"
    data  = load_json(CMD_FILE, {})
    data["supervisor_response"] = f"Shopify Agent: {pct}% complete. Current phase: {phase}. Latest: {last}"
    data["response_at"]         = utc_now()
    data["status"]              = "responded"
    save_json(CMD_FILE, data)

def read_command() -> dict:
    try:
        if not Path(CMD_FILE).exists():
            return {}
        data = load_json(CMD_FILE, {})
        if data.get("status") == "executed":
            return {}
        cmd = (data.get("command") or "").lower().strip()
        if not cmd:
            return {}
        print(f"[COMMAND] Dashboard: '{cmd}'")
        return {"raw": cmd, "issued_at": data.get("issued_at", "")}
    except Exception as e:
        print(f"[COMMAND] Read error: {e}")
        return {}

def clear_command():
    try:
        data = load_json(CMD_FILE, {})
        data["status"]    = "executed"
        data["executed_at"] = utc_now()
        save_json(CMD_FILE, data)
    except Exception:
        pass

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
    lesson = await call_claude(
        system=f"Extract ONE specific actionable lesson from these agent logs. 1 sentence. No generic advice.",
        user=f"Logs:\n{recent}\n\nSingle most important lesson?",
        max_tokens=120,
    )
    if not is_clean(lesson):
        return
    lessons = memory.get("lessons", [])
    lessons.insert(0, {"timestamp": utc_now(), "lesson": lesson})
    memory["lessons"]         = lessons[:MAX_LESSONS]
    memory["last_lesson_utc"] = utc_now()
    evo = memory.get("evolution_log", [])
    evo.insert(0, {"timestamp": utc_now(), "event": f"Lesson by Supervisor v{VERSION}"})
    memory["evolution_log"] = evo[:20]
    save_json(MEMORY_FILE, memory)
    print(f"[MEMORY] Lesson: {lesson[:80]}...")
    send_alert("Overnight Lesson", f"{lesson}\n\n{days_until_end_of_march()} days to March deadline.\n— Supervisor v{VERSION}")

# ═══════════════════════════════════════════════════════════════════
# ── MAIN ──────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════
async def run():
    print("═" * 60)
    print(f"  NUCLEUS SUPERVISOR v{VERSION}")
    print(f"  {sast_now()}")
    print(f"  March deadline: {days_until_end_of_march()} days remaining")
    print("═" * 60)

    # 1. Scan inbox for "Nucleus" emails — respond to all
    inbound = scan_inbox()
    for sender, subject, body, msg_id in inbound:
        await handle_inbound_email(sender, subject, body, msg_id)

    # 2. Check task queue transitions — send email if task changed
    await check_task_transitions()

    # 3. Read and handle War Room command
    cmd = read_command()
    if cmd:
        await handle_war_room_command(cmd)
        clear_command()

    # 4. Monitor all agents
    issues = await monitor_all_agents()

    # 5. Write cortex log
    await write_cortex()

    # 6. Build current priority task (Shopify until 100%, then Design Agent, then Vercel)
    current_task = get_current_task()
    if current_task:
        if current_task["id"] == "shopify_agent":
            build = load_json(SHOPIFY_BUILD, {})
            if build.get("percent_complete", 0) < 100:
                await build_shopify_agent()
            else:
                print("[SHOPIFY] ✅ Build complete — moving to next task next run")
        else:
            print(f"[TASK] Next task: {current_task['name']} — build code needed")
    else:
        print("[TASK] ✅ All tasks complete")

    # 7. Overnight learning
    await learn_overnight()

    status_summary = "clean" if not issues else f"{len(issues)} issue(s)"
    print(f"\n[SUPERVISOR] Done ✅ — {status_summary} — {sast_now()}")

if __name__ == "__main__":
    asyncio.run(run())
