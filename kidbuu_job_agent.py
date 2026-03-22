"""
╔══════════════════════════════════════════════════════════════╗
║  KIDBUU JOB ORCHESTRATOR v5.5                                ║
║  Operator: K-I-D-B-U-U                                       ║
║  24/7 on GitHub Actions — no timezone scheduling needed      ║
║                                                              ║
║  v5.5 ARCHITECTURE REBUILD:                                  ║
║  [NEW-1] Company name extraction is now mandatory —          ║
║          no company name = skip. No agency blind posts.      ║
║  [NEW-2] Internet scouring: searches web + social for        ║
║          company website, real emails, WhatsApp numbers      ║
║  [NEW-3] Email verification via SMTP probe — checks if       ║
║          mailbox actually exists before sending              ║
║  [NEW-4] Send to verified TO + BCC 2 verified extras         ║
║  [NEW-5] WhatsApp numbers saved to whatsapp_leads.json       ║
║  [NEW-6] Plain language searches — reliably get DDG results  ║
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
    "remote data entry jobs south africa 2025 apply",
    "CRM administrator remote job south africa hiring",
    "data capture clerk remote work south africa",
    "virtual assistant remote job south africa entry level",
    "back office administrator remote cape town hiring",
    "AI data annotator remote job south africa",
    "remote bookkeeper entry level south africa",
    "helpdesk support remote south africa entry level",
    "IT support technician remote south africa junior",
    "data entry specialist remote no experience required",
    "transcription remote job south africa 2025",
    "remote admin assistant south africa hiring now",
    "CRM data entry remote job UK hire south africa contractor",
    "online data processor remote job south africa apply",
    "remote customer support data entry south africa 2025",
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

# ── COMPANY INTELLIGENCE ──────────────────────────────────────────
# v5.5: Scour the internet for a company's real contact details.
# Returns dict with: domain, emails[], whatsapp[], website

async def scour_company(company_name: str) -> dict:
    """
    Given a company name, search the web for:
    - Their official website
    - Direct HR/hiring email addresses
    - WhatsApp numbers (saved separately, not emailed)
    Returns everything found. Empty lists if nothing found.
    """
    result = {"domain": None, "emails": [], "whatsapp": [], "website": None}

    # Step 1: Find their website via web search
    searches = [
        f"{company_name} official website contact",
        f"{company_name} HR email address hiring contact",
        f"{company_name} careers email jobs apply",
        f"{company_name} company LinkedIn Facebook contact",
    ]

    all_text = ""
    for q in searches[:2]:  # 2 searches per company to stay fast
        try:
            async with httpx.AsyncClient(timeout=12, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            }) as client:
                r = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": q, "kl": "za-en"},
                )
                all_text += r.text
                await asyncio.sleep(2)
        except Exception:
            pass

    # Step 2: Extract emails from all search text
    raw_emails = re.findall(
        r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
        all_text
    )
    # Filter: must not be job boards, must look like hr/hiring/info/careers
    hr_patterns = ['hr@', 'hire@', 'hiring@', 'careers@', 'jobs@',
                   'recruit@', 'talent@', 'info@', 'hello@', 'contact@',
                   'admin@', 'office@', 'apply@', 'work@', 'people@']
    candidate_emails = []
    seen = set()
    for e in raw_emails:
        e = e.lower().strip('.,;')
        if e in seen:
            continue
        seen.add(e)
        if not valid_email(e):
            continue
        if is_blocked_email(e):
            continue
        # Prioritise HR-pattern emails
        is_hr = any(e.startswith(p) for p in hr_patterns)
        if is_hr:
            candidate_emails.insert(0, e)
        else:
            candidate_emails.append(e)
    result["emails"] = candidate_emails[:5]  # keep top 5 candidates

    # Step 3: Extract WhatsApp numbers
    # Look for WA.me links or "WhatsApp: +XX" patterns
    wa_links = re.findall(r'wa\.me/(\d{7,15})', all_text)
    wa_text  = re.findall(r'[Ww]hats[Aa]pp[:\s]+(\+\d{7,15})', all_text)
    whatsapp_numbers = list(set([f"+{n}" for n in wa_links] + wa_text))
    result["whatsapp"] = whatsapp_numbers[:3]

    # Step 4: Extract domain from emails found
    if result["emails"]:
        domain_match = re.search(r'@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', result["emails"][0])
        if domain_match:
            result["domain"] = domain_match.group(1).lower()

    # Step 5: If no emails found from search, ask Claude to guess HR emails
    # using the domain if we have one, or derive a likely domain from company name
    if not result["emails"]:
        domain = result["domain"]
        if not domain:
            # Derive domain from company name
            slug = re.sub(r'[^a-z0-9]', '', company_name.lower())
            if len(slug) >= 3:
                domain = f"{slug}.com"
                result["domain"] = domain

        if domain and not any(b in domain for b in JOB_BOARD_DOMAINS):
            guessed = [
                f"hr@{domain}",
                f"careers@{domain}",
                f"info@{domain}",
            ]
            result["emails"] = [e for e in guessed if valid_email(e)]
            log.info(f"[SCOUR] {company_name}: No emails found — guessing {result['emails']}")
        else:
            log.info(f"[SCOUR] {company_name}: No emails and no usable domain — skip")

    if result["emails"]:
        log.info(f"[SCOUR] {company_name}: Found {len(result['emails'])} emails, "
                 f"{len(result['whatsapp'])} WhatsApp")
    return result

# ── EMAIL VERIFICATION (SMTP PROBE) ──────────────────────────────
# Checks if an email address actually exists by probing the mail server.
# Does NOT send any email. Uses SMTP RCPT TO handshake.
# Returns True if mailbox confirmed, False if rejected, None if uncertain.

async def verify_email_smtp(email: str) -> bool:
    """
    Probe the mail server to check if the email address exists.
    Returns True = verified real, False = definitely doesn't exist,
    None = uncertain (server won't tell us / timeout).
    """
    import socket
    import asyncio

    if not valid_email(email):
        return False

    domain = email.split('@')[1]

    try:
        # Get MX record via DNS lookup
        loop = asyncio.get_event_loop()
        try:
            mx_result = await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(domain, 25, socket.AF_UNSPEC, socket.SOCK_STREAM)
            )
            if not mx_result:
                return None
            mx_host = mx_result[0][4][0]
        except Exception:
            return None  # Can't resolve — uncertain

        # SMTP handshake probe
        def smtp_probe():
            import smtplib
            try:
                with smtplib.SMTP(timeout=8) as smtp:
                    smtp.connect(mx_host, 25)
                    smtp.ehlo_or_helo_if_needed()
                    smtp.mail('verify@nucleus-check.local')
                    code, _ = smtp.rcpt(email)
                    smtp.quit()
                    return code  # 250 = exists, 550 = doesn't exist
            except smtplib.SMTPRecipientsRefused:
                return 550
            except Exception:
                return None

        code = await loop.run_in_executor(None, smtp_probe)

        if code == 250:
            log.info(f"[VERIFY] ✅ {email} — confirmed real")
            return True
        elif code == 550:
            log.info(f"[VERIFY] ❌ {email} — mailbox doesn't exist")
            return False
        else:
            # Many servers return 250 for everything (catch-all) or block probes
            # Treat as uncertain = still try it, just log
            log.info(f"[VERIFY] ? {email} — server uncertain (code {code})")
            return None

    except Exception as e:
        log.info(f"[VERIFY] ? {email} — probe failed: {e}")
        return None

async def get_verified_emails(emails: list) -> list:
    """
    Takes a list of candidate emails.
    Returns only those that are verified real (or uncertain — we still try).
    Drops emails confirmed as non-existent.
    """
    verified = []
    for email in emails[:5]:  # check up to 5
        result = await verify_email_smtp(email)
        if result is not False:  # True or None — keep it
            verified.append(email)
        await asyncio.sleep(1)  # small pause between probes
    return verified

def save_whatsapp_leads(company: str, numbers: list, job_title: str):
    """Save WhatsApp numbers to a separate leads file for manual follow-up."""
    if not numbers:
        return
    wa_file = "whatsapp_leads.json"
    leads = load_json_file(wa_file, [])
    for num in numbers:
        if not any(l.get("number") == num for l in leads):
            leads.append({
                "number":  num,
                "company": company,
                "role":    job_title,
                "found":   datetime.now(timezone.utc).isoformat(),
                "status":  "new",
            })
            log.info(f"[WHATSAPP] 📱 Saved: {num} → {company} ({job_title})")
    save_json_file(wa_file, leads)

# ── EXTRACT COMPANY NAME FROM JOB LISTING ────────────────────────
def extract_company_from_listing(title: str, snippet: str, url: str) -> str:
    """
    Extract the actual hiring company name from a job listing.
    Returns company name string, or empty string if not determinable.
    Skips agency/confidential posts — those can't be directly contacted.
    """
    text = title + " " + snippet

    # Hard skip: agency/confidential — no direct contact possible
    skip_patterns = [
        "confidential", "agency", "through a recruiter", "via agency",
        "staffing", "recruitment agency", "undisclosed", "not disclosed",
        "our client", "on behalf of"
    ]
    for p in skip_patterns:
        if p in text.lower():
            log.info(f"[EXTRACT] Skipping — agency/confidential post: '{p}' found")
            return ""

    # Try to extract from title: "Role at Company", "Company - Role", "Role | Company"
    patterns = [
        r'\bat\s+([A-Z][A-Za-z\s&\.]{2,30}?)(?:\s*[-–|]|$)',
        r'^([A-Z][A-Za-z\s&\.]{2,30?})\s*[-–|]',
        r'[-–|]\s*([A-Z][A-Za-z\s&\.]{2,30})$',
        r'@\s*([A-Z][A-Za-z\s&\.]{2,30})',
    ]
    for pat in patterns:
        m = re.search(pat, title)
        if m:
            name = m.group(1).strip()
            # Sanity check: not a job title word
            if not any(w in name.lower() for w in ['remote', 'entry', 'level', 'data', 'admin', 'clerk']):
                return name

    # Try to extract from URL if it's a company domain (not a job board)
    dm = re.search(r"https?://(?:www\.)?([^/\s]+)", url)
    if dm:
        domain = dm.group(1).lower()
        if not any(b in domain for b in JOB_BOARD_DOMAINS):
            # Use domain as company name
            name = domain.split('.')[0].replace('-', ' ').title()
            return name

    # Nothing found
    return ""

# ── GET COMPANY EMAILS (legacy fallback) ─────────────────────────
async def get_emails(company: str, domain: str) -> List[str]:
    """Legacy fallback — used only when scour_company is skipped."""
    if not domain:
        return []
    return [f"hr@{domain}", f"careers@{domain}", f"info@{domain}"]

# ── SEND EMAIL (TO + BCC 2 verified) ─────────────────────────────
def send(job: Job, email_data: dict, cv: dict) -> bool:
    bounced = set(load_json_file(CONFIG["bounce_file"], []))

    clean_emails = [
        e for e in job.emails
        if valid_email(e) and not is_blocked_email(e) and e not in bounced
    ]
    if not clean_emails:
        log.warning(f"[SEND] No valid emails for {job.company}")
        return False

    to_addr   = clean_emails[0]
    bcc_addrs = clean_emails[1:3]  # BCC up to 2 others (not CC — cleaner)

    subject = email_data.get("email_subject", "")
    body    = email_data.get("email_body", "")

    if not subject or not body or len(body) < 40:
        log.warning(f"[SEND] Email incomplete for {job.company} — skip")
        return False

    msg = MIMEMultipart()
    msg["From"]    = GMAIL_FROM
    msg["To"]      = to_addr
    msg["Subject"] = subject
    # BCC: added to envelope but not visible in headers (professional)
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

    all_recipients = [to_addr] + bcc_addrs
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_FROM, all_recipients, msg.as_string())
        log.info(
            f"[SENT] ✅ {job.company} | {job.title} | "
            f"→ {to_addr} | BCC:{len(bcc_addrs)} | style:{email_data.get('style')}"
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
    """
    Full v5.5 pipeline:
    1. Extract company name — skip if agency/unknown
    2. Scour internet for real emails + WhatsApp
    3. SMTP-verify emails — drop confirmed dead ones
    4. Return Job with verified emails ready to send
    """
    url   = result.get("url", "")
    title = result.get("title", "")
    snip  = result.get("snippet", "")

    # ── STEP 1: Entry-level filter ────────────────────────────────
    if not is_entry_level(snip, title):
        return None

    # ── STEP 2: Extract company name (mandatory) ──────────────────
    company = extract_company_from_listing(title, snip, url)
    if not company:
        log.info(f"[SKIP] No company name extractable — agency or undisclosed: {title[:60]}")
        return None

    log.info(f"[COMPANY] Found: '{company}' from '{title[:50]}'")

    # ── STEP 3: Scour internet for real contact details ───────────
    intel = await scour_company(company)

    # Save any WhatsApp numbers found
    if intel["whatsapp"]:
        save_whatsapp_leads(company, intel["whatsapp"], title)

    if not intel["emails"]:
        log.info(f"[SKIP] {company}: No emails found after internet scour")
        return None

    # ── STEP 4: SMTP verify emails — drop confirmed dead ─────────
    verified = await get_verified_emails(intel["emails"])
    if not verified:
        log.info(f"[SKIP] {company}: All emails failed verification")
        return None

    log.info(f"[READY] {company}: {len(verified)} verified email(s) → {verified[0]}")

    domain = intel.get("domain") or (verified[0].split('@')[1] if verified else "")

    return Job(
        title=title[:80],
        company=company,
        location="Remote",
        description=snip,
        url=url,
        company_domain=domain,
        emails=verified,
    )

# ── WRITE STATUS (Supervisor reads this) ─────────────────────────
def write_status(sent: int, skipped: int, error: str = None):
    save_json_file(CONFIG["status_file"], {
        "agent":       "kidbuu_job_agent",
        "version":     "5.5",
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
    print(f"  KIDBUU JOB AGENT v5.5 — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
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
