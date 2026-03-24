"""
╔══════════════════════════════════════════════════════════════════╗
║  NUCLEUS AUTONOMOUS ENGINE v1.1                                  ║
║  v1.1 CHANGES:                                                   ║
║    - handle_operator_command() added — called by Supervisor     ║
║      when operator emails a build/create/make request           ║
║    - parse_intent() added — detects build vs query vs command   ║
║    - AGENT_BLUEPRINTS expanded with all known agent types       ║
║    - build_new_agent_from_brief() unchanged — already working   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, asyncio, httpx, smtplib, re, base64, random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.mime.text import MIMEText

# ── SECRETS ───────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
GMAIL_FROM         = os.getenv("GMAIL_FROM", "")
GMAIL_TO           = os.getenv("GMAIL_TO", GMAIL_FROM)
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GH_PAT             = os.getenv("GH_PAT", "")
OPERATOR           = os.getenv("OPERATOR_ALIAS", "Nucleus Operator")

VERSION      = "1.1"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
GH_REPO      = "k1dbUU/fx-bot"
GH_API       = "https://api.github.com"

# ── FILE PATHS ────────────────────────────────────────────────────
MEMORY_FILE      = "NUCLEUS_MEMORY.json"
ENGINE_LOG_FILE  = "nucleus_engine_log.json"
SHOPIFY_BUILD    = "shopify_build_log.json"
WORKFLOW_FILE    = ".github/workflows/fx_agent_workflow.yml"
LEARNING_LOG     = "nucleus_learning_log.json"

# ── PRIME DIRECTIVE ───────────────────────────────────────────────
PRIME_DIRECTIVE = """
NUCLEUS PRIME DIRECTIVE:
You are an autonomous AI operating system. Your default is to ACT, not to ask.
- If you can solve it → solve it
- If you need a credential → email the operator with exact instructions
- If it requires a human decision with real consequences → email asking for approval
- Never say "I cannot" if the only blocker is missing information you can request
- Always find a path forward
"""

# ── CURIOSITY TOPICS ──────────────────────────────────────────────
CURIOSITY_DOMAINS = [
    "best free Python AI agent frameworks 2026",
    "Claude API new features capabilities 2026",
    "async Python performance GitHub Actions",
    "ICT smart money concepts forex 2026 updates",
    "gold XAUUSD algorithmic trading edge 2026",
    "remote work south africa opportunities 2026",
    "best entry level remote jobs south africa 2026",
    "Shopify API automation Python 2026",
    "luxury pet furniture market south africa 2026",
    "profitable AI agent business models 2026",
    "AI automation services small business 2026",
    "email deliverability best practices 2026",
    "GitHub Actions free tier optimisation tricks",
    "Python IMAP Gmail automation improvements",
    "autonomous agent design patterns 2026",
]

# ── AGENT BLUEPRINTS ──────────────────────────────────────────────
AGENT_BLUEPRINTS = {
    "shopify": {
        "name":        "Shopify Store Manager",
        "build_log":   "shopify_build_log.json",
        "output_file": "shopify_agent.py",
        "status_file": "shopify_agent_status.json",
        "secrets_needed": ["SHOPIFY_TOKEN", "SHOPIFY_STORE_URL"],
        "schedule":    "*/30 * * * *",
        "timeout":     15,
    },
    "lead_gen": {
        "name":        "Lead Generation Agent",
        "output_file": "nucleus_lead_agent.py",
        "status_file": "lead_agent_status.json",
        "secrets_needed": [],
        "schedule":    "0 */4 * * *",
        "timeout":     20,
    },
    "email_manager": {
        "name":        "Email Manager Agent",
        "output_file": "nucleus_email_manager.py",
        "status_file": "email_manager_status.json",
        "secrets_needed": [],
        "schedule":    "*/15 * * * *",
        "timeout":     10,
    },
    "report": {
        "name":        "Client Report Agent",
        "output_file": "nucleus_report_agent.py",
        "status_file": "report_agent_status.json",
        "secrets_needed": [],
        "schedule":    "0 8 * * 1",
        "timeout":     15,
    },
}

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

def is_overnight() -> bool:
    h = datetime.now(timezone(timedelta(hours=2))).hour
    return 0 <= h < 6

def is_clean(text: str) -> bool:
    for p in ["sk-ant-", "shpat_", "ghp_", "github_pat_", "app_password"]:
        if p.lower() in text.lower():
            return False
    return True

def gh_headers() -> dict:
    return {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

# ── CLAUDE API ────────────────────────────────────────────────────
async def call_claude(system: str, user: str, max_tokens: int = 2000) -> str:
    if not ANTHROPIC_API_KEY:
        return "[No API key]"
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
                    "system": PRIME_DIRECTIVE + "\n\n" + system,
                    "messages": [{"role": "user", "content": user}],
                }
            )
            return r.json()["content"][0]["text"]
    except Exception as e:
        return f"[Claude error: {e}]"

# ── EMAIL ─────────────────────────────────────────────────────────
def send_email(subject: str, body: str):
    if not all([GMAIL_FROM, GMAIL_APP_PASSWORD, GMAIL_TO]):
        return
    if not is_clean(body):
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = f"[Nucleus] {subject}"
        msg["From"]    = f"Nucleus Engine <{GMAIL_FROM}>"
        msg["To"]      = GMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        print(f"[ENGINE] Email: {subject}")
    except Exception as e:
        print(f"[ENGINE] Email failed: {e}")

# ── GITHUB API ────────────────────────────────────────────────────
async def gh_get_file(filepath: str):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{GH_API}/repos/{GH_REPO}/contents/{filepath}",
                headers=gh_headers()
            )
            if r.status_code == 200:
                d = r.json()
                content = base64.b64decode(d["content"]).decode("utf-8")
                return content, d["sha"]
    except Exception as e:
        print(f"[ENGINE] gh_get_file failed: {e}")
    return None, None

async def gh_commit_file(filepath: str, content: str, message: str, sha: str = None) -> bool:
    try:
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        body = {"message": message, "content": encoded, "branch": "main"}
        if sha:
            body["sha"] = sha
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.put(
                f"{GH_API}/repos/{GH_REPO}/contents/{filepath}",
                headers=gh_headers(),
                json=body
            )
            if r.status_code in (200, 201):
                print(f"[ENGINE] ✅ Committed: {filepath}")
                return True
            print(f"[ENGINE] Commit failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[ENGINE] gh_commit_file error: {e}")
    return False

async def gh_trigger_workflow() -> bool:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{GH_API}/repos/{GH_REPO}/actions/workflows/fx_agent_workflow.yml/dispatches",
                headers=gh_headers(),
                json={"ref": "main"}
            )
            return r.status_code == 204
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════
# NEW v1.1 — EMAIL INTENT PARSER + COMMAND HANDLER
# Called by nucleus_supervisor.py handle_inbound_email()
# ══════════════════════════════════════════════════════════════════

def parse_intent(subject: str, body: str) -> dict:
    """
    Reads email subject + body and returns intent dict.
    intent types: build_agent | fix_agent | query | command | unknown
    """
    text = (subject + " " + body).lower()

    build_keywords  = ["build", "create", "make", "new agent", "add agent", "deploy", "i want an agent", "create me"]
    fix_keywords    = ["fix", "broken", "not working", "error", "crashed", "repair"]
    pause_keywords  = ["pause", "stop", "disable", "turn off"]
    resume_keywords = ["resume", "start", "enable", "turn on", "activate"]

    if any(k in text for k in build_keywords):
        # Extract the brief — everything after the trigger word
        brief = body.strip() if body.strip() else subject.strip()
        agent_id = re.sub(r"[^a-z0-9_]", "_", brief[:30].lower().strip())
        return {"type": "build_agent", "brief": brief, "agent_id": agent_id}

    if any(k in text for k in fix_keywords):
        return {"type": "fix_agent", "body": body}

    if any(k in text for k in pause_keywords):
        return {"type": "command", "action": "pause", "body": body}

    if any(k in text for k in resume_keywords):
        return {"type": "command", "action": "resume", "body": body}

    return {"type": "query"}


async def handle_operator_command(sender: str, subject: str, body: str) -> str:
    """
    Called by Supervisor when an email from a TRUSTED address
    contains an actionable command (not just a status query).

    Returns a reply string to send back to operator.
    """
    intent = parse_intent(subject, body)
    print(f"[ENGINE] Intent detected: {intent['type']} | from: {sender}")

    # ── BUILD AGENT ────────────────────────────────────────────────
    if intent["type"] == "build_agent":
        brief    = intent["brief"]
        agent_id = intent["agent_id"]

        # Check if already exists
        existing_file = f"nucleus_{agent_id}_agent.py"
        existing, _ = await gh_get_file(existing_file)
        if existing:
            return (
                f"Agent '{agent_id}' already exists in your repo ({existing_file}).\n"
                f"Email me 'fix {agent_id}' if it needs repairs, or give me a different name.\n"
                f"— Nucleus Engine v{VERSION}"
            )

        # Fire and forget — build runs async, operator gets email when done
        asyncio.create_task(build_new_agent_from_brief(brief, agent_id))

        return (
            f"Got it. Building: {brief[:80]}\n\n"
            f"I'm designing and writing the agent now.\n"
            f"You'll get a separate email when it's live in your repo with:\n"
            f"  • The file name\n"
            f"  • The schedule it runs on\n"
            f"  • Any GitHub Secrets you need to add\n\n"
            f"No action needed from you until that email arrives.\n"
            f"— Nucleus Engine v{VERSION}"
        )

    # ── FIX AGENT ─────────────────────────────────────────────────
    if intent["type"] == "fix_agent":
        return (
            f"Fix request received. The self-healing engine will pick this up on the next run.\n"
            f"If you see a specific error, forward the GitHub failure email to trigger instant repair.\n"
            f"— Nucleus Engine v{VERSION}"
        )

    # ── PAUSE/RESUME ──────────────────────────────────────────────
    if intent["type"] == "command":
        action = intent.get("action")
        return (
            f"Command '{action}' noted. Manual workflow control requires GitHub Actions UI.\n"
            f"Go to: github.com/k1dbUU/fx-bot/actions → select workflow → disable/enable.\n"
            f"— Nucleus Engine v{VERSION}"
        )

    # ── NOT A COMMAND — let supervisor handle as normal query ──────
    return None


# ══════════════════════════════════════════════════════════════════
# 1. AGENT ASSEMBLER
# ══════════════════════════════════════════════════════════════════

async def assemble_and_deploy_agent(agent_key: str) -> bool:
    blueprint = AGENT_BLUEPRINTS.get(agent_key)
    if not blueprint:
        print(f"[ASSEMBLE] Unknown agent: {agent_key}")
        return False

    build_log_path = blueprint.get("build_log")
    if not build_log_path:
        print(f"[ASSEMBLE] No build_log defined for {agent_key}")
        return False

    build_content, _ = await gh_get_file(build_log_path)
    if not build_content:
        print(f"[ASSEMBLE] Could not read {build_log_path}")
        return False

    build = json.loads(build_content)
    pct   = build.get("percent_complete", 0)
    code  = build.get("shopify_agent_code", {})

    if pct < 100:
        print(f"[ASSEMBLE] {agent_key} only {pct}% complete — skipping")
        return False

    existing, existing_sha = await gh_get_file(blueprint["output_file"])
    if existing and "assembled by nucleus" in existing.lower():
        print(f"[ASSEMBLE] {blueprint['output_file']} already assembled")
        return True

    print(f"[ASSEMBLE] Assembling {blueprint['name']} — {len(code)} phases")

    phases = []
    for i in range(1, 20):
        body = code.get(f"phase_{i}", "")
        if body and "[No ANTHROPIC_API_KEY]" not in body and len(body) > 50:
            phases.append(body.strip())

    if not phases:
        print(f"[ASSEMBLE] No valid phase code found for {agent_key}")
        return False

    phases_raw = "\n\n# --- NEXT PHASE ---\n\n".join(phases)
    assembled = await call_claude(
        system=f"""You are assembling a Python agent from phase fragments.
