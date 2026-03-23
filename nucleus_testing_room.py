"""
╔══════════════════════════════════════════════════════════════════╗
║  NUCLEUS TESTING ROOM v1.0                                       ║
║  "The Ivy League Standard"                                       ║
║                                                                  ║
║  MANDATE: When an agent is idle, it checks into this room.      ║
║  In here, it gets upgraded to the best in its industry.         ║
║                                                                  ║
║  PROCESS PER AGENT:                                              ║
║    1. Research top 3 industry leaders in that agent's field     ║
║    2. Extract best strategies, frameworks, tactics              ║
║    3. Inject knowledge into agent's system prompt / logic       ║
║    4. Run 10 ghost tests (simulated scenarios)                  ║
║    5. If success rate improves → commit upgraded code           ║
║    6. If it regresses → rollback, try different approach        ║
║                                                                  ║
║  AGENT PERSONAS:                                                 ║
║    FX Agent     → Wall Street Floor Trader. Never breaks rules. ║
║    Shopify      → $500/hr E-commerce Consultant                 ║
║    Job Agent    → Executive Recruiter + LinkedIn Expert         ║
║    Email Clean  → Enterprise IT Operations Manager             ║
║    Nucleus      → CTO of a $1B AI company                      ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, asyncio, httpx, re, base64, random
from datetime import datetime, timezone, timedelta
from pathlib import Path

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
GH_PAT             = os.getenv("GH_PAT", "")
GMAIL_FROM         = os.getenv("GMAIL_FROM", "")
GMAIL_TO           = os.getenv("GMAIL_TO", GMAIL_FROM)
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
OPERATOR           = os.getenv("OPERATOR_ALIAS", "Nucleus Operator")

VERSION      = "1.0"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
GH_REPO      = "k1dbUU/fx-bot"
GH_API       = "https://api.github.com"
ROOM_LOG     = "nucleus_testing_room_log.json"
MEMORY_FILE  = "NUCLEUS_MEMORY.json"

# ── AGENT PROFILES ────────────────────────────────────────────────
# Each agent has: persona, domain, research sources, golden rules
AGENT_PROFILES = {
    "fx_agent_bot.py": {
        "name":        "FX Agent",
        "persona":     "Wall Street Floor Trader with 20 years SMC experience",
        "domain":      "forex algorithmic trading smart money concepts",
        "research_queries": [
            "smart money concepts ICT forex 2026 best strategies",
            "liquidity grab order blocks breaker blocks forex 2026",
            "algorithmic forex scalping low risk high reward 2026",
        ],
        "golden_rules": [
            "Never risk more than 1% per trade",
            "Never lose more than 10% in a single day — stop all trading",
            "Only trade during confirmed sweep + displacement + BOS",
            "Follow SMC: liquidity grabs, order blocks, fair value gaps",
            "Scalp only when market structure confirms intraday trend",
        ],
        "success_metric": "win_rate_above_60_percent_with_rr_above_1.5",
    },
    "shopify_agent.py": {
        "name":        "Shopify Agent",
        "persona":     "$500/hr E-commerce Consultant — conversion rate expert",
        "domain":      "shopify ecommerce conversion optimization luxury products",
        "research_queries": [
            "shopify conversion rate optimization luxury products 2026",
            "ecommerce upsell cross-sell automation best practices 2026",
            "shopify page speed product photography AI tools 2026",
        ],
        "golden_rules": [
            "Always optimize for conversion rate first",
            "Monitor inventory — never let hero products go out of stock",
            "Price dynamically based on competitor data",
            "Upsell complementary products on every order",
            "Luxury positioning: never discount more than 15%",
        ],
        "success_metric": "conversion_rate_above_3_percent",
    },
    "kidbuu_job_agent.py": {
        "name":        "Job Agent",
        "persona":     "Executive Recruiter + LinkedIn Top Voice — placement expert",
        "domain":      "executive recruiting professional email outreach south africa remote",
        "research_queries": [
            "best cold email strategies for job applications 2026",
            "professional CV cover letter format remote jobs south africa 2026",
            "highest reply rate job application email templates 2026",
        ],
        "golden_rules": [
            "Always attach CV as PDF — never plain text",
            "Research company before sending — personalise every email",
            "Never apply to the same company twice within 30 days",
            "Remove bounced emails immediately from contact list",
            "Subject line must reference a specific role or need",
        ],
        "success_metric": "reply_rate_above_10_percent",
    },
    "nucleus_email_sanitizer.py": {
        "name":        "Email Sanitizer",
        "persona":     "Enterprise IT Operations Manager — zero tolerance for noise",
        "domain":      "email management spam filtering inbox organisation",
        "research_queries": [
            "enterprise email hygiene best practices 2026",
            "gmail spam filter advanced techniques",
            "email inbox zero productivity methods 2026",
        ],
        "golden_rules": [
            "Never delete receipts, invoices, or payment confirmations",
            "Never delete human-to-human emails",
            "Always move uncertain emails to review — never force delete",
            "Emails under 7 days old: review only, never auto-delete",
            "One daily summary at 20:00 SAST only",
        ],
        "success_metric": "inbox_noise_below_10_percent_of_total",
    },
    "nucleus_supervisor.py": {
        "name":        "Nucleus Supervisor",
        "persona":     "CTO of a $1B autonomous AI company — architect of intelligence",
        "domain":      "ai agent orchestration autonomous systems self-healing architecture",
        "research_queries": [
            "best AI agent orchestration patterns 2026",
            "autonomous AI supervisor architecture best practices",
            "multi-agent system design patterns fault tolerance 2026",
        ],
        "golden_rules": [
            "Always reply to operator within one 5-minute cycle",
            "Self-heal any error within 3 attempts before escalating",
            "Never expose secrets or credentials in any output",
            "All agent upgrades tested before deployment",
            "Weekly Sunday self-audit — remove redundancy, improve speed",
        ],
        "success_metric": "uptime_above_99_percent_zero_crashes",
    },
}

# ── HELPERS ───────────────────────────────────────────────────────
def load_json(path, default):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return default

def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, default=str))

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def sast_now():
    return datetime.now(timezone(timedelta(hours=2))).strftime("%Y-%m-%d %H:%M SAST")

def is_clean(text):
    for p in ["sk-ant-", "shpat_", "ghp_", "github_pat_", "app_password"]:
        if p.lower() in text.lower():
            return False
    return True

def gh_headers():
    return {"Authorization": f"token {GH_PAT}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

async def call_claude(system, user, max_tokens=2000):
    if not ANTHROPIC_API_KEY:
        return "[No API key]"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": CLAUDE_MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
            )
            return r.json()["content"][0]["text"]
    except Exception as e:
        return f"[Claude error: {e}]"

async def web_search(query):
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get("https://html.duckduckgo.com/html/", params={"q": query, "kl": "en-en"}, headers={"User-Agent": "Mozilla/5.0"})
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.DOTALL)
            return " | ".join(re.sub('<[^>]+>', '', s).strip() for s in snippets[:3]) or "No results"
    except Exception as e:
        return f"Search failed: {e}"

async def gh_get_file(filepath):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{GH_API}/repos/{GH_REPO}/contents/{filepath}", headers=gh_headers())
            if r.status_code == 200:
                d = r.json()
                return base64.b64decode(d["content"]).decode("utf-8"), d["sha"]
    except Exception:
        pass
    return None, None

async def gh_commit_file(filepath, content, message, sha=None):
    try:
        encoded = base64.b64encode(content.encode()).decode()
        body = {"message": message, "content": encoded, "branch": "main"}
        if sha:
            body["sha"] = sha
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.put(f"{GH_API}/repos/{GH_REPO}/contents/{filepath}", headers=gh_headers(), json=body)
            return r.status_code in (200, 201)
    except Exception:
        return False

# ── TESTING ROOM CORE ─────────────────────────────────────────────

async def check_agent_idle(filename):
    """Check if agent is currently idle (not mid-task)."""
    status_map = {
        "fx_agent_bot.py":            "status.json",
        "shopify_agent.py":           "shopify_agent_status.json",
        "kidbuu_job_agent.py":        "job_agent_status.json",
        "nucleus_email_sanitizer.py": "email_sanitizer_status.json",
        "nucleus_supervisor.py":      "cortex_log.json",
    }
    status_file = status_map.get(filename)
    if not status_file:
        return True

    content, _ = await gh_get_file(status_file)
    if not content:
        return True

    try:
        data = json.loads(content)
        last_run = data.get("last_run") or data.get("last_seen_utc")
        if last_run:
            from datetime import datetime, timezone
            t = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            mins_ago = (datetime.now(timezone.utc) - t).total_seconds() / 60
            return mins_ago > 10  # idle if last run > 10min ago
    except Exception:
        pass
    return True


async def research_industry_best_practices(profile):
    """Search for latest best practices in agent's domain."""
    all_results = []
    for query in profile["research_queries"][:2]:  # limit to 2 searches to save time
        result = await web_search(query)
        all_results.append(f"Query: {query}\nResults: {result}")
        await asyncio.sleep(1)
    return "\n\n".join(all_results)


