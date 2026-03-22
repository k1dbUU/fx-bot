"""
╔══════════════════════════════════════════════════════════════╗
║  KIDBUU JOB ORCHESTRATOR v5.3                                ║
║  Operator: K-I-D-B-U-U                                       ║
║  24/7 on GitHub Actions — no timezone scheduling needed      ║
║                                                              ║
║  v5.3 FIXES (vs v5.2):                                       ║
║  [FIX-7] Search queries now target company career pages,     ║
║          not job boards — reduces skip rate dramatically     ║
║  [FIX-8] Job board results: extract company from title       ║
║          and derive domain — instead of hard skipping        ║
║  [FIX-9] Added skip reason logging for every filtered result ║
╚══════════════════════════════════════════════════════════════╝

TARGET ROLES (agent-compatible, entry-level, no 5yr experience):
  - Data Entry Specialist (remote)
  - CRM Data Administrator (remote)
  - Virtual Bookkeeper / Accounts Capture (remote)
  - Medical Billing / Claims Processor (remote)
  - AI Data Annotator / Transcriptionist (remote)
  - Back Office Administrator (remote)

SECRETS: ANTHROPIC_API_KEY, GMAIL_FROM, GMAIL_APP_PASSWORD
         — all via GitHub Secrets, never hardcoded
"""

import os, json, re, sys, random, smtplib, asyncio, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from dataclasses import dataclass, field
from typing import List, Optional
import httpx