Combine all phases into one clean, working Python file.
The agent name is: {blueprint['name']}
Output file will be: {blueprint['output_file']}
Rules:
- Remove duplicate imports
- Remove duplicate functions — keep most complete version
- Add async main() at the bottom
- Add status writer to {blueprint['status_file']}
- Secrets from os.getenv(): {', '.join(blueprint['secrets_needed'])}
- Output ONLY raw Python — no markdown
- First line: # Assembled by Nucleus Engine {utc_now()}""",
        user=f"Assemble:\n\n{phases_raw[:8000]}",
        max_tokens=4000,
    )

    assembled = re.sub(r'^```python\s*|^```\s*|```$', '', assembled.strip(), flags=re.MULTILINE).strip()

    if not assembled or len(assembled) < 200 or not is_clean(assembled):
        print(f"[ASSEMBLE] Assembly failed")
        return False

    sha = existing_sha if existing else None
    committed = await gh_commit_file(
        blueprint["output_file"],
        assembled,
        f"engine: assemble {blueprint['name']} — {sast_now()}",
        sha
    )

    if committed:
        await inject_agent_into_workflow(agent_key, blueprint)
        send_email(
            f"✅ {blueprint['name']} assembled and deployed",
            f"FILE: {blueprint['output_file']}\nTIME: {sast_now()}\n"
            f"{'Secrets needed: ' + ', '.join(blueprint['secrets_needed']) if blueprint['secrets_needed'] else 'No secrets needed — fully operational.'}\n"
            f"— Nucleus Engine v{VERSION}"
        )
        return True

    return False


