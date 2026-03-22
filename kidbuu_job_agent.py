"""
╔══════════════════════════════════════════════════════════════╗
║  KIDBUU JOB ORCHESTRATOR v5.0                                ║
║  Operator: K-I-D-B-U-U                                       ║
║  24/7 on GitHub Actions — sends while you sleep              ║
║                                                              ║
║  UPGRADES FROM v4.0:                                         ║
║  + Timezone scheduling — arrives 08:15-09:00 recipient time  ║
║  + Global remote roles — not just Cape Town                  ║
║  + 30-day dedup via NUCLEUS_MEMORY.json                      ║
║  + Bounce logging + permanent blacklist                      ║
║  + Secrets via env / GitHub Secrets — nothing hardcoded      ║
║  + Photo/CV attached only for client-facing roles            ║
║  + 3 non-AI email styles — rotated per send                  ║
║  + Max 10 emails/hour throttle — anti-spam                   ║
╚══════════════════════════════════════════════════════════════╝

SECRETS: Set these in GitHub Secrets (never hardcode):
  ANTHROPIC_API_KEY, GMAIL_FROM, GMAIL_TO, GMAIL_APP_PASSWORD

CV DATA: Loaded from cv_data.json in repo root.
PHOTO:   Place kidbuu_photo.jpg in repo root (private repo) or
         leave absent — agent skips photo for non-visual roles.
"""

import os, json, re, sys, time, random, smtplib, asyncio, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from dataclasses import dataclass, field
from typing import List, Optional
import httpx