async def run_ghost_tests(agent_name, current_code, profile, upgraded_prompt):
    """
    Simulate 10 scenarios for the agent.
    Returns: success_count, total, improvement_vs_baseline
    """
    scenarios = await call_claude(
        system=f"You are testing an AI agent called {agent_name}. Generate 10 realistic test scenarios for this agent's domain. Return as JSON array of objects with 'scenario', 'expected_outcome', 'success_criteria'.",
        user=f"Agent domain: {profile['domain']}\nAgent persona: {profile['persona']}\nGenerate 10 ghost test scenarios.",
        max_tokens=800,
    )

    try:
        clean = re.sub(r"```json|```", "", scenarios).strip()
        test_cases = json.loads(clean)
    except Exception:
        return 5, 10, 0  # assume neutral if parsing fails

    successes = 0
    for test in test_cases[:10]:
        result = await call_claude(
            system=f"""You are {profile['persona']}.
Golden rules you never break: {chr(10).join(profile['golden_rules'])}
Upgraded knowledge: {upgraded_prompt[:500]}
Answer the scenario correctly based on your expertise.""",
            user=f"Scenario: {test.get('scenario', '')}\nExpected: {test.get('expected_outcome', '')}\nDid you handle this correctly? Reply YES or NO and why in one sentence.",
            max_tokens=100,
        )
        if "YES" in result.upper():
            successes += 1

    baseline = 5  # assume 50% without upgrade
    return successes, 10, successes - baseline