# ══════════════════════════════════════════════════════════════════
# 2. WORKFLOW INJECTOR
# ══════════════════════════════════════════════════════════════════

async def inject_agent_into_workflow(agent_key: str, blueprint: dict):
    workflow_content, sha = await gh_get_file(WORKFLOW_FILE)
    if not workflow_content:
        return False

    job_name = agent_key.replace("_", "-")

    if f"name: {blueprint['name']}" in workflow_content or job_name + ":" in workflow_content:
        print(f"[WORKFLOW] {blueprint['name']} already in workflow")
        return True

    new_cron = f"    - cron: \"{blueprint['schedule']}\""
    if blueprint["schedule"] not in workflow_content:
        workflow_content = workflow_content.replace(
            "  workflow_dispatch:",
            f"{new_cron}\n  workflow_dispatch:"
        )

    new_job = f"""
  # ── {blueprint['name'].upper()} ─────────────────────────────────────────
  {job_name}:
    runs-on: ubuntu-latest
    timeout-minutes: {blueprint['timeout']}
    name: {blueprint['name']}
    if: |
      github.event_name == 'workflow_dispatch' ||
      github.event.schedule == '{blueprint["schedule"]}'

    steps:
      - uses: actions/checkout@v4
        with:
          token: ${{{{ secrets.GH_PAT }}}}

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install httpx

      - name: Run {blueprint['name']}
        env:
          ANTHROPIC_API_KEY:  ${{{{ secrets.ANTHROPIC_API_KEY }}}}
          GMAIL_FROM:         ${{{{ secrets.GMAIL_FROM }}}}
          GMAIL_TO:           ${{{{ secrets.GMAIL_TO }}}}
          GMAIL_APP_PASSWORD: ${{{{ secrets.GMAIL_APP_PASSWORD }}}}
          GH_PAT:             ${{{{ secrets.GH_PAT }}}}
          OPERATOR_ALIAS:     Nucleus Operator
        run: python {blueprint['output_file']}

      - name: Commit outputs
        if: always()
        run: |
          git config user.name  "nucleus-bot"
          git config user.email "bot@nucleus.local"
          git add {blueprint['status_file']} || true
          git diff --staged --quiet || git commit -m "{job_name}: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
          git pull --rebase origin main || true
          git push || true
"""

    workflow_content += new_job

    committed = await gh_commit_file(
        WORKFLOW_FILE,
        workflow_content,
        f"engine: add {blueprint['name']} to workflow",
        sha
    )

    if committed:
        print(f"[WORKFLOW] ✅ {blueprint['name']} added to workflow")
    return committed