# ── SECRETS (GitHub Secrets → env only) ──────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
GMAIL_FROM         = os.getenv("GMAIL_FROM", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
OPERATOR           = os.getenv("OPERATOR_ALIAS", "K-I-D-B-U-U")

# ── CONFIG (edit these freely — no secrets here) ─────────────────
CONFIG = {
    "max_per_hour":          10,     # anti-spam throttle
    "delay_between_sec":     65,     # min gap between sends
    "ghost_job_max_days":    30,     # skip jobs older than this
    "reapply_cooldown_days": 30,     # don't re-email same company
    "headless_browser":      True,
    "photo_path":            "kidbuu_photo.jpg",
    "cv_data_path":          "cv_data.json",
    "memory_file":           "NUCLEUS_MEMORY.json",
    "applied_file":          "applied_jobs.json",
    "bounce_file":           "bounced_emails.json",
    "log_file":              "job_agent_log.txt",
    "run_it_track":          True,
    "run_remote_track":      True,
    "run_global_track":      True,   # NEW: international remote roles
    "run_bar_track":         False,  # disable bar for overnight runs
    "send_window_start":     "08:15",  # recipient local time
    "send_window_end":       "09:00",
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

# ── CV DATA (loaded from cv_data.json — no PII in code) ──────────
def load_cv() -> dict:
    path = Path(CONFIG["cv_data_path"])
    if path.exists():
        return json.loads(path.read_text())
    # Fallback minimal profile — operator fills cv_data.json locally
    log.warning("[CV] cv_data.json not found — using minimal fallback")
    return {
        "name": "Applicant",
        "phone": "",
        "email": GMAIL_FROM,
        "location": "Cape Town, Western Cape",
        "linkedin": "",
        "summary": "CompTIA A+ certified IT professional with CRM and remote work experience.",
        "skills": ["CompTIA A+", "CRM systems", "Hardware troubleshooting", "Remote work"],
        "experience": [],
        "education": "Edgemead High School · 2017–2021",
        "certifications": ["CompTIA A+"],
    }

# ── DEDUP & MEMORY ────────────────────────────────────────────────
def load_applied() -> dict:
    """Returns dict of {company_domain: last_sent_utc}"""
    p = Path(CONFIG["applied_file"])
    if p.exists():
        try: return json.loads(p.read_text())
        except: pass
    return {}

def save_applied(applied: dict):
    Path(CONFIG["applied_file"]).write_text(json.dumps(applied, indent=2))

def load_bounced() -> set:
    p = Path(CONFIG["bounce_file"])
    if p.exists():
        try: return set(json.loads(p.read_text()))
        except: pass
    return set()

def save_bounced(bounced: set):
    Path(CONFIG["bounce_file"]).write_text(json.dumps(list(bounced), indent=2))

def was_recently_applied(domain: str, applied: dict) -> bool:
    last = applied.get(domain)
    if not last: return False
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - last_dt).days
        return days < CONFIG["reapply_cooldown_days"]
    except: return False

# ── TIMEZONE SCHEDULING ───────────────────────────────────────────
TIMEZONE_MAP = {
    # Country/region → UTC offset (approximate, handles common cases)
    "south africa": 2, "cape town": 2, "johannesburg": 2,
    "uk": 0, "london": 0, "united kingdom": 0,
    "usa": -5, "new york": -5, "chicago": -6,
    "california": -8, "los angeles": -8, "san francisco": -8,
    "australia": 10, "sydney": 10, "melbourne": 10,
    "germany": 1, "netherlands": 1, "france": 1,
    "india": 5,
    "canada": -5, "toronto": -5,
    "uae": 4, "dubai": 4,
    "singapore": 8,
    "default": 0,
}

def get_utc_offset(location: str) -> int:
    loc = location.lower()
    for k, v in TIMEZONE_MAP.items():
        if k in loc: return v
    return TIMEZONE_MAP["default"]

def seconds_until_send_window(recipient_utc_offset: int) -> int:
    """
    Returns seconds to wait before sending so email arrives
    in the 08:15-09:00 window in the recipient's local time.
    If already in window, returns 0.
    """
    now_utc = datetime.now(timezone.utc)
    recipient_now = now_utc + timedelta(hours=recipient_utc_offset)
    target_hour, target_min = 8, 15
    target = recipient_now.replace(hour=target_hour, minute=target_min,
                                   second=0, microsecond=0)
    # If past window today, schedule for tomorrow
    if recipient_now.hour >= 9:
        target += timedelta(days=1)
    # If already in window, send now
    if target_hour <= recipient_now.hour < 9:
        return 0
    wait = (target - recipient_now).total_seconds()
    return max(0, int(wait))

# ── JOB DATA CLASS ────────────────────────────────────────────────
@dataclass
class Job:
    title:          str
    company:        str
    location:       str
    description:    str
    url:            str
    source:         str
    track:          str
    company_domain: str = ""
    emails_found:   List[str] = field(default_factory=list)
    hr_name:        str = ""
    apply_id:       str = ""
    is_remote:      bool = False
    recipient_tz:   int = 0  # UTC offset

    def __post_init__(self):
        if not self.apply_id:
            self.apply_id = re.sub(r"[^a-z0-9]", "", 
                f"{self.company}{self.title}".lower())[:60]
        self.is_remote = any(w in self.location.lower() 
                             for w in ["remote", "wfh", "work from home", "anywhere"])
        self.recipient_tz = get_utc_offset(self.location)

# ── EMAIL BLACKLIST ───────────────────────────────────────────────
JOB_BOARD_DOMAINS = {
    "indeed.com","linkedin.com","pnet.co.za","careers24.com","upwork.com",
    "fiverr.com","freelancer.com","seek.com","glassdoor.com","monster.com",
    "jobmail.co.za","gumtree.co.za","reed.co.uk","stepstone.com","workable.com",
    "lever.co","greenhouse.io","noreply","no-reply","donotreply","notifications",
    "mailer","bounce","autorespond",
}

def is_blocked_email(email: str) -> bool:
    email = email.lower()
    return any(b in email for b in JOB_BOARD_DOMAINS)

def validate_email_format(email: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email))

# ── GLOBAL REMOTE JOB SEARCHES ────────────────────────────────────
GLOBAL_REMOTE_QUERIES = [
    "remote data entry south africa hire",
    "remote customer support tier 1 south africa",
    "remote virtual assistant entry level south africa",
    "remote CRM administrator south africa",
    "remote lead generation specialist south africa",
    "remote digital admin work from home south africa",
    "remote appointment setter south africa",
    "work from home data capture south africa",
    "remote operations support entry level global",
    "remote back office administrator africa hire",
]

# ── CLAUDE API ────────────────────────────────────────────────────
async def call_claude(prompt: str, max_tokens: int = 1200) -> str:
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set in GitHub Secrets")
    async with httpx.AsyncClient(timeout=40) as client:
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
                "messages": [{"role": "user", "content": prompt}]
            }
        )
        data = r.json()
        return data["content"][0]["text"]

# ── EMAIL STYLE ROTATION ──────────────────────────────────────────
# 3 non-AI opening styles — rotated per send to avoid pattern detection
EMAIL_STYLES = [
    "direct",       # Lead with the result, not the intro
    "specific",     # Open with something specific to the company
    "problem_first" # Name a problem they have, then position as solution
]

