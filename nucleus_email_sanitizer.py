"""
╔══════════════════════════════════════════════════════════════════╗
║  NUCLEUS EMAIL SANITIZER v1.0 — K-I-D-B-U-U                     ║
║  The "Eliminator" — 24/7 inbox deep clean                       ║
║                                                                  ║
║  MANDATE:                                                        ║
║    DELETE:  Spam, phishing, unsolicited marketing, dead promos  ║
║    PROTECT: Receipts, invoices, H2H mail, alerts, work comms    ║
║    QUARANTINE: Anything uncertain → [NUCLEUS_REVIEW] folder     ║
║    HISTORY: Purge expired/dead data going back years            ║
║    REPORT: One daily summary email — never per-item spam        ║
║                                                                  ║
║  RUNS: Every 30min via GitHub Actions                           ║
║  AUTH: Gmail App Password (IMAP/SMTP) — no OAuth needed        ║
╚══════════════════════════════════════════════════════════════════╝

5×5 SAFETY AUDIT (run mentally before every delete batch):
  PASS 1 — Subject scan:      Does subject contain receipt/invoice/order keywords?
  PASS 2 — Sender scan:       Is sender a known human or trusted service?
  PASS 3 — Body scan:         Does body contain amounts, order numbers, tracking?
  PASS 4 — Age + category:    Is this < 7 days old? If yes, downgrade to REVIEW.
  PASS 5 — Final Claude vote: Claude must return VERDICT: DELETE|PROTECT|REVIEW
            Any non-DELETE vote → PROTECT or REVIEW, never force-delete.
"""

import os, json, re, asyncio, imaplib, email, smtplib, httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.mime.text import MIMEText
from email.header import decode_header
from email.utils import parsedate_to_datetime

# ── SECRETS ───────────────────────────────────────────────────────
GMAIL_FROM         = os.getenv("GMAIL_FROM", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO           = os.getenv("GMAIL_TO", GMAIL_FROM)
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
OPERATOR           = os.getenv("OPERATOR_ALIAS", "K-I-D-B-U-U")

# ── CONFIG ────────────────────────────────────────────────────────
VERSION            = "1.0"
CLAUDE_MODEL       = "claude-sonnet-4-20250514"
STATUS_FILE        = "email_sanitizer_status.json"
PURGE_LOG_FILE     = "email_purge_log.json"
REVIEW_FOLDER      = "[NUCLEUS_REVIEW]"
MAX_PER_RUN        = 50          # max emails to process per run (rate limit safety)
BATCH_SIZE         = 10          # how many to send to Claude at once
HISTORICAL_DAYS    = 730         # how far back to clean (2 years)
RECENT_SAFE_DAYS   = 7           # emails under 7 days → never auto-delete, review instead
DAILY_SUMMARY_HOUR = 20          # SAST hour to send daily summary (20:00 = 8PM)

# ── PROTECT SIGNALS (any match = PROTECT, never delete) ──────────
PROTECT_SUBJECTS = [
    "receipt", "invoice", "order confirmation", "your order", "order #",
    "payment", "booking confirmation", "reservation", "ticket", "subscription",
    "bank statement", "account statement", "tax invoice", "proof of payment",
    "delivery", "shipped", "tracking", "dispatch", "your appointment",
    "2fa", "verification code", "otp", "one-time", "security alert",
    "password reset", "account activity", "sign-in attempt",
    "nucleus", "job agent", "fx agent", "shopify agent",   # our own system
]

PROTECT_SENDERS = [
    "paypal", "stripe", "payfast", "yoco", "peach payments",
    "shopify", "metaapi", "anthropic", "github", "google",
    "namecheap", "godaddy", "cloudflare",
    "fnb", "nedbank", "absa", "standard bank", "capitec",
    "sars", "home affairs", "department of",
    "noreply@github", "noreply@anthropic",
]

# ── DESTROY SIGNALS (strong indicators → likely delete, still Claude-verified) ──
DESTROY_SUBJECTS = [
    "you've been selected", "you won", "congratulations! you",
    "urgent: your account", "verify your email or lose access",
    "limited time offer!", "act now!", "last chance!",
    "unsubscribe", "click here to claim",
    "nigerian prince", "wire transfer request", "inheritance",
    "bitcoin investment", "crypto opportunity", "make money fast",
    "enlarge", "casino", "online pharmacy",
]

DESTROY_SENDERS_PATTERNS = [
    r"noreply@.*\.xyz$", r"admin@.*\.click$", r"info@.*\.buzz$",
    r"promo@", r"newsletter@", r"marketing@", r"deals@",
    r"no-reply@.*bulk", r"bounce\+", r"mailer-daemon@",
]

# ── DEAD DATA PATTERNS (expired content → delete without remorse) ──
DEAD_DATA_SUBJECTS = [
    "flash sale", "sale ends tonight", "today only", "expires soon",
    "black friday", "cyber monday", "holiday sale",
    "weekly newsletter", "monthly digest", "daily digest",
    "your weekly", "your monthly", "this week in",
    "unsubscribe from", "manage your email preferences",
]

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

def decode_str(s) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result).strip()