# ══════════════════════════════════════════════════════════════════
# 3. AGENT FACTORY — builds entirely new agents from scratch
# ══════════════════════════════════════════════════════════════════

async def build_new_agent_from_brief(brief: str, agent_id: str):
    print(f"[FACTORY] Building new agent: {brief[:60]}")

    spec_raw = await call_claude(
        system=f"""You are the Nucleus Agent Factory. Design a profitable, fully autonomous agent.
The agent will run 24/7 on GitHub Actions for free.
It must be completely functional — no placeholders, no TODOs.

Return ONLY a JSON object with these exact keys:
{{
  "name": "Agent name",
  "description": "What it does",
  "revenue_model": "How it makes money",
  "python_file": "filename.py",
  "status_file": "status.json",
  "schedule": "*/30 * * * *",
  "timeout_minutes": 15,
  "secrets_needed": [],
  "phases": ["phase 1 name", "phase 2 name"]
}}""",
        user=f"Design a profitable agent for: {brief}",
        max_tokens=400,
    )

    try:
        spec_clean = re.sub(r"```json|```", "", spec_raw).strip()
        spec = json.loads(spec_clean)
    except Exception:
        spec = {
            "name": f"Custom Agent — {brief[:30]}",
            "python_file": f"nucleus_{agent_id}_agent.py",
            "status_file": f"{agent_id}_agent_status.json",
            "schedule": "0 */6 * * *",
            "timeout_minutes": 15,
            "secrets_needed": [],
            "phases": ["Research", "Core logic", "Output", "Status write"],
        }

    agent_code = await call_claude(
        system="""You are building a complete, production-ready Python agent.
It runs on GitHub Actions (Ubuntu 24, Python 3.11).
100% functional — no placeholders, no TODOs.
Output ONLY raw Python code.""",
        user=f"""Build this agent completely:

Name: {spec.get('name')}
Description: {spec.get('description', brief)}
Revenue model: {spec.get('revenue_model', '—')}
Phases: {spec.get('phases', [])}

Secrets via os.getenv(): ANTHROPIC_API_KEY, GMAIL_FROM, GMAIL_APP_PASSWORD, GH_PAT
Status file: {spec.get('status_file')}

Requirements:
- Async Python, httpx, asyncio
- Writes status to {spec.get('status_file')} after every run
- Sends email report on completion
- Full error handling
- if __name__ == '__main__': asyncio.run(main())""",
        max_tokens=4000,
    )

    agent_code = re.sub(r'^```python\s*|^```\s*|```$', '', agent_code.strip(), flags=re.MULTILINE).strip()

    if not agent_code or len(agent_code) < 200 or not is_clean(agent_code):
        print(f"[FACTORY] Agent code generation failed")
        send_email(
            f"⚠ Agent build failed: {brief[:40]}",
            f"The build for '{brief[:60]}' failed at code generation.\nRetrying next cycle.\n— Nucleus Engine v{VERSION}"
        )
        return False

    python_file = spec.get("python_file", f"nucleus_{agent_id}_agent.py")
    existing, sha = await gh_get_file(python_file)
    committed = await gh_commit_file(
        python_file,
        agent_code,
        f"factory: new agent '{spec.get('name')}' — {sast_now()}",
        sha
    )

    if not committed:
        return False

    blueprint = {
        "name":        spec.get("name"),
        "output_file": python_file,
        "status_file": spec.get("status_file", f"{agent_id}_status.json"),
        "secrets_needed": spec.get("secrets_needed", []),
        "schedule":    spec.get("schedule", "0 */6 * * *"),
        "timeout":     spec.get("timeout_minutes", 15),
    }
    await inject_agent_into_workflow(agent_id, blueprint)

    memory = load_json(MEMORY_FILE, {})
    agents_built = memory.get("agents_built", [])
    agents_built.append({
        "id":       agent_id,
        "name":     spec.get("name"),
        "file":     python_file,
        "built_at": utc_now(),
        "brief":    brief,
    })
    memory["agents_built"] = agents_built
    save_json(MEMORY_FILE, memory)

    await gh_trigger_workflow()

    send_email(
        f"🤖 New agent built: {spec.get('name')}",
        f"""NUCLEUS AGENT FACTORY — NEW AGENT DEPLOYED
{'='*50}
AGENT:    {spec.get('name')}
FILE:     {python_file}
SCHEDULE: {spec.get('schedule')}
TIME:     {sast_now()}

WHAT IT DOES:
{spec.get('description', brief)}

REVENUE MODEL:
{spec.get('revenue_model', '—')}

{'SECRETS NEEDED — add to GitHub Secrets:' + chr(10) + chr(10).join('  • ' + s for s in spec.get('secrets_needed', [])) if spec.get('secrets_needed') else 'No additional secrets needed — agent is fully operational.'}

The agent is live in your repo and running on the next cycle.

— Nucleus Engine v{VERSION}"""
    )

    print(f"[FACTORY] ✅ {spec.get('name')} built, deployed, workflow updated")
    return True