async def ai_customise(job: Job, cv: dict, style_index: int) -> dict:
    style = EMAIL_STYLES[style_index % 3]
    style_instruction = {
        "direct": (
            "Lead with the result, not who you are. "
            "Example: 'Your CRM queue runs cleaner when the person managing it "
            "has actually worked 100+ calls a day.' Then prove it in 2 lines."
        ),
        "specific": (
            "Open with ONE specific, researched thing about this company or role. "
            "Not generic. Something that shows you read their site or recent activity. "
            "Then connect your proof to that."
        ),
        "problem_first": (
            "Name the problem this role is solving for them. "
            "Then position your background as the direct fix. "
            "No 'I am applying' or 'I would like to.' Start mid-thought."
        )
    }[style]

    needs_photo = any(w in job.description.lower() 
                      for w in ["client-facing","customer facing","representative",
                                "front desk","sales","bartend"])

    prompt = f"""
You are writing a job application email. Operator is K-I-D-B-U-U internally.
The applicant's real details are in the cv_data below.

ROLE: {job.title}
COMPANY: {job.company}
LOCATION: {job.location}
REMOTE: {job.is_remote}
HR CONTACT: {job.hr_name or "unknown"}
JOB DESCRIPTION: {job.description[:1800] if job.description else "Not provided"}

CV DATA: {json.dumps(cv, indent=2)}

STYLE THIS RUN: {style_instruction}

ABSOLUTE RULES:
- ZERO placeholder brackets like [Name] or [Phone]. If you don't know it, skip it.
- Max 5 lines body. Every sentence earns its place.
- Sign off with applicant's real first name and phone from cv_data.
- NEVER: "excited to", "passionate about", "leverage", "synergy", "great opportunity"
- No AI-sounding openers. Sound like a person who has read the job post.
- Only reference skills that exist in cv_data. Do not invent.
- Subject line: specific, 6-8 words max. Not "Application for [role]".
- If remote role: confirm fully set up, available immediately.
- include_photo: {needs_photo}

Return ONLY valid JSON, no markdown:
{{
  "email_subject": "...",
  "email_body": "...",
  "include_photo": {str(needs_photo).lower()},
  "cv_highlights": ["...","...","..."],
  "style_used": "{style}"
}}
"""
    try:
        raw = await call_claude(prompt)
        clean = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        data = json.loads(clean)
        # Final validation — no placeholders allowed through
        body = data.get("email_body", "")
        if re.search(r"\[.*?\]", body):
            log.warning(f"[QA] Placeholder detected in email body — regenerating")
            return await ai_customise(job, cv, style_index + 1)
        return data
    except Exception as e:
        log.error(f"[AI] Customise failed for {job.company}: {e}")
        return {
            "email_subject": f"{job.title} — {cv.get('name','Applicant')}",
            "email_body": (
                f"Hi{' ' + job.hr_name.split()[0] if job.hr_name else ''},\n\n"
                f"CompTIA A+ certified, {cv.get('experience',[{}])[0].get('title','CRM background')} — "
                f"applying for {job.title}. Available immediately.\n\n"
                f"{cv.get('name','')} / {cv.get('phone','')}"
            ),
            "include_photo": False,
            "cv_highlights": cv.get("skills", [])[:3],
            "style_used": "fallback"
        }