# ── SECRETS ───────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
GMAIL_FROM         = os.getenv("GMAIL_FROM", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
OPERATOR           = os.getenv("OPERATOR_ALIAS", "K-I-D-B-U-U")

# ── COMMAND FILE READER ───────────────────────────────────────────
def read_nucleus_command() -> dict:
    """
    Reads nucleus_command.json if present.
    Returns parsed command dict or empty dict.
    Supports: max override, pause, resume, report.
    """
    cmd_file = os.getenv("NUCLEUS_COMMAND_FILE", "nucleus_command.json")
    try:
        if os.path.exists(cmd_file):
            with open(cmd_file) as f:
                data = json.load(f)
            status = data.get("status", "pending")
            if status == "executed":
                return {}
            cmd_text = (data.get("command") or "").lower()
            result = {"raw": cmd_text}
            # Parse: "send 20 applications and stop"
            import re
            m = re.search(r'send\s+(\d+)', cmd_text)
            if m:
                result["max_override"] = int(m.group(1))
            if "pause" in cmd_text or "stop sending" in cmd_text:
                result["pause"] = True
            if "resume" in cmd_text or "start sending" in cmd_text:
                result["resume"] = True
            print(f"[COMMAND] Received from War Room: {cmd_text}")
            return result
    except Exception as e:
        print(f"[COMMAND] Read error: {e}")
    return {}

# ── CONFIG ────────────────────────────────────────────────────────
CONFIG = {
    "max_per_run":             8,      # max emails per GitHub Actions run
    "max_per_hour":            10,     # hard anti-spam cap
    "delay_between_sec":       70,     # gap between sends
    "reapply_cooldown_days":   30,     # skip company if emailed < 30 days ago
    "cv_data_path":            "cv_data.json",
    "applied_file":            "applied_jobs.json",
    "bounce_file":             "bounced_emails.json",
    "status_file":             "job_agent_status.json",
    "log_file":                "job_agent_log.txt",
    "photo_path":              "kidbuu_photo.jpg",
}

CLAUDE_MODEL = "claude-sonnet-4-20250514"

# ── LOGGING ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("KidBuuJobs")

# ── TARGET SEARCHES ───────────────────────────────────────────────
# Entry-level, structured, no-interaction roles — globally remote
# Research-based: legal data ops, medical billing, AI annotation,
# bookkeeping, CRM admin, back office — all hire SA contractors
JOB_SEARCHES = [
    # Direct company career page searches — avoids job boards
    'site:careers "data entry" remote "apply now" entry level',
    'site:jobs "CRM administrator" remote "south africa" hire',
    '"we are hiring" "data entry" remote "work from home" apply',
    '"now hiring" "data capture" OR "CRM" remote 2025 2026',
    '"apply" "data entry clerk" remote "no experience" site:com',

    # Direct outreach targets — companies known to hire SA remote
    '"remote data entry" "south africa" contractor 2025 hire email',
    '"hiring" "data administrator" remote "cape town" OR "south africa"',
    '"data entry" "remote" "compTIA" OR "CRM" "apply" email hr@',
    '"entry level" "data processor" remote hire email careers@',
    '"virtual assistant" OR "data entry" remote hire "south africa" 2025',

    # Company job pages directly
    '"careers" "data entry" remote "immediately" OR "urgently" apply',
    '"job opening" "CRM" OR "data" remote "south africa" contractor',
    'intitle:"hiring" "back office" remote "data" "entry level" email',
    '"open position" "data capture" remote "apply" 2025 OR 2026',
    '"remote role" "data entry" OR "admin" "south africa" contact hr',
]

# ── ENTRY-LEVEL FILTER ────────────────────────────────────────────
# NUCLEUS FIX-2: Skip anything requiring experience beyond what CV shows
EXPERIENCE_BLOCKLIST = [
    "5+ years", "5 years", "senior", "lead ", "manager",
    "director", "degree required", "bachelor required",
    "master's", "phd", "10 years", "7 years",
    "must have experience in", "minimum 3 years",
]

SCAM_BLOCKLIST = [
    "pyramid", "mlm", "multi-level", "investment required",
    "pay to start", "training fee", "commission only",
    "crypto", "bitcoin", "nft",
]

def is_entry_level(description: str, title: str) -> bool:
    """Returns True if role is genuinely entry-level."""
    text = (description + " " + title).lower()
    for block in EXPERIENCE_BLOCKLIST:
        if block in text:
            log.info(f"[FILTER] Blocked — experience requirement: '{block}'")
            return False
    for scam in SCAM_BLOCKLIST:
        if scam in text:
            log.info(f"[FILTER] Blocked — scam indicator: '{scam}'")
            return False
    return True

# ── EMAIL BLOCKLIST ───────────────────────────────────────────────
JOB_BOARD_DOMAINS = {
    "indeed.com", "linkedin.com", "pnet.co.za", "careers24.com",
    "upwork.com", "fiverr.com", "freelancer.com", "seek.com",
    "glassdoor.com", "monster.com", "jobmail.co.za", "gumtree.co.za",
    "noreply", "no-reply", "donotreply", "mailer", "autorespond",
    "notifications", "bounce", "do-not-reply",
}

def is_blocked_email(email: str) -> bool:
    return any(b in email.lower() for b in JOB_BOARD_DOMAINS)

def valid_email(email: str) -> bool:
    return bool(re.match(
        r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email
    ))

# ── DATA CLASSES ──────────────────────────────────────────────────
@dataclass
class Job:
    title:          str
    company:        str
    location:       str
    description:    str
    url:            str
    company_domain: str = ""
    emails:         List[str] = field(default_factory=list)
    hr_name:        str = ""
    is_remote:      bool = True

    def __post_init__(self):
        self.is_remote = any(
            w in (self.location + self.description).lower()
            for w in ["remote", "wfh", "work from home", "anywhere"]
        )

    @property
    def apply_id(self) -> str:
        return re.sub(r"[^a-z0-9]", "",
            f"{self.company}{self.title}".lower())[:60]

# ── PERSISTENCE ───────────────────────────────────────────────────
def load_json_file(path: str, default):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return default

def save_json_file(path: str, data):
    Path(path).write_text(json.dumps(data, indent=2))

def already_applied(domain: str, applied: dict) -> bool:
    last = applied.get(domain)
    if not last:
        return False
    try:
        days = (datetime.now(timezone.utc) -
                datetime.fromisoformat(last.replace("Z", "+00:00"))).days
        return days < CONFIG["reapply_cooldown_days"]
    except Exception:
        return False

# ── CLAUDE API ────────────────────────────────────────────────────
async def call_claude(prompt: str, max_tokens: int = 800) -> str:
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY missing from GitHub Secrets")
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":           ANTHROPIC_API_KEY,
                "anthropic-version":   "2023-06-01",
                "content-type":        "application/json",
            },
            json={
                "model":      CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "messages":   [{"role": "user", "content": prompt}],
            }
        )
        return r.json()["content"][0]["text"]

# ── EMAIL WRITER ──────────────────────────────────────────────────
# NUCLEUS FIX-4: Honest CV, no inflation, 3 rotating styles
EMAIL_STYLES = ["direct", "specific", "problem_first"]