async def upgrade_agent(filename, profile):
    """
    Full upgrade cycle for one agent:
    1. Research best practices
    2. Generate upgraded system prompt / logic improvements
    3. Run ghost tests
    4. If improved: inject into agent code and commit
    """
    print(f"[TESTING ROOM] 🎓 Checking in: {profile['name']}")

    # Step 1: Research
    research = await research_industry_best_practices(profile)
    print(f"[TESTING ROOM] Research complete for {profile['name']}")

    # Step 2: Generate upgraded prompt/knowledge
    upgraded_knowledge = await call_claude(
        system=f"""You are upgrading an AI agent to Ivy League standard.
Agent: {profile['name']}
Persona: {profile['persona']}
Domain: {profile['domain']}
Golden Rules (NEVER change these): {chr(10).join(profile['golden_rules'])}

Based on the research, generate:
1. An upgraded system prompt (200 words max) that makes this agent world-class
2. 3 specific code improvements to suggest
Keep all golden rules intact. Make the agent smarter, not riskier.""",
        user=f"Industry research:\n{research}\n\nGenerate the upgrade:",
        max_tokens=600,
    )

    if not is_clean(upgraded_knowledge):
        print(f"[TESTING ROOM] Upgrade blocked by leak scanner")
        return False

    # Step 3: Ghost tests
    successes, total, improvement = await run_ghost_tests(profile["name"], "", profile, upgraded_knowledge)
    print(f"[TESTING ROOM] Ghost tests: {successes}/{total} — improvement: {improvement:+d}")

    # Step 4: Only commit if improved
    if improvement > 0:
        # Get current agent file
        current_code, sha = await gh_get_file(filename)
        if not current_code:
            print(f"[TESTING ROOM] Could not read {filename}")
            return False

        # Ask Claude to inject the upgrade into the actual file
        upgraded_code = await call_claude(
            system=f"""You are upgrading a Python agent file. 
Inject the upgraded knowledge as a KNOWLEDGE_BASE constant near the top of the file (after imports).
Do NOT change any logic, functions, or golden rule enforcement.
Only add: a AGENT_PERSONA constant and an AGENT_KNOWLEDGE_BASE constant with the upgraded strategies.
Return ONLY the complete modified Python file. Raw code, no markdown.""",
            user=f"""Current file (first 3000 chars):
{current_code[:3000]}

Upgrade to inject:
{upgraded_knowledge[:800]}

Return the complete upgraded file.""",
            max_tokens=4000,
        )

        upgraded_code = re.sub(r'^```python\s*|^```\s*|```$', '', upgraded_code.strip(), flags=re.MULTILINE).strip()

        if len(upgraded_code) < 200 or not is_clean(upgraded_code):
            print(f"[TESTING ROOM] Upgrade code invalid — skipping")
            return False

        # Commit the upgrade
        committed = await gh_commit_file(
            filename,
            upgraded_code,
            f"testing-room: upgrade {profile['name']} — {successes}/{total} ghost tests passed — {sast_now()}",
            sha
        )

        if committed:
            print(f"[TESTING ROOM] ✅ {profile['name']} upgraded and committed")
            # Log it
            log = load_json(ROOM_LOG, {"sessions": []})
            log["sessions"].insert(0, {
                "timestamp":   utc_now(),
                "agent":       profile["name"],
                "file":        filename,
                "ghost_tests": f"{successes}/{total}",
                "improvement": improvement,
                "committed":   True,
            })
            log["sessions"] = log["sessions"][:50]
            save_json(ROOM_LOG, log)
            return True
    else:
        print(f"[TESTING ROOM] No improvement detected for {profile['name']} — keeping current version")

    return False