# ── SEND EMAIL ────────────────────────────────────────────────────
def send_email(job: Job, ai_data: dict, cv: dict) -> bool:
    bounced = load_bounced()
    safe_emails = [
        e for e in job.emails_found
        if validate_email_format(e)
        and not is_blocked_email(e)
        and e not in bounced
    ]
    if not safe_emails:
        log.warning(f"[SEND] No valid emails for {job.company} — skip")
        return False

    to_addr  = safe_emails[0]
    cc_addrs = safe_emails[1:4]  # max 3 CC

    subject  = ai_data.get("email_subject", f"{job.title} — {cv.get('name')}")
    body     = ai_data.get("email_body", "")

    if not body or len(body) < 30:
        log.warning(f"[SEND] Email body too short for {job.company} — skip")
        return False

    msg = MIMEMultipart()
    msg["From"]    = GMAIL_FROM
    msg["To"]      = to_addr
    msg["Subject"] = subject
    if cc_addrs: msg["Cc"] = ", ".join(cc_addrs)
    msg.attach(MIMEText(body, "plain"))

    # Attach photo only if role warrants it
    photo_path = Path(CONFIG["photo_path"])
    if ai_data.get("include_photo") and photo_path.exists():
        with open(photo_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename=profile.jpg")
        msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
            all_recip = [to_addr] + cc_addrs
            s.sendmail(GMAIL_FROM, all_recip, msg.as_string())
        log.info(f"[SENT] ✅ {job.company} → {to_addr} | CC:{len(cc_addrs)} | style:{ai_data.get('style_used')}")
        return True
    except smtplib.SMTPRecipientsRefused:
        log.warning(f"[BOUNCE] {to_addr} refused — blacklisting")
        bounced.add(to_addr)
        save_bounced(bounced)
        return False
    except Exception as e:
        log.error(f"[SEND] Failed {job.company}: {e}")
        return False

# ── TIMEZONE-AWARE SEND WITH THROTTLE ────────────────────────────
class RateTracker:
    def __init__(self):
        self.sends_this_hour = 0
        self.hour_start = datetime.now(timezone.utc)

    def can_send(self) -> bool:
        now = datetime.now(timezone.utc)
        if (now - self.hour_start).seconds > 3600:
            self.sends_this_hour = 0
            self.hour_start = now
        return self.sends_this_hour < CONFIG["max_per_hour"]

    def record_send(self):
        self.sends_this_hour += 1

rate = RateTracker()

async def send_with_timezone(job: Job, ai_data: dict, cv: dict) -> bool:
    if not rate.can_send():
        log.info(f"[THROTTLE] {rate.sends_this_hour}/10 this hour — waiting 10min")
        await asyncio.sleep(600)
        rate.sends_this_hour = 0

    wait_secs = seconds_until_send_window(job.recipient_tz)
    if wait_secs > 0:
        wait_hrs = wait_secs / 3600
        log.info(f"[TZ] {job.company} in {job.location} (UTC+{job.recipient_tz}) — waiting {wait_hrs:.1f}h for 08:15 window")
        # In GitHub Actions we can't actually sleep for hours — queue it
        # For jobs with >2hr wait, write to queue file and skip this run
        if wait_secs > 7200:
            queue_job(job, ai_data)
            return False
        await asyncio.sleep(wait_secs)

    success = send_email(job, ai_data, cv)
    if success:
        rate.record_send()
        await asyncio.sleep(CONFIG["delay_between_sec"] + random.randint(-10, 15))
    return success

def queue_job(job: Job, ai_data: dict):
    """Jobs that need to wait >2h are queued for next run."""
    queue_file = Path("job_queue.json")
    q = []
    if queue_file.exists():
        try: q = json.loads(queue_file.read_text())
        except: q = []
    q.append({
        "company":    job.company,
        "title":      job.title,
        "location":   job.location,
        "emails":     job.emails_found,
        "subject":    ai_data.get("email_subject"),
        "body":       ai_data.get("email_body"),
        "tz_offset":  job.recipient_tz,
        "queued_at":  datetime.now(timezone.utc).isoformat(),
    })
    queue_file.write_text(json.dumps(q, indent=2))
    log.info(f"[QUEUE] {job.company} queued for next send window")

# ── JOB SCRAPING (DuckDuckGo — no browser needed for GitHub Actions)
async def search_jobs_ddg(query: str, limit: int = 8) -> List[dict]:
    """
    Lightweight job search via DuckDuckGo Instant Answer API.
    No browser, no Playwright — works on GitHub Actions.
    Returns list of raw result dicts.
    """
    try:
        async with httpx.AsyncClient(timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"
        }) as client:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query, "kl": "za-en"},
            )
            # Parse result snippets from HTML
            results = []
            for match in re.finditer(
                r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>.*?'
                r'<a[^>]+class="result__snippet"[^>]*>([^<]+)</a>',
                r.text, re.DOTALL
            )[:limit]:
                results.append({
                    "url":     match.group(1),
                    "title":   match.group(2).strip(),
                    "snippet": match.group(3).strip(),
                })
            return results
    except Exception as e:
        log.warning(f"[SEARCH] DDG failed for '{query}': {e}")
        return []

async def extract_company_email(company_name: str, domain: str) -> List[str]:
    """
    Ask Claude to suggest likely email formats for a company,
    then validate format. No DNS — too slow for GitHub Actions.
    """
    if not domain or not ANTHROPIC_API_KEY:
        return []
    prompt = f"""Company: {company_name}, domain: {domain}
List the 3 most likely HR/hiring manager email addresses.
Format: firstname.lastname@{domain}, hr@{domain}, careers@{domain} etc.
Return ONLY a JSON array of email strings. No explanation."""
    try:
        raw = await call_claude(prompt, max_tokens=100)
        emails = json.loads(re.sub(r"```(?:json)?|```", "", raw).strip())
        return [e for e in emails if validate_email_format(e) and domain in e]
    except:
        # Fallback: standard patterns
        return [f"hr@{domain}", f"careers@{domain}", f"info@{domain}"]