async def write_email(job: Job, cv: dict, style_idx: int) -> dict:
    style = EMAIL_STYLES[style_idx % 3]
    style_guide = {
        "direct":
            "Lead with the result. 'Your CRM stays accurate when someone "
            "has actually worked 100+ daily data tasks.' Then 2 proof lines.",
        "specific":
            "Open with ONE thing specific to this company or role — "
            "shows you read the post. Connect your real background to it.",
        "problem_first":
            "Name the problem this hire solves. Position real experience "
            "as the fix. No 'I am applying.' Start mid-thought.",
    }[style]

    # Only attach photo for client-facing/visual roles
    client_facing = any(w in job.description.lower() for w in [
        "client-facing", "customer facing", "front desk", "sales rep"])

    prompt = f"""Write a job application email. Be human. Be brief.

ROLE: {job.title}
COMPANY: {job.company}
LOCATION: {job.location}
REMOTE: {job.is_remote}
HR: {job.hr_name or "not known"}
JOB POST: {job.description[:1500]}

APPLICANT CV:
{json.dumps(cv, indent=2)}

STYLE: {style_guide}

RULES — read every one:
1. ZERO placeholders. No [Name], [Phone], [Company]. If unknown, skip it.
2. Max 5 lines body. Every line must earn its place.
3. Sign off: first name from cv + phone from cv. Nothing else.
4. BANNED words: excited, passionate, leverage, synergy, great fit,
   looking forward, thrilled, perfect opportunity, ideal candidate.
5. Only mention skills that exist in the CV. Do not invent anything.
6. No AI-sounding openers. Sound like a person, not a template.
7. Subject: 6-8 words, specific to role. Not "Application for [role]".
8. Entry-level honest tone — don't oversell. State what exists.
9. If remote: one line confirming fully set up, available immediately.

Return ONLY valid JSON, no markdown fences:
{{"email_subject":"...","email_body":"...","include_photo":{str(client_facing).lower()}}}"""

    try:
        raw = await call_claude(prompt, max_tokens=600)
        clean = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        result = json.loads(clean)

        # QA: reject if placeholder slipped through
        body = result.get("email_body", "")
        if re.search(r"\[.{1,30}\]", body):
            log.warning(f"[QA] Placeholder in body — retry with style {style_idx+1}")
            return await write_email(job, cv, style_idx + 1)

        # QA: reject if too short (probably failed)
        if len(body) < 40:
            log.warning("[QA] Body too short — using fallback")
            raise ValueError("body too short")

        result["style"] = style
        return result

    except Exception as e:
        log.error(f"[EMAIL] Write failed for {job.company}: {e}")
        # Clean fallback — honest, no buzzwords
        name = cv.get("name", "Applicant").split()[0]
        phone = cv.get("phone", "")
        return {
            "email_subject": f"Data Entry / Remote Admin — {name}",
            "email_body": (
                f"Hi{' ' + job.hr_name.split()[0] if job.hr_name else ''},\n\n"
                f"Applying for {job.title}. CompTIA A+ certified, "
                f"3+ years CRM and data handling, available immediately. "
                f"Fully remote setup.\n\n{name} / {phone}"
            ),
            "include_photo": False,
            "style": "fallback",
        }

# ── GET COMPANY EMAILS ────────────────────────────────────────────
async def get_emails(company: str, domain: str) -> List[str]:
    """Ask Claude for likely HR email formats. Validate before using."""
    if not domain:
        return []
    prompt = f"""Company: {company}, domain: {domain}
List the 3 most likely HR/hiring email addresses for this company.
Use standard patterns: hr@, careers@, hiring@, jobs@, recruit@, info@
Return ONLY a JSON array of email strings. No explanation."""
    try:
        raw = await call_claude(prompt, max_tokens=80)
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        emails = json.loads(clean)
        return [e for e in emails
                if valid_email(e) and domain in e and not is_blocked_email(e)]
    except Exception:
        # Standard fallback patterns
        return [f"hr@{domain}", f"careers@{domain}", f"info@{domain}"]

