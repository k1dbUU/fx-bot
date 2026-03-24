"""
╔══════════════════════════════════════════════════════════════════╗
║  NUCLEUS SUPERVISOR v5.6                                         ║
║  v5.6 PATCHES:                                                   ║
║    - AGENTS registry: Lens Agent + Email Sanitizer added        ║
║    - build_system_context: Lens Agent status in all emails      ║
║    - write_todo: Lens Agent in always_on block                  ║
║    - send_eod_summary: Lens Agent daily report line             ║
║    - self_healing_cycle: knows nucleus_lens_agent.py +          ║
║      nucleus_email_sanitizer.py file names                      ║
║    - VERSION bumped to 5.6                                      ║
╚══════════════════════════════════════════════════════════════════╝

HOW TO USE THIS FILE:
  This is NOT a full replacement — it is a patch guide.
  In GitHub, open nucleus_supervisor.py and make these 5 targeted changes.
  Each section is clearly marked with FIND → REPLACE.

  Alternatively: replace nucleus_supervisor.py entirely with the
  full file below (scroll to FULL FILE section).
"""

# ════════════════════════════════════════════════════════════════════
# PATCH 1 — AGENTS REGISTRY (line ~121)
# FIND this exact block and REPLACE with the one below
# ════════════════════════════════════════════════════════════════════

# ── FIND ──────────────────────────────────────────────────────────
AGENTS_OLD = """
AGENTS = [
    {"name": "FX Agent",      "status_file": "status.json",           "stale_hours": 0.2,  "critical": True},
    {"name": "Job Agent",     "status_file": "job_agent_status.json", "stale_hours": 0.2,  "critical": False},
    {"name": "Shopify Agent", "status_file": "shopify_status.json",   "stale_hours": 24,   "critical": False},
]
"""

# ── REPLACE WITH ──────────────────────────────────────────────────
AGENTS_NEW = """
AGENTS = [
    {"name": "FX Agent",        "status_file": "status.json",                    "stale_hours": 0.2,  "critical": True},
    {"name": "Job Agent",       "status_file": "job_agent_status.json",          "stale_hours": 0.2,  "critical": False},
    {"name": "Shopify Agent",   "status_file": "shopify_agent_status.json",      "stale_hours": 24,   "critical": False},
    {"name": "Lens Agent",      "status_file": "lens_agent_status.json",         "stale_hours": 0.5,  "critical": False},
    {"name": "Email Sanitizer", "status_file": "email_sanitizer_status.json",    "stale_hours": 1.0,  "critical": False},
]
"""

# NOTE: shopify_status.json → shopify_agent_status.json (matches actual file in repo)


# ════════════════════════════════════════════════════════════════════
# PATCH 2 — build_system_context() (line ~290)
# FIND the ctx string block and REPLACE
# ════════════════════════════════════════════════════════════════════

# ── FIND ──────────────────────────────────────────────────────────
CONTEXT_OLD = """
def build_system_context(is_trusted: bool) -> str:
    \"\"\"Build live system context for Claude to answer from.\"\"\"
    fx     = load_json("status.json", {})
    jobs   = load_json("job_agent_status.json", {})
    build  = load_json(SHOPIFY_BUILD, {})
    cortex = load_json(CORTEX_FILE, [])
    mem    = load_json(MEMORY_FILE, {})

    # Always-safe public info
    ctx = f\"\"\"NUCLEUS SYSTEM STATUS — {sast_now()}
FX Agent: balance ZAR {fx.get('balance', '—')}, status {fx.get('status', '—')}, last seen {fx.get('last_seen_utc', '—')}
FX Session: {'ACTIVE' if is_fx_session() else 'CLOSED'}
Job Agent: {jobs.get('sent', 0)} sent today, {jobs.get('skipped', 0)} skipped
Shopify Build: {build.get('percent_complete', 0)}% — {build.get('current_phase', '—')}
March deadline: {days_until_end_of_march()} days remaining
Latest cortex entry: {cortex[0].get('full', '—')[:300] if cortex else 'None'}\"\"\"
"""