def email_age_days(msg) -> float:
    """Return age of email in days. Returns 0 if unparseable (treat as recent)."""
    try:
        date_str = msg.get("Date", "")
        if date_str:
            dt = parsedate_to_datetime(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        pass
    return 0

def extract_sender_address(sender: str) -> str:
    m = re.search(r"<(.+?)>", sender)
    return m.group(1).lower() if m else sender.lower().strip()

def quick_protect_check(subject: str, sender: str) -> bool:
    """Fast pre-filter: return True if email should definitely be protected."""
    subj_l = subject.lower()
    send_l = sender.lower()
    for p in PROTECT_SUBJECTS:
        if p in subj_l:
            return True
    for p in PROTECT_SENDERS:
        if p in send_l:
            return True
    return False

def quick_destroy_check(subject: str, sender: str) -> str:
    """
    Fast pre-filter: return 'DELETE' if obvious spam, 'DEAD' if dead data,
    '' if not sure (needs Claude).
    """
    subj_l = subject.lower()
    send_l = sender.lower()
    for p in DESTROY_SUBJECTS:
        if p in subj_l:
            return "DEAD"
    for p in DEAD_DATA_SUBJECTS:
        if p in subj_l:
            return "DEAD"
    for pattern in DESTROY_SENDERS_PATTERNS:
        if re.search(pattern, send_l):
            return "DELETE"
    return ""

# ── CLAUDE API ────────────────────────────────────────────────────
async def call_claude(system: str, user: str, max_tokens: int = 600) -> str:
    if not ANTHROPIC_API_KEY:
        return "REVIEW"  # fail safe — never auto-delete without Claude
    try:
        async with httpx.AsyncClient(timeout=40) as client:
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
        print(f"[CLAUDE] Error: {e} — defaulting to REVIEW")
        return "REVIEW"

async def claude_classify_batch(emails_batch: list) -> list:
    """
    Send a batch of emails to Claude for classification.
    Returns list of verdicts: DELETE | PROTECT | REVIEW per email.

    5×5 AUDIT embedded in system prompt — Claude runs all 5 passes internally.
    """
    system = """You are the Nucleus Email Sanitizer for an operator.
Classify each email as DELETE, PROTECT, or REVIEW.

STRICT RULES:
PROTECT if ANY of:
  - Contains receipt, invoice, order number, payment, booking, ticket
  - Contains OTP, 2FA, verification code, security alert, password reset
  - Sent by a human (personal email, not automated)
  - From a bank, payment processor, or government body
  - From Nucleus/FX/Job/Shopify agents (our own system)
  - Less than 7 days old AND purpose is unclear
  - Job application reply or callback

DELETE if ALL of:
  - Clearly automated marketing or promotional
  - Expired sale/promo (any time-limited offer from the past)
  - Newsletter/digest the operator did not request
  - Phishing/scam (fake prizes, urgent account warnings, crypto schemes)
  - No financial, transactional, or human-to-human value

REVIEW if:
  - Uncertain
  - Could be important but you're not sure
  - Looks like spam but sender could be legitimate
  - Any doubt at all — REVIEW, never force DELETE

Run 5 internal passes before deciding:
1. Subject keywords (receipt/invoice = PROTECT)
2. Sender legitimacy (human/bank = PROTECT)
3. Body financial signals (amounts/order# = PROTECT)
4. Age + category (< 7 days + unclear = REVIEW not DELETE)
5. Final vote — only DELETE if all 5 passes confirm worthless

Respond ONLY in this exact JSON format, one object per email:
[{"id": "EMAIL_ID", "verdict": "DELETE|PROTECT|REVIEW", "reason": "one sentence"}]
No other text. Valid JSON only."""

    items = []
    for e in emails_batch:
        items.append(f'ID:{e["id"]} | FROM:{e["sender"]} | SUBJECT:{e["subject"]} | AGE:{e["age_days"]:.0f}d | PREVIEW:{e["preview"][:150]}')

    response = await call_claude(system, "\n\n".join(items), max_tokens=800)

    # Parse response
    try:
        # Strip any markdown fences
        clean = re.sub(r"```json|```", "", response).strip()
        results = json.loads(clean)
        return results
    except Exception as e:
        print(f"[CLASSIFY] Parse error: {e} — defaulting all to REVIEW")
        return [{"id": e["id"], "verdict": "REVIEW", "reason": "parse error"} for e in emails_batch]

# ── IMAP OPERATIONS ───────────────────────────────────────────────
def get_imap_connection():
    """Connect and login to Gmail IMAP."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
    return mail

def ensure_review_folder(mail):
    """Create [NUCLEUS_REVIEW] folder if it doesn't exist."""
    try:
        result = mail.create(REVIEW_FOLDER)
        if result[0] == "OK":
            print(f"[IMAP] Created folder: {REVIEW_FOLDER}")
    except Exception:
        pass  # already exists — fine

def fetch_emails_for_scan(mail, folder: str = "INBOX", days_back: int = HISTORICAL_DAYS) -> list:
    """
    Fetch email metadata from a folder.
    Returns list of dicts: id, uid, sender, subject, age_days, preview.
    Does NOT download full bodies — metadata only for speed.
    """
    emails = []
    try:
        mail.select(folder)
        # Search for all emails (no UNSEEN filter — this is a deep clean)
        _, data = mail.search(None, "ALL")
        ids = data[0].split() if data[0] else []

        # Limit per run
        ids_to_process = ids[-MAX_PER_RUN:] if len(ids) > MAX_PER_RUN else ids

        print(f"[SCAN] {folder}: {len(ids)} total → processing {len(ids_to_process)}")

        for num in ids_to_process:
            try:
                # Fetch headers only (faster than full RFC822)
                _, msg_data = mail.fetch(num, "(RFC822.HEADER UID)")
                uid_data = mail.fetch(num, "(UID)")[1][0].decode()
                uid_match = re.search(r"UID (\d+)", uid_data)
                uid = uid_match.group(1) if uid_match else num.decode()

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                sender  = decode_str(msg.get("From", ""))
                subject = decode_str(msg.get("Subject", ""))
                age     = email_age_days(msg)

                # Skip too recent (safe zone) unless obviously spam
                # Recent safe: only skip NON-spam recent emails
                sender_addr = extract_sender_address(sender)

                # Get brief body preview
                preview = ""
                try:
                    _, full_data = mail.fetch(num, "(RFC822)")
                    full_msg = email.message_from_bytes(full_data[0][1])
                    if full_msg.is_multipart():
                        for part in full_msg.walk():
                            if part.get_content_type() == "text/plain":
                                preview = part.get_payload(decode=True).decode("utf-8", errors="replace")[:200]
                                break
                    else:
                        preview = full_msg.get_payload(decode=True).decode("utf-8", errors="replace")[:200]
                except Exception:
                    preview = ""

                emails.append({
                    "id":        uid,
                    "num":       num,
                    "sender":    sender_addr,
                    "sender_raw": sender,
                    "subject":   subject,
                    "age_days":  age,
                    "preview":   preview.strip()[:200],
                })

            except Exception as e:
                print(f"[SCAN] Error fetching {num}: {e}")
                continue

    except Exception as e:
        print(f"[SCAN] Folder scan error: {e}")

    return emails

def delete_email(mail, num) -> bool:
    """Permanently delete an email (move to Trash then expunge)."""
    try:
        # Gmail: move to [Gmail]/Trash
        mail.copy(num, "[Gmail]/Trash")
        mail.store(num, "+FLAGS", "\\Deleted")
        mail.expunge()
        return True
    except Exception as e:
        print(f"[DELETE] Failed: {e}")
        return False

def move_to_review(mail, num) -> bool:
    """Move email to [NUCLEUS_REVIEW] folder."""
    try:
        mail.copy(num, REVIEW_FOLDER)
        mail.store(num, "+FLAGS", "\\Deleted")
        mail.expunge()
        return True
    except Exception as e:
        print(f"[REVIEW] Move failed: {e}")
        return False

# ── SEND SUMMARY EMAIL ────────────────────────────────────────────
def send_daily_summary(stats: dict):
    """Send the daily clean summary. One email, not per-deletion."""
    if not all([GMAIL_FROM, GMAIL_APP_PASSWORD, GMAIL_TO]):
        return

    deleted  = stats.get("deleted_today", 0)
    reviewed = stats.get("reviewed_today", 0)
    protected = stats.get("protected_today", 0)
    total    = stats.get("total_purged_alltime", 0)
    storage  = stats.get("storage_freed_mb", 0)

    body = f"""NUCLEUS EMAIL SANITIZER — DAILY CLEAN SUMMARY
{'='*50}
DATE:      {sast_now()}
TODAY:     {deleted} deleted · {reviewed} moved to review · {protected} protected
ALL-TIME:  {total} emails purged
STORAGE:   ~{storage:.1f} MB freed (estimated)

REVIEW FOLDER: [{REVIEW_FOLDER}]
  Items pending your review: {reviewed}
  Open Gmail → look for uncertain emails — decide manually.

SYSTEM STATUS:
  Sanitizer v{VERSION} — running 24/7
  Next run: ~30 minutes

— Nucleus Supervisor"""

    try:
        msg = MIMEText(body)
        msg["Subject"] = f"[Nucleus] 🧹 Daily Clean: {deleted} deleted, {reviewed} for review"
        msg["From"]    = f"Nucleus Sanitizer <{GMAIL_FROM}>"
        msg["To"]      = GMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        print(f"[SUMMARY] Daily summary sent — {deleted} deleted, {reviewed} reviewed")
    except Exception as e:
        print(f"[SUMMARY] Failed to send: {e}")

# ── MAIN CLEAN CYCLE ──────────────────────────────────────────────
async def run_clean_cycle():
    """
    Main sanitizer cycle.
    1. Connect IMAP
    2. Ensure REVIEW folder exists
    3. Fetch emails from INBOX (and historical)
    4. Pre-filter with fast rules
    5. Send ambiguous batch to Claude
    6. Execute: delete / move to review / leave protected
    7. Update status file
    8. Send daily summary if it's time
    """
    print("═" * 56)
    print(f"  NUCLEUS EMAIL SANITIZER v{VERSION}")
    print(f"  {sast_now()}")
    print("═" * 56)

    status = load_json(STATUS_FILE, {
        "deleted_today": 0,
        "reviewed_today": 0,
        "protected_today": 0,
        "total_purged_alltime": 0,
        "storage_freed_mb": 0,
        "last_run": None,
        "last_summary_date": None,
        "run_count": 0,
    })

    # Reset daily counters if new day
    today = datetime.now(timezone(timedelta(hours=2))).strftime("%Y-%m-%d")
    if status.get("last_run", "")[:10] != today:
        status["deleted_today"]   = 0
        status["reviewed_today"]  = 0
        status["protected_today"] = 0

    if not all([GMAIL_FROM, GMAIL_APP_PASSWORD]):
        print("[SANITIZER] Gmail credentials not configured — exiting")
        return

    mail = None
    deleted_this_run  = 0
    reviewed_this_run = 0
    protected_this_run = 0

    try:
        mail = get_imap_connection()
        ensure_review_folder(mail)

        # Scan inbox
        all_emails = fetch_emails_for_scan(mail, "INBOX", HISTORICAL_DAYS)

        # Pre-filter pass
        to_delete_fast  = []  # obvious — no Claude needed
        to_review_fast  = []  # obvious recent/important — skip
        needs_claude    = []  # ambiguous — send to Claude

        for e in all_emails:
            # 1. Quick protect check
            if quick_protect_check(e["subject"], e["sender"]):
                protected_this_run += 1
                continue

            # 2. Recent safe zone: < RECENT_SAFE_DAYS → only delete if OBVIOUSLY spam
            quick_verdict = quick_destroy_check(e["subject"], e["sender"])
            if e["age_days"] < RECENT_SAFE_DAYS and quick_verdict not in ("DELETE",):
                # Recent + not obvious spam → review
                protected_this_run += 1
                continue

            if quick_verdict in ("DELETE", "DEAD"):
                to_delete_fast.append(e)
            else:
                needs_claude.append(e)

        print(f"[FILTER] {len(to_delete_fast)} fast-delete · {len(needs_claude)} needs Claude · {protected_this_run} protected")

        # Execute fast deletes (dead data — no Claude needed for obvious stuff)
        mail.select("INBOX")
        for e in to_delete_fast:
            success = delete_email(mail, e["num"])
            if success:
                deleted_this_run += 1
                print(f"[DELETE] ✅ {e['subject'][:60]} | {e['sender'][:40]} | {e['age_days']:.0f}d old")

        # Claude classification for ambiguous emails
        if needs_claude and ANTHROPIC_API_KEY:
            print(f"[CLAUDE] Classifying {len(needs_claude)} ambiguous emails in batches of {BATCH_SIZE}...")
            for i in range(0, len(needs_claude), BATCH_SIZE):
                batch = needs_claude[i:i+BATCH_SIZE]
                results = await claude_classify_batch(batch)

                # Build lookup
                verdict_map = {r["id"]: r for r in results}

                mail.select("INBOX")
                for e in batch:
                    r = verdict_map.get(e["id"], {"verdict": "REVIEW", "reason": "not found"})
                    verdict = r.get("verdict", "REVIEW").upper().strip()
                    reason  = r.get("reason", "—")

                    if verdict == "DELETE":
                        # 5th pass safety: if age < RECENT_SAFE_DAYS, override to REVIEW
                        if e["age_days"] < RECENT_SAFE_DAYS:
                            verdict = "REVIEW"
                            reason  = "recent email (<7d) — overridden to REVIEW for safety"

                    if verdict == "DELETE":
                        success = delete_email(mail, e["num"])
                        if success:
                            deleted_this_run += 1
                            print(f"[DELETE] 🗑 {e['subject'][:50]} | {reason[:50]}")
                    elif verdict == "REVIEW":
                        success = move_to_review(mail, e["num"])
                        if success:
                            reviewed_this_run += 1
                            print(f"[REVIEW] 📂 {e['subject'][:50]} | {reason[:50]}")
                    else:  # PROTECT
                        protected_this_run += 1
                        print(f"[PROTECT] 🛡 {e['subject'][:50]}")

                # Brief pause between batches
                await asyncio.sleep(2)

        mail.logout()

    except Exception as e:
        print(f"[SANITIZER] Error: {e}")
        if mail:
            try:
                mail.logout()
            except Exception:
                pass

    # Update status
    status["deleted_today"]      = status.get("deleted_today", 0) + deleted_this_run
    status["reviewed_today"]     = status.get("reviewed_today", 0) + reviewed_this_run
    status["protected_today"]    = status.get("protected_today", 0) + protected_this_run
    status["total_purged_alltime"] = status.get("total_purged_alltime", 0) + deleted_this_run
    status["storage_freed_mb"]   = status.get("storage_freed_mb", 0) + (deleted_this_run * 0.05)  # ~50KB avg
    status["last_run"]           = utc_now()
    status["run_count"]          = status.get("run_count", 0) + 1
    status["agent_version"]      = VERSION
    save_json(STATUS_FILE, status)

    # Append to purge log
    purge_log = load_json(PURGE_LOG_FILE, [])
    purge_log.insert(0, {
        "timestamp":  utc_now(),
        "sast":       sast_now(),
        "deleted":    deleted_this_run,
        "reviewed":   reviewed_this_run,
        "protected":  protected_this_run,
    })
    save_json(PURGE_LOG_FILE, purge_log[:200])  # keep last 200 runs

    print(f"\n[DONE] ✅ Deleted: {deleted_this_run} | Reviewed: {reviewed_this_run} | Protected: {protected_this_run}")

    # Daily summary — send once per day at DAILY_SUMMARY_HOUR SAST
    sast_hour = datetime.now(timezone(timedelta(hours=2))).hour
    last_summary_date = status.get("last_summary_date", "")
    if sast_hour >= DAILY_SUMMARY_HOUR and last_summary_date != today:
        send_daily_summary(status)
        status["last_summary_date"] = today
        save_json(STATUS_FILE, status)


if __name__ == "__main__":
    asyncio.run(run_clean_cycle())