# ── SEND EMAIL ────────────────────────────────────────────────────
def send(job: Job, email_data: dict, cv: dict) -> bool:
    bounced = set(load_json_file(CONFIG["bounce_file"], []))

    clean_emails = [
        e for e in job.emails
        if valid_email(e) and not is_blocked_email(e) and e not in bounced
    ]
    if not clean_emails:
        log.warning(f"[SEND] No valid emails for {job.company}")
        return False

    to_addr  = clean_emails[0]
    cc_addrs = clean_emails[1:3]  # max 2 CC

    subject = email_data.get("email_subject", "")
    body    = email_data.get("email_body", "")

    if not subject or not body or len(body) < 40:
        log.warning(f"[SEND] Email incomplete for {job.company} — skip")
        return False

    msg = MIMEMultipart()
    msg["From"]    = GMAIL_FROM
    msg["To"]      = to_addr
    msg["Subject"] = subject
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg.attach(MIMEText(body, "plain"))

    # Photo — only if role warrants it and file exists
    if email_data.get("include_photo"):
        photo = Path(CONFIG["photo_path"])
        if photo.exists():
            with open(photo, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment; filename=profile.jpg")
            msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_FROM, [to_addr] + cc_addrs, msg.as_string())
        log.info(
            f"[SENT] ✅ {job.company} | {job.title} | "
            f"→ {to_addr} | CC:{len(cc_addrs)} | style:{email_data.get('style')}"
        )
        return True
    except smtplib.SMTPRecipientsRefused:
        log.warning(f"[BOUNCE] {to_addr} — blacklisting permanently")
        bounced.add(to_addr)
        save_json_file(CONFIG["bounce_file"], list(bounced))
        return False
    except Exception as e:
        log.error(f"[SEND] Failed {job.company}: {e}")
        return False

# ── JOB SEARCH (DuckDuckGo — no browser, works on GitHub Actions) ─
async def search(query: str, limit: int = 5) -> list:
    try:
        async with httpx.AsyncClient(timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }) as client:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query, "kl": "za-en"},
            )
            results = []
            # Extract URLs and titles
            urls    = re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text)
            titles  = re.findall(r'class="result__a"[^>]*>[^<]*<[^>]+>([^<]+)<', r.text)
            snippets = re.findall(r'class="result__snippet"[^>]*>([^<]+)<', r.text)
            for i in range(min(limit, len(urls))):
                results.append({
                    "url":     urls[i] if i < len(urls) else "",
                    "title":   titles[i].strip() if i < len(titles) else "",
                    "snippet": snippets[i].strip() if i < len(snippets) else "",
                })
            return results
    except Exception as e:
        log.warning(f"[SEARCH] Failed for '{query}': {e}")
        return []

async def result_to_job(result: dict) -> Optional[Job]:
    url   = result.get("url", "")
    title = result.get("title", "")
    snip  = result.get("snippet", "")

    dm = re.search(r"https?://(?:www\.)?([^/\s]+)", url)
    if not dm:
        log.info(f"[SKIP] No domain found in URL: {url[:60]}")
        return None
    domain = dm.group(1).lower()

    # If it's a job board, try to extract the real company from title/snippet
    is_board = any(b in domain for b in JOB_BOARD_DOMAINS)
    if is_board:
        # Try to find a company name + derive domain from snippet
        # Pattern: "Company Name - Data Entry | Indeed" or "Company Name hiring..."
        company_match = re.search(r'^([A-Z][A-Za-z\s&]{2,30})\s*[-–|]', title)
        if company_match:
            company_name = company_match.group(1).strip()
            # Derive likely domain
            slug = re.sub(r'[^a-z0-9]', '', company_name.lower())
            if len(slug) >= 3:
                domain = f"{slug}.com"
                log.info(f"[BOARD] Extracted company '{company_name}' → trying {domain}")
            else:
                log.info(f"[SKIP] Job board domain and no extractable company: {domain}")
                return None
        else:
            log.info(f"[SKIP] Job board domain, no company extractable: {domain}")
            return None

    # Entry-level filter
    if not is_entry_level(snip, title):
        return None

    company = domain.split(".")[0].replace("-", " ").title()
    location = "Remote"

    emails = await get_emails(company, domain)
    if not emails:
        log.info(f"[SKIP] No emails found for {domain}")
        return None

    return Job(
        title=title[:80],
        company=company,
        location=location,
        description=snip,
        url=url,
        company_domain=domain,
        emails=emails,
    )