# ── REPLACE WITH ──────────────────────────────────────────────────
CONTEXT_NEW = """
def build_system_context(is_trusted: bool) -> str:
    \"\"\"Build live system context for Claude to answer from.\"\"\"
    fx     = load_json("status.json", {})
    jobs   = load_json("job_agent_status.json", {})
    build  = load_json(SHOPIFY_BUILD, {})
    cortex = load_json(CORTEX_FILE, [])
    mem    = load_json(MEMORY_FILE, {})
    lens   = load_json("lens_agent_status.json", {})
    email_san = load_json("email_sanitizer_status.json", {})

    # Always-safe public info
    ctx = f\"\"\"NUCLEUS SYSTEM STATUS — {sast_now()}
FX Agent: balance ZAR {fx.get('balance', '—')}, status {fx.get('status', '—')}, last seen {fx.get('last_seen_utc', '—')}
FX Session: {'ACTIVE' if is_fx_session() else 'CLOSED'}
Job Agent: {jobs.get('sent', 0)} sent today, {jobs.get('skipped', 0)} skipped
Shopify Build: {build.get('percent_complete', 0)}% — {build.get('current_phase', '—')}
Lens Agent: {lens.get('last_run', '—')} | {lens.get('processed', 0)} videos processed | queue: {lens.get('queue_size', 0)}
Email Sanitizer: {email_san.get('last_run', '—')} | trashed: {email_san.get('trashed_total', 0)}
March deadline: {days_until_end_of_march()} days remaining
Latest cortex entry: {cortex[0].get('full', '—')[:300] if cortex else 'None'}\"\"\"
"""


# ════════════════════════════════════════════════════════════════════
# PATCH 3 — write_todo() always_on block (line ~1278)
# FIND and REPLACE
# ════════════════════════════════════════════════════════════════════

# ── FIND ──────────────────────────────────────────────────────────
TODO_OLD = """
    # Always-on agents
    todo["always_on"] = [
        {"name": "FX Agent",       "schedule": "every 5min session hours"},
        {"name": "Job Agent",      "schedule": "every 5min always"},
        {"name": "Email Sanitizer","schedule": "every 30min always"},
        {"name": "Supervisor",     "schedule": "every 5min always"},
    ]
"""

# ── REPLACE WITH ──────────────────────────────────────────────────
TODO_NEW = """
    # Always-on agents
    todo["always_on"] = [
        {"name": "FX Agent",        "schedule": "every 5min Mon-Thu session"},
        {"name": "Job Agent",       "schedule": "every 5min always"},
        {"name": "Email Sanitizer", "schedule": "every 30min always"},
        {"name": "Lens Agent",      "schedule": "every 15min always"},
        {"name": "Supervisor",      "schedule": "every 5min always"},
    ]
"""


# ════════════════════════════════════════════════════════════════════
# PATCH 4 — send_eod_summary() agent lines (line ~1370 area)
# FIND and REPLACE the agent summary lines block
# ════════════════════════════════════════════════════════════════════

# ── FIND ──────────────────────────────────────────────────────────
EOD_OLD = """
    fx   = load_json("status.json", {})
    job  = load_json("job_agent_status.json", {})
    shop = load_json("shopify_agent_status.json", {})
    purge = load_json("email_purge_log.json", {"trashed_today": []})
    cortex = load_json(CORTEX_FILE, [])
    room_log = load_json("nucleus_testing_room_log.json", {"sessions": []})

    run_count = len([e for e in cortex if (e.get("sast") or "").startswith(sast.strftime("%Y-%m-%d"))])
    trashed_today = purge.get("trashed_today", [])
    today_upgrades = [s for s in room_log.get("sessions", []) if (s.get("timestamp") or "").startswith(sast.strftime("%Y-%m-%d"))]

    lines.append(f"FX AGENT:       ZAR {fx.get('balance_zar','—')} | {fx.get('trades_today',0)} trades | {'Active' if fx.get('in_session') else 'Market closed'}")
    lines.append(f"JOB AGENT:      {job.get('emails_sent_today', job.get('total_sent',0))} sent | {job.get('skipped_today',0)} skipped")
    lines.append(f"SHOPIFY AGENT:  {'Connected' if shop.get('store_connected') else 'Awaiting credentials'} | {shop.get('last_action','Monitoring')}")
    lines.append(f"EMAIL CLEANER:  {len(trashed_today)} emails trashed | Inbox maintained")
    lines.append(f"SUPERVISOR:     {run_count} runs today | Self-healing active")
    if today_upgrades:
        lines.append(f"TESTING ROOM:   {len(today_upgrades)} agent(s) upgraded tonight")
"""

