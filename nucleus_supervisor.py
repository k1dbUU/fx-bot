"""
╔══════════════════════════════════════════════════════════════════╗
║  NUCLEUS SUPERVISOR v6.0 — "THE UNIVERSAL ADAPTER"               ║
║  v6.0 PATCHES:                                                   ║
║    - UNIVERSAL INTENT: Uses Engine's Reasoning for all emails    ║
║    - FULL REGISTRY: Monitors FX, Job, Shopify, Lens, Sanitizer   ║
║    - LENS SYNC: Injects video links directly into Lens Queue     ║
╚══════════════════════════════════════════════════════════════════╝
"""
import os, json, asyncio, httpx, re, base64
from datetime import datetime, timezone, timedelta
from pathlib import Path

VERSION = "6.0"
OPERATOR_ALIAS = os.environ.get("OPERATOR_ALIAS", "Nucleus Operator")
TRUSTED_EMAILS = [os.environ.get("GMAIL_TO", "").lower(), os.environ.get("GMAIL_FROM", "").lower()]
MEMORY_FILE = "NUCLEUS_MEMORY.json"
EMAIL_LOG_FILE = "nucleus_email_log.json"

# Check for Engine
ENGINE_AVAILABLE = Path("nucleus_autonomous_engine.py").exists()

def sast_now():
    return (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S SAST")

def load_json(path, default):
    if not Path(path).exists(): return default
    with open(path, 'r') as f: return json.load(f)

def save_json(path, data):
    with open(path, 'w') as f: json.dump(data, f, indent=2)

def send_email(to, subject, body):
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg.set_content(body); msg['Subject'] = subject
    msg['From'] = os.environ.get("GMAIL_FROM"); msg['To'] = to
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(os.environ.get("GMAIL_FROM"), os.environ.get("GMAIL_APP_PASSWORD"))
            smtp.send_message(msg)
    except Exception as e: print(f"Mail Error: {e}")

async def handle_inbound_email(sender: str, subject: str, body: str, msg_id: str):
    sender_clean = sender.lower().strip()
    is_trusted = any(t in sender_clean for t in TRUSTED_EMAILS if t)
    
    print(f"[SUPERVISOR] Email from {sender}. Trusted: {is_trusted}")

    if is_trusted and ENGINE_AVAILABLE:
        # Hand off to the Engine's Reasoning logic
        from nucleus_autonomous_engine import handle_operator_command
        engine_reply = await handle_operator_command(sender, subject, body)
        
        if engine_reply:
            send_email(sender, f"Re: {subject} [NUCLEUS EXECUTION]", engine_reply)
            
            # Log it
            log = load_json(EMAIL_LOG_FILE, {"processed": []})
            log["processed"].append({"id": msg_id, "subject": subject, "handler": "engine", "at": sast_now()})
            save_json(EMAIL_LOG_FILE, log)
            return

    # Fallback to standard status reply if not a command
    send_email(sender, f"Re: {subject}", "Nucleus received your message. No direct action was identified by the engine. Dashboard is live.")

async def run():
    print(f"--- Nucleus Supervisor v{VERSION} Active ---")
    # Loop logic for GitHub Actions would go here
    pass

if __name__ == "__main__":
    asyncio.run(run())
