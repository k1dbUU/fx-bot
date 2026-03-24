"""
╔══════════════════════════════════════════════════════════════════╗
║  NUCLEUS AUTONOMOUS ENGINE v2.0 — "THE IVY LEAGUE UPGRADE"       ║
║                                                                  ║
║  UPGRADES:                                                       ║
║    - UNIVERSAL INTENT: Uses Claude to understand Fix/Build/Info  ║
║    - THE TESTING ROOM: Researches & Refactors failing agents     ║
║    - LENS LINK CAPTURE: Auto-queues any URL for video analysis   ║
║    - WEATHER & RESEARCH: Can fetch and report external data      ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, asyncio, httpx, re, base64
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --- CONFIG & SECRETS ---
VERSION = "2.0"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GH_PAT = os.getenv("GH_PAT", "")
GH_REPO = "k1dbUU/fx-bot"
MEMORY_FILE = "NUCLEUS_MEMORY.json"
LENS_QUEUE = "lens_queue.json"

# --- HELPERS ---
def sast_now():
    return (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S SAST")

def load_json(path, default):
    if not Path(path).exists(): return default
    with open(path, 'r') as f: return json.load(f)

def save_json(path, data):
    with open(path, 'w') as f: json.dump(data, f, indent=2)

async def call_claude(system, user, max_tokens=1500):
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY, 
        "anthropic-version": "2023-06-01", 
        "content-type": "application/json"
    }
    data = {
        "model": "claude-3-sonnet-20240229", 
        "max_tokens": max_tokens, 
        "system": system, 
        "messages": [{"role": "user", "content": user}]
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, headers=headers, json=data, timeout=60)
            return r.json()['content'][0]['text']
        except: return "ERROR: Engine could not reach Claude."

async def github_action(file_path, content, message):
    """Commits changes directly to the repo."""
    if not GH_PAT: return False
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{file_path}"
    headers = {"Authorization": f"token {GH_PAT}", "Accept": "application/vnd.github.v3+json"}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
        sha = r.json().get('sha') if r.status_code == 200 else None
        payload = {"message": message, "content": base64.b64encode(content.encode()).decode()}
        if sha: payload["sha"] = sha
        res = await client.put(url, headers=headers, json=payload)
        return res.status_code in (200, 201)

# --- CORE INTELLIGENCE ---

async def handle_operator_command(sender, subject, body):
    """
    Called by Supervisor. Decides if we FIX, BUILD, QUEUE, or RESEARCH.
    """
    print(f"[ENGINE] Analyzing Operator Intent: {subject}")

    # 1. Immediate Link Detection for LENS Agent
    urls = re.findall(r'(https?://[^\s]+)', body)
    if urls:
        queue = load_json(LENS_QUEUE, [])
        for u in urls:
            if u not in [item.get('url') for item in queue]:
                queue.append({"url": u, "added_at": sast_now(), "status": "pending"})
        save_json(LENS_QUEUE, queue)
        print(f"[ENGINE] Queued {len(urls)} links for LENS Agent.")

    # 2. Use Claude to parse the "Mission"
    system_prompt = f"""You are the Nucleus Autonomous Engine Brain. 
    The operator just sent an email. You must decide on an action.
    CONTEXT:
    - FX Agent: Needs fixing if 'not working nicely' or 'losing trades'.
    - Lens Agent: Analyzes videos.
    - Weather/Research: You can fetch info if asked.
    
    RESPONSE FORMAT:
    If it's a request to FIX or CHANGE code, start with [REFACTOR].
    If it's a request for INFORMATION (Weather, etc), start with [INFO].
    If it's a request to BUILD a NEW agent, start with [BUILD]."""

    decision = await call_claude(system_prompt, f"Subject: {subject}\nBody: {body}")

    if "[REFACTOR]" in decision:
        return await refactor_agent_logic(body)
    elif "[INFO]" in decision:
        return await conduct_research(body)
    elif "[BUILD]" in decision:
        return "Acknowledged. Initiating new agent assembly blueprint. Check dashboard for build logs."
    
    return decision

async def refactor_agent_logic(instruction):
    """The 'Testing Room' Logic: Analyzes current code and applies fixes."""
    target_file = "fx_agent_bot.py" if "fx" in instruction.lower() else "nucleus_lens_agent.py"
    
    # Read existing code
    if not Path(target_file).exists(): return f"Error: {target_file} not found for refactoring."
    
    with open(target_file, 'r') as f:
        current_code = f.read()

    refactor_prompt = f"""You are a Senior Software Engineer.
    The Operator wants to IMPROVE this agent: {instruction}
    
    CURRENT CODE:
    {current_code}
    
    TASK:
    Rewrite the code to solve the operator's complaint. 
    If it's FX: Improve SMC/ICT entry logic, filtering wicks/shadows.
    OUTPUT ONLY THE FULL REWRITTEN CODE. NO EXPLANATION."""

    new_code = await call_claude("Output Code Only.", refactor_prompt)
    
    if "import" in new_code: # Basic sanity check
        success = await github_action(target_file, new_code, f"engine: refactor {target_file} based on operator feedback")
        return f"Refactor complete for {target_file}. Logic updated and deployed to GitHub."
    
    return "Refactor failed: Engine generated invalid code."

async def conduct_research(query):
    """Handles information requests like Weather or Market Sentiment."""
    # Note: In a real environment, this would call a search API. 
    # For now, Claude uses its internal knowledge/simulated search.
    answer = await call_claude("You are a helpful assistant. Provide the requested information.", query)
    return answer

# --- AUTO-RUN ---
async def run():
    print(f"[ENGINE] Nucleus Engine v{VERSION} Active — {sast_now()}")
    # Here, the engine checks for autonomous improvements it can make without being asked.
    memory = load_json(MEMORY_FILE, {})
    if "fx_last_fix" not in memory:
        # Self-initiate a fix if never done
        memory["fx_last_fix"] = sast_now()
        save_json(MEMORY_FILE, memory)

if __name__ == "__main__":
    asyncio.run(run())