# ── WRITE STATUS (Supervisor reads this) ─────────────────────────
def write_status(sent: int, skipped: int, error: str = None):
    save_json_file(CONFIG["status_file"], {
        "agent":       "kidbuu_job_agent",
        "version":     "5.3",
        "last_run":    datetime.now(timezone.utc).isoformat(),
        "sent":        sent,
        "skipped":     skipped,
        "error":       error,
        "run_id":      os.environ.get("GITHUB_RUN_ID", "local"),
    })

# ── RATE TRACKER ─────────────────────────────────────────────────
class RateTracker:
    def __init__(self):
        self._count = 0
        self._start = datetime.now(timezone.utc)

    def can_send(self) -> bool:
        now = datetime.now(timezone.utc)
        if (now - self._start).seconds > 3600:
            self._count = 0
            self._start = now
        return self._count < CONFIG["max_per_hour"]

    def record(self):
        self._count += 1

# ── MAIN ──────────────────────────────────────────────────────────
async def run():
    print("═" * 54)
    print(f"  KIDBUU JOB AGENT v5.3 — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("═" * 54)

    # Startup checks
    if not ANTHROPIC_API_KEY:
        log.error("[START] ANTHROPIC_API_KEY not set in GitHub Secrets")
        write_status(0, 0, "Missing ANTHROPIC_API_KEY")
        sys.exit(1)
    if not GMAIL_FROM or not GMAIL_APP_PASSWORD:
        log.error("[START] Gmail secrets missing")
        write_status(0, 0, "Missing Gmail secrets")
        sys.exit(1)

    # Load CV
    cv = load_json_file(CONFIG["cv_data_path"], {})
    if not cv or cv.get("name") == "YOUR FULL NAME":
        log.error("[START] cv_data.json not filled in — add CV_DATA_JSON to GitHub Secrets")
        write_status(0, 0, "cv_data.json not configured")
        sys.exit(1)

    applied = load_json_file(CONFIG["applied_file"], {})
    rate    = RateTracker()
    sent    = 0
    skipped = 0
    style_i = 0

    # ── READ WAR ROOM COMMAND ─────────────────────────────────────
    cmd = read_nucleus_command()
    max_this_run = CONFIG["max_per_run"]
    if cmd.get("pause"):
        log.info("[COMMAND] PAUSE received — skipping all sends this run")
        write_status(0, 0, "paused by command")
        print("\n[DONE] ✅ Paused by War Room command")
        return
    if cmd.get("max_override"):
        max_this_run = cmd["max_override"]
        log.info(f"[COMMAND] Max override: sending up to {max_this_run} this run")

    for query in JOB_SEARCHES:
        if sent >= max_this_run:
            log.info(f"[LIMIT] Reached {max_this_run} sends — stopping run")
            break

        log.info(f"\n[SEARCH] {query}")
        results = await search(query, limit=4)

        for result in results:
            if sent >= max_this_run:
                break
            if not rate.can_send():
                log.info("[THROTTLE] Hourly cap — pausing 10min")
                await asyncio.sleep(600)
                rate._count = 0

            # NUCLEUS FIX-6: wrap each job in try/except
            # one bad result never crashes the whole run
            try:
                job = await result_to_job(result)
                if not job:
                    skipped += 1
                    continue

                if already_applied(job.company_domain, applied):
                    log.info(f"[SKIP] {job.company} — applied within 30 days")
                    skipped += 1
                    continue

                log.info(f"[JOB] {job.title} @ {job.company}")

                email_data = await write_email(job, cv, style_i)
                style_i += 1

                success = send(job, email_data, cv)

                if success:
                    applied[job.company_domain] = datetime.now(timezone.utc).isoformat()
                    save_json_file(CONFIG["applied_file"], applied)
                    sent += 1
                    rate.record()
                    log.info(f"[COUNT] Sent this run: {sent}")
                    # Delay between sends — anti-spam
                    await asyncio.sleep(
                        CONFIG["delay_between_sec"] + random.randint(-10, 20)
                    )
                else:
                    skipped += 1

            except Exception as e:
                log.error(f"[ERROR] Unexpected: {e} — continuing to next result")
                skipped += 1
                continue

        # Brief pause between search batches
        await asyncio.sleep(3)

    write_status(sent, skipped)
    print(f"\n[DONE] ✅  Sent: {sent} | Skipped: {skipped}")
    print(f"[LOG]  {CONFIG['log_file']}")

if __name__ == "__main__":
    asyncio.run(run())