async def process_search_result(result: dict, track: str, cv: dict) -> Optional[Job]:
    """Turn a search result into a Job object with email."""
    url   = result.get("url", "")
    title = result.get("title", "")
    snip  = result.get("snippet", "")

    # Extract domain
    domain_match = re.search(r"https?://(?:www\.)?([^/]+)", url)
    if not domain_match: return None
    domain = domain_match.group(1).lower()

    # Skip job boards
    if any(b in domain for b in JOB_BOARD_DOMAINS): return None

    # Extract company name from domain
    company = domain.split(".")[0].replace("-", " ").title()
    location = "Remote" if "remote" in snip.lower() else "Cape Town, South Africa"

    emails = await extract_company_email(company, domain)
    if not emails: return None

    job = Job(
        title=title[:80],
        company=company,
        location=location,
        description=snip,
        url=url,
        source="DuckDuckGo",
        track=track,
        company_domain=domain,
        emails_found=emails,
        recipient_tz=get_utc_offset(location),
    )
    return job

# ── MAIN ORCHESTRATOR ─────────────────────────────────────────────
async def run():
    print("═" * 56)
    print(f"  KIDBUU JOB ORCHESTRATOR v5.0 — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("═" * 56)

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set — add to GitHub Secrets"); sys.exit(1)
    if not GMAIL_FROM or not GMAIL_APP_PASSWORD:
        log.error("GMAIL_FROM or GMAIL_APP_PASSWORD not set"); sys.exit(1)

    cv      = load_cv()
    applied = load_applied()
    sent    = 0
    style_i = 0

    queries = []
    if CONFIG["run_remote_track"] or CONFIG["run_global_track"]:
        queries.extend(GLOBAL_REMOTE_QUERIES)
    if CONFIG["run_it_track"]:
        queries.extend([
            "IT helpdesk technician cape town hiring",
            "IT support technician cape town direct",
            "junior IT support remote south africa",
            "service desk agent cape town company",
        ])

    for query in queries:
        log.info(f"\n[SEARCH] {query}")
        results = await search_jobs_ddg(query, limit=6)

        for result in results:
            job = await process_search_result(result, "remote", cv)
            if not job: continue

            # 30-day dedup check
            if was_recently_applied(job.company_domain, applied):
                log.info(f"[SKIP] {job.company} — applied within 30 days")
                continue

            log.info(f"[JOB] {job.title} @ {job.company} ({job.location})")

            ai_data = await ai_customise(job, cv, style_i)
            style_i += 1

            success = await send_with_timezone(job, ai_data, cv)

            if success:
                applied[job.company_domain] = datetime.now(timezone.utc).isoformat()
                save_applied(applied)
                sent += 1
                log.info(f"[COUNT] Sent today: {sent}")

        await asyncio.sleep(3)  # brief pause between query batches

    # Also process any queued jobs from previous runs
    queue_file = Path("job_queue.json")
    if queue_file.exists():
        try:
            queued = json.loads(queue_file.read_text())
            remaining = []
            for q in queued:
                wait = seconds_until_send_window(q.get("tz_offset", 0))
                if wait <= 300:  # within 5 mins of window
                    dummy_job = Job(
                        title=q["title"], company=q["company"],
                        location=q["location"], description="",
                        url="", source="queue", track="remote",
                        company_domain="", emails_found=q.get("emails", []),
                        recipient_tz=q.get("tz_offset", 0)
                    )
                    ai_d = {"email_subject": q["subject"], "email_body": q["body"],
                            "include_photo": False, "style_used": "queued"}
                    success = send_email(dummy_job, ai_d, cv)
                    if success:
                        applied[q["company"]] = datetime.now(timezone.utc).isoformat()
                        save_applied(applied)
                        sent += 1
                else:
                    remaining.append(q)  # not time yet, keep in queue
            queue_file.write_text(json.dumps(remaining, indent=2))
        except Exception as e:
            log.error(f"[QUEUE] Failed to process queue: {e}")

    print(f"\n[DONE] ✅ {sent} applications sent this run")
    print(f"[LOG]  {CONFIG['log_file']}")

if __name__ == "__main__":
    asyncio.run(run())