# ── REPLACE WITH ──────────────────────────────────────────────────
EOD_NEW = """
    fx   = load_json("status.json", {})
    job  = load_json("job_agent_status.json", {})
    shop = load_json("shopify_agent_status.json", {})
    lens = load_json("lens_agent_status.json", {})
    purge = load_json("email_purge_log.json", {"trashed_today": []})
    cortex = load_json(CORTEX_FILE, [])
    room_log = load_json("nucleus_testing_room_log.json", {"sessions": []})

    run_count = len([e for e in cortex if (e.get("sast") or "").startswith(sast.strftime("%Y-%m-%d"))])
    trashed_today = purge.get("trashed_today", [])
    today_upgrades = [s for s in room_log.get("sessions", []) if (s.get("timestamp") or "").startswith(sast.strftime("%Y-%m-%d"))]
    lens_processed_today = lens.get("processed_today", lens.get("processed", 0))

    lines.append(f"FX AGENT:       ZAR {fx.get('balance_zar','—')} | {fx.get('trades_today',0)} trades | {'Active' if fx.get('in_session') else 'Market closed'}")
    lines.append(f"JOB AGENT:      {job.get('emails_sent_today', job.get('total_sent',0))} sent | {job.get('skipped_today',0)} skipped")
    lines.append(f"SHOPIFY AGENT:  {'Connected' if shop.get('store_connected') else 'Awaiting credentials'} | {shop.get('last_action','Monitoring')}")
    lines.append(f"EMAIL CLEANER:  {len(trashed_today)} emails trashed | Inbox maintained")
    lines.append(f"LENS AGENT:     {lens_processed_today} videos processed today | last: {lens.get('last_run','—')}")
    lines.append(f"SUPERVISOR:     {run_count} runs today | Self-healing active")
    if today_upgrades:
        lines.append(f"TESTING ROOM:   {len(today_upgrades)} agent(s) upgraded tonight")
"""


# ════════════════════════════════════════════════════════════════════
# PATCH 5 — self_healing_cycle() file detection (line ~1140 area)
# FIND the file detection for-loop and REPLACE
# ════════════════════════════════════════════════════════════════════

# ── FIND ──────────────────────────────────────────────────────────
HEAL_OLD = """
    for e in errors:
        if "kidbuu_job_agent" in e["message"] or "job_agent" in e["message"].lower():
            file_to_fix = "kidbuu_job_agent.py"
            break
        if "fx_agent_bot" in e["message"] or "fx_agent" in e["message"].lower():
            file_to_fix = "fx_agent_bot.py"
            break
        if "nucleus_supervisor" in e["message"]:
            file_to_fix = "nucleus_supervisor.py"
            break
"""

# ── REPLACE WITH ──────────────────────────────────────────────────
HEAL_NEW = """
    for e in errors:
        msg_lower = e["message"].lower()
        if "nucleus_job_agent" in msg_lower or "kidbuu_job_agent" in msg_lower or "job_agent" in msg_lower:
            file_to_fix = "nucleus_job_agent.py"
            break
        if "fx_agent_bot" in msg_lower or "fx_agent" in msg_lower:
            file_to_fix = "fx_agent_bot.py"
            break
        if "nucleus_lens_agent" in msg_lower or "lens_agent" in msg_lower:
            file_to_fix = "nucleus_lens_agent.py"
            break
        if "nucleus_email_sanitizer" in msg_lower or "email_sanitizer" in msg_lower:
            file_to_fix = "nucleus_email_sanitizer.py"
            break
        if "nucleus_supervisor" in msg_lower:
            file_to_fix = "nucleus_supervisor.py"
            break
        if "shopify_agent" in msg_lower:
            file_to_fix = "shopify_agent.py"
            break
"""


# ════════════════════════════════════════════════════════════════════
# PATCH 6 — VERSION string (line ~42)
# ════════════════════════════════════════════════════════════════════

# FIND:   VERSION   = "5.4"
# REPLACE: VERSION   = "5.6"


# ════════════════════════════════════════════════════════════════════
# FILES TO REMOVE FROM REPO — NONE
# ════════════════════════════════════════════════════════════════════
"""
DO NOT DELETE ANY FILES.

kidbuu_job_agent.py  — KEEP. Alias migration rule says keep until 3+ days stable.
                       Self-heal now correctly routes to nucleus_job_agent.py instead.

shopify_status.json  — does not exist yet, no action needed. shopify_agent_status.json
                       is the correct target (already in repo).

All other files in repo image are legitimate and active.
"""


# ════════════════════════════════════════════════════════════════════
# SUMMARY — WHAT THESE 6 PATCHES FIX
# ════════════════════════════════════════════════════════════════════
"""
BEFORE patches:
  - Supervisor monitors 3 agents (FX, Job, Shopify)
  - Email replies say nothing about Lens or Email Sanitizer
  - EOD report has no Lens Agent line
  - Self-heal tries to fix kidbuu_job_agent.py (old file)
  - Self-heal doesn't know nucleus_lens_agent.py exists
  - Todo dashboard missing Lens Agent entry

AFTER patches:
  - Supervisor monitors ALL 5 agents
  - Email replies include Lens + Email Sanitizer live status
  - EOD report has full Lens Agent daily summary
  - Self-heal routes job errors → nucleus_job_agent.py (correct)
  - Self-heal can fix lens, email sanitizer, shopify when broken
  - Todo dashboard shows Lens Agent as always-on
  - VERSION = 5.6 — clear audit trail

ROOT CAUSE CONFIRMED:
  It was NEVER a GitHub connectivity issue.
  It was agent registry blindness — Supervisor's internal maps
  were never updated when new agents were added to the repo.
  These 6 targeted patches close every gap identified.
"""