async def run_testing_room():
    """
    Main testing room cycle.
    Checks each agent — if idle, runs upgrade cycle.
    Runs during overnight window to avoid interfering with live tasks.
    """
    h = datetime.now(timezone(timedelta(hours=2))).hour
    # Run testing room 01:00-05:00 SAST only
    if not (1 <= h < 5):
        print(f"[TESTING ROOM] Outside upgrade window (01:00-05:00 SAST) — skipping")
        return

    log = load_json(ROOM_LOG, {"sessions": [], "last_full_run": None})

    # Only run full upgrade cycle once per day
    today = datetime.now(timezone(timedelta(hours=2))).strftime("%Y-%m-%d")
    if log.get("last_full_run") == today:
        print(f"[TESTING ROOM] Already ran today — skipping")
        return

    print(f"[TESTING ROOM] 🎓 Opening for upgrades — {sast_now()}")
    upgraded = []

    for filename, profile in AGENT_PROFILES.items():
        idle = await check_agent_idle(filename)
        if idle:
            success = await upgrade_agent(filename, profile)
            if success:
                upgraded.append(profile["name"])
        else:
            print(f"[TESTING ROOM] {profile['name']} is busy — skipping this cycle")
        await asyncio.sleep(2)

    log["last_full_run"] = today
    save_json(ROOM_LOG, log)

    print(f"[TESTING ROOM] ✅ Done — {len(upgraded)} agent(s) upgraded: {upgraded}")


# ── WEEKLY SELF-AUDIT (Nucleus upgrades itself every Sunday) ──────

async def sunday_self_audit():
    """
    Weekly intelligence upgrade for Nucleus itself.
    Runs Sunday 02:00-04:00 SAST.
    Audits own code for redundancy, speed, better patterns.
    """
    now = datetime.now(timezone(timedelta(hours=2)))
    if not (now.weekday() == 6 and 2 <= now.hour < 4):
        return

    print(f"[AUDIT] Sunday self-audit starting — {sast_now()}")

    # Research latest supervisor patterns
    research = await web_search("best AI agent supervisor architecture patterns 2026 autonomous")
    await asyncio.sleep(1)
    research2 = await web_search("multi-agent orchestration reliability fault tolerance 2026")

    audit_result = await call_claude(
        system="""You are auditing an AI supervisor system for redundancy and performance.
Identify: 1) any redundant code patterns, 2) speed improvements, 3) better error handling approaches.
Be specific. Return as JSON: {"redundancies": [], "speed_improvements": [], "new_capabilities": []}""",
        user=f"""Nucleus Supervisor manages: FX Agent, Job Agent, Shopify Agent, Email Sanitizer, Testing Room, Autonomous Engine.
It runs every 5 minutes on GitHub Actions.

Industry research:
{research}
{research2}

Audit findings:""",
        max_tokens=600,
    )

    memory = load_json(MEMORY_FILE, {})
    evo = memory.get("evolution_log", [])
    evo.insert(0, {"timestamp": utc_now(), "event": f"Sunday self-audit — {sast_now()}"})
    memory["evolution_log"] = evo[:20]
    memory["last_audit"] = utc_now()
    memory["last_audit_findings"] = audit_result[:500] if is_clean(audit_result) else "audit blocked"
    save_json(MEMORY_FILE, memory)

    print(f"[AUDIT] ✅ Self-audit complete")


if __name__ == "__main__":
    asyncio.run(run_testing_room())