# ══════════════════════════════════════════════════════════════════
# 4. SELF-LEARNING
# ══════════════════════════════════════════════════════════════════

async def web_search(query: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query, "kl": "en-en"},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.DOTALL)
            titles   = re.findall(r'class="result__a"[^>]*>(.*?)</a>', r.text, re.DOTALL)
            results  = []
            for t, s in zip(titles[:4], snippets[:4]):
                results.append(f"{re.sub('<[^>]+>','',t).strip()}: {re.sub('<[^>]+>','',s).strip()}")
            return "\n".join(results) or "No results"
    except Exception as e:
        return f"Search failed: {e}"

async def run_learning_cycle():
    if not is_overnight():
        return

    log   = load_json(LEARNING_LOG, {"sessions": [], "total": 0})
    today = datetime.now(timezone(timedelta(hours=2))).strftime("%Y-%m-%d")
    if log.get("sessions") and log["sessions"][-1].get("date") == today:
        return

    topics  = random.sample(CURIOSITY_DOMAINS, min(4, len(CURIOSITY_DOMAINS)))
    lessons = []
    upgrades = []

    for topic in topics:
        results = await web_search(topic)
        await asyncio.sleep(1)
        lesson = await call_claude(
            system="Extract ONE specific actionable lesson. Format: LESSON: [one sentence] | UPGRADE: YES/NO | if YES explain why in 10 words. Output only this line.",
            user=f"Topic: {topic}\nResults:\n{results}",
            max_tokens=80,
        )
        if is_clean(lesson):
            lessons.append({"topic": topic, "lesson": lesson.strip(), "timestamp": utc_now()})
            if "UPGRADE: YES" in lesson.upper():
                upgrades.append(lesson.strip())

    memory = load_json(MEMORY_FILE, {"lessons": []})
    existing = memory.get("lessons", [])
    for l in lessons:
        existing.insert(0, {"timestamp": l["timestamp"], "lesson": l["lesson"], "source": "learning", "topic": l["topic"]})
    memory["lessons"] = existing[:30]
    evo = memory.get("evolution_log", [])
    evo.insert(0, {"timestamp": utc_now(), "event": f"Learning: {len(lessons)} lessons, {len(upgrades)} upgrades"})
    memory["evolution_log"] = evo[:20]
    save_json(MEMORY_FILE, memory)

    log["sessions"] = (log.get("sessions", []))[-29:] + [{"date": today, "lessons": len(lessons), "upgrades": len(upgrades)}]
    log["total"] = log.get("total", 0) + len(lessons)
    save_json(LEARNING_LOG, log)

    print(f"[LEARNING] ✅ {len(lessons)} lessons, {len(upgrades)} upgrades")

    if upgrades:
        send_email(
            f"⚡ {len(upgrades)} upgrade opportunity tonight",
            f"NUCLEUS LEARNING — {sast_now()}\n\n" + "\n\n".join(upgrades) + f"\n\n— Nucleus Engine v{VERSION}"
        )


# ══════════════════════════════════════════════════════════════════
# 5. AUTONOMOUS DECISIONS
# ══════════════════════════════════════════════════════════════════

async def run_autonomous_decisions():
    memory = load_json(MEMORY_FILE, {})
    decisions_made = []

    # Decision 1: Shopify assembled?
    shopify_file, _ = await gh_get_file("shopify_agent.py")
    if not shopify_file or "assembled by nucleus" not in (shopify_file or "").lower():
        shopify_build, _ = await gh_get_file(SHOPIFY_BUILD)
        if shopify_build:
            build = json.loads(shopify_build)
            if build.get("percent_complete", 0) >= 100:
                print("[AUTONOMY] Shopify 100% — assembling now")
                success = await assemble_and_deploy_agent("shopify")
                if success:
                    decisions_made.append("Assembled and deployed Shopify Agent")

    # Decision 2: Init missing status files
    agent_files = ["shopify_agent.py", "nucleus_email_sanitizer.py", "nucleus_job_agent.py", "nucleus_lens_agent.py"]
    for af in agent_files:
        content, _ = await gh_get_file(af)
        if content:
            status_name = af.replace(".py", "_status.json")
            status_content, _ = await gh_get_file(status_name)
            if not status_content:
                init_status = {"last_run": None, "status": "initialised", "note": "Created by Nucleus Engine"}
                await gh_commit_file(status_name, json.dumps(init_status, indent=2), f"engine: init {status_name}")
                decisions_made.append(f"Initialised: {status_name}")

    # Decision 3: Private repo
    if not memory.get("private_repo_created") and GH_PAT:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    f"{GH_API}/user/repos",
                    headers=gh_headers(),
                    json={"name": "nucleus-private", "description": "Nucleus private workspace", "private": True, "auto_init": True}
                )
                if r.status_code in (200, 201):
                    memory["private_repo_created"] = True
                    save_json(MEMORY_FILE, memory)
                    decisions_made.append("Created private repo")
                    send_email("Private repo created ✅", f"nucleus-private is live.\n— Nucleus Engine")
        except Exception as e:
            print(f"[AUTONOMY] Private repo: {e}")

    if decisions_made:
        for d in decisions_made:
            print(f"  [AUTONOMY] • {d}")

    return decisions_made


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

async def run():
    print(f"[ENGINE] Nucleus Autonomous Engine v{VERSION} — {sast_now()}")
    await run_autonomous_decisions()
    await run_learning_cycle()
    print(f"[ENGINE] Done — {sast_now()}")


if __name__ == "__main__":
    asyncio.run(run())
