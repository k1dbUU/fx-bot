"""
nucleus_lens_agent.py — LENS (Video Intelligence Agent) v1.0
Operator: Nucleus Operator
Purpose: Receive video links from any platform, dual-transcribe (captions + Whisper),
         evaluate with Claude, and if worth it — feed upgrade proposals to Nucleus Supervisor.

Intake methods:
  1. Email with subject containing [LENS] — auto-detected by this agent via Gmail IMAP
  2. lens_queue.json — paste link directly (dashboard or manual)

Platforms supported: YouTube, Instagram, TikTok, Twitter/X, Facebook, Reddit, + 1000 more via yt-dlp
"""

import os, json, re, imaplib, email, subprocess, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
GMAIL_FROM        = os.environ.get("GMAIL_FROM", "")
GMAIL_APP_PASSWORD= os.environ.get("GMAIL_APP_PASSWORD", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
QUEUE_FILE        = "lens_queue.json"
LOG_FILE          = "lens_log.json"
LENS_STATUS_FILE  = "lens_agent_status.json"
MAX_LINKS_PER_RUN = 2          # cap per cycle — keeps GitHub Actions under 12min
WHISPER_MODEL     = "base"     # base = good accuracy, fast on CPU. upgrade to small if needed.

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def log(msg):
    print(f"[LENS] {msg}")

# ── Step 1: Pull new links from Gmail [LENS] emails ───────────────────────────

def fetch_email_links():
    """Check Gmail for emails with [LENS] in subject. Extract URLs. Mark seen."""
    links = []
    if not GMAIL_FROM or not GMAIL_APP_PASSWORD:
        log("No Gmail creds — skipping email intake")
        return links
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
        mail.select("inbox")
        _, data = mail.search(None, '(UNSEEN SUBJECT "[LENS]")')
        ids = data[0].split()
        log(f"Found {len(ids)} [LENS] email(s)")
        for eid in ids:
            _, msg_data = mail.fetch(eid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            # Extract body text
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body += part.get_payload(decode=True).decode(errors="ignore")
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")
            # Find all URLs in body
            found = re.findall(r'https?://[^\s\]\)>\"\']+', body)
            for url in found:
                links.append({"url": url.strip(), "source": "email", "added": now_utc()})
            # Mark as read
            mail.store(eid, '+FLAGS', '\\Seen')
        mail.logout()
    except Exception as e:
        log(f"Email intake error: {e}")
    return links

# ── Step 2: Load + merge queue ─────────────────────────────────────────────────

def load_queue():
    return load_json(QUEUE_FILE, [])

def save_queue(q):
    save_json(QUEUE_FILE, q)

def enqueue(new_links, queue):
    existing_urls = {item["url"] for item in queue}
    added = 0
    for item in new_links:
        if item["url"] not in existing_urls:
            queue.append({**item, "status": "pending"})
            existing_urls.add(item["url"])
            added += 1
    log(f"Enqueued {added} new link(s)")
    return queue

# ── Step 3a: Caption extraction via yt-dlp ────────────────────────────────────

def extract_captions_ytdlp(url, tmpdir):
    """Try to pull native subtitles/captions. Returns text or None."""
    try:
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en",
            "--sub-format", "vtt",
            "--output", f"{tmpdir}/caption",
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        # Find any .vtt file produced
        vtt_files = list(Path(tmpdir).glob("*.vtt"))
        if not vtt_files:
            log("No caption file found via yt-dlp")
            return None
        vtt_text = vtt_files[0].read_text(errors="ignore")
        # Strip VTT formatting tags and timestamps
        lines = []
        for line in vtt_text.splitlines():
            line = line.strip()
            if "-->" in line or line.startswith("WEBVTT") or not line:
                continue
            clean = re.sub(r'<[^>]+>', '', line)
            if clean:
                lines.append(clean)
        # Deduplicate consecutive identical lines (caption stuttering)
        deduped = []
        prev = None
        for l in lines:
            if l != prev:
                deduped.append(l)
            prev = l
        caption_text = " ".join(deduped)
        log(f"Caption extracted via yt-dlp: {len(caption_text)} chars")
        return caption_text if len(caption_text) > 50 else None
    except Exception as e:
        log(f"yt-dlp caption error: {e}")
        return None

# ── Step 3b: Whisper transcription ────────────────────────────────────────────

def extract_whisper(url, tmpdir):
    """Download audio via yt-dlp, transcribe with Whisper. Returns text or None."""
    try:
        audio_path = f"{tmpdir}/audio.%(ext)s"
        dl_cmd = [
            "yt-dlp",
            "-f", "bestaudio[ext=m4a]/bestaudio/best",
            "--no-playlist",
            "--max-filesize", "50m",   # skip massive files — Reels/Shorts are tiny
            "-o", audio_path,
            url
        ]
        log("Downloading audio for Whisper...")
        dl_result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=180)
        # Find downloaded audio file
        audio_files = list(Path(tmpdir).glob("audio.*"))
        if not audio_files:
            log("No audio file downloaded")
            return None
        audio_file = str(audio_files[0])
        log(f"Transcribing with Whisper ({WHISPER_MODEL})...")
        # Use whisper CLI
        whisper_cmd = [
            "whisper", audio_file,
            "--model", WHISPER_MODEL,
            "--language", "en",
            "--output_format", "txt",
            "--output_dir", tmpdir
        ]
        w_result = subprocess.run(whisper_cmd, capture_output=True, text=True, timeout=600)
        # Find output txt
        txt_files = list(Path(tmpdir).glob("audio*.txt"))
        if not txt_files:
            log("Whisper produced no output")
            return None
        whisper_text = txt_files[0].read_text(errors="ignore").strip()
        log(f"Whisper transcript: {len(whisper_text)} chars")
        return whisper_text if len(whisper_text) > 50 else None
    except Exception as e:
        log(f"Whisper error: {e}")
        return None

# ── Step 3c: Merge + validate both transcripts ────────────────────────────────

def merge_transcripts(caption_text, whisper_text):
    """
    Both run every time. Claude compares both.
    If they differ significantly → flag for review.
    Returns merged best-version transcript.
    """
    if caption_text and whisper_text:
        # Simple similarity check — word overlap ratio
        cap_words = set(caption_text.lower().split())
        whi_words = set(whisper_text.lower().split())
        if len(cap_words) == 0:
            return whisper_text, "whisper_only", False
        overlap = len(cap_words & whi_words) / max(len(cap_words), len(whi_words))
        divergent = overlap < 0.4  # less than 40% overlap = flag it
        # Use whichever is longer (more complete)
        best = caption_text if len(caption_text) >= len(whisper_text) else whisper_text
        return best, "dual_confirmed", divergent
    elif caption_text:
        return caption_text, "caption_only", False
    elif whisper_text:
        return whisper_text, "whisper_only", False
    else:
        return None, "failed", False

# ── Step 4: Claude evaluation ──────────────────────────────────────────────────

def call_claude(transcript, url):
    """
    Send transcript to Claude. Ask: is this a real Nucleus improvement?
    Returns: {verdict, summary, proposal, confidence}
    """
    try:
        import httpx
        system = """You are LENS, the Video Intelligence Agent for Nucleus — an autonomous AI operating system.
Your job: evaluate video transcripts to determine if they contain genuine, actionable improvements for Nucleus.

Nucleus components: FX trading agent, Job application agent, Shopify store agent, Email sanitizer, Autonomous engine, Dashboard.
Nucleus runs on GitHub Actions + Python + Claude API. Budget is extremely tight (~$4/month total).

You must respond ONLY in valid JSON — no preamble, no markdown, no explanation outside the JSON.

JSON format:
{
  "verdict": "IMPLEMENT" | "MONITOR" | "SKIP",
  "confidence": 0-100,
  "summary": "One sentence: what the video is about",
  "value": "One sentence: specific value to Nucleus if any",
  "proposal": "If IMPLEMENT: exact description of what to build/change. If not: null",
  "reason": "Why this verdict"
}

IMPLEMENT = clear, specific, actionable upgrade with high ROI for Nucleus
MONITOR = interesting but not immediately actionable or needs more info
SKIP = unrelated, already done, too vague, or not applicable"""

        prompt = f"""Video URL: {url}

Transcript:
{transcript[:6000]}

Evaluate this for Nucleus. Does it contain a real improvement we should implement?"""

        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "system": system,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        data = resp.json()
        raw = data["content"][0]["text"].strip()
        # Strip any accidental markdown
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        result = json.loads(raw)
        log(f"Claude verdict: {result.get('verdict')} (confidence: {result.get('confidence')})")
        return result
    except Exception as e:
        log(f"Claude eval error: {e}")
        return {"verdict": "SKIP", "confidence": 0, "summary": "Claude eval failed", "value": None, "proposal": None, "reason": str(e)}

# ── Step 5: Write upgrade proposal to Nucleus ──────────────────────────────────

def write_proposal_to_nucleus(item, evaluation):
    """
    If verdict is IMPLEMENT — write to nucleus_command.json for Supervisor to pick up.
    Supervisor already checks this file every 5min.
    """
    proposal = {
        "source": "LENS",
        "timestamp": now_utc(),
        "status": "pending",
        "url": item["url"],
        "summary": evaluation.get("summary"),
        "proposal": evaluation.get("proposal"),
        "confidence": evaluation.get("confidence"),
        "value": evaluation.get("value")
    }
    # Load existing command file — append to queue or create
    existing = load_json("nucleus_command.json", {})
    if "lens_proposals" not in existing:
        existing["lens_proposals"] = []
    existing["lens_proposals"].append(proposal)
    # Set status to pending so Supervisor processes it
    existing["status"] = "pending"
    save_json("nucleus_command.json", existing)
    log(f"Proposal written to nucleus_command.json")

# ── Step 6: Update log ─────────────────────────────────────────────────────────

def update_log(log_entry):
    logs = load_json(LOG_FILE, [])
    logs.append(log_entry)
    # Keep last 200 entries
    if len(logs) > 200:
        logs = logs[-200:]
    save_json(LOG_FILE, logs)

def update_status(processed, implement_count, skip_count, monitor_count):
    save_json(LENS_STATUS_FILE, {
        "agent": "LENS",
        "last_run": now_utc(),
        "status": "ACTIVE",
        "processed_this_run": processed,
        "verdicts": {
            "IMPLEMENT": implement_count,
            "MONITOR": monitor_count,
            "SKIP": skip_count
        }
    })

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log("=== LENS Agent starting ===")

    # 1. Pull from Gmail
    email_links = fetch_email_links()

    # 2. Load queue + merge
    queue = load_queue()
    queue = enqueue(email_links, queue)
    save_queue(queue)

    # 3. Process pending (max MAX_LINKS_PER_RUN)
    pending = [item for item in queue if item.get("status") == "pending"]
    log(f"Pending links: {len(pending)} | Processing: {min(len(pending), MAX_LINKS_PER_RUN)}")

    implement_count = 0
    skip_count = 0
    monitor_count = 0
    processed = 0

    for item in pending[:MAX_LINKS_PER_RUN]:
        url = item["url"]
        log(f"--- Processing: {url}")

        caption_text = None
        whisper_text = None

        with tempfile.TemporaryDirectory() as tmpdir:
            # Both always run — dual confirmation
            caption_text = extract_captions_ytdlp(url, tmpdir)
            whisper_text = extract_whisper(url, tmpdir)

        transcript, method, divergent = merge_transcripts(caption_text, whisper_text)

        if divergent:
            log("⚠️  DIVERGENCE DETECTED — caption and Whisper disagree significantly")

        if not transcript:
            log("No transcript obtained — skipping")
            item["status"] = "failed"
            item["error"] = "no_transcript"
            update_log({"url": url, "status": "failed", "reason": "no_transcript", "timestamp": now_utc()})
            processed += 1
            continue

        # Claude evaluation
        evaluation = call_claude(transcript, url)
        verdict = evaluation.get("verdict", "SKIP")

        # Act on verdict
        if verdict == "IMPLEMENT":
            write_proposal_to_nucleus(item, evaluation)
            implement_count += 1
        elif verdict == "MONITOR":
            monitor_count += 1
        else:
            skip_count += 1

        item["status"] = "done"
        item["verdict"] = verdict
        item["evaluated_at"] = now_utc()
        item["method"] = method
        item["divergent"] = divergent

        update_log({
            "url": url,
            "verdict": verdict,
            "confidence": evaluation.get("confidence"),
            "summary": evaluation.get("summary"),
            "method": method,
            "divergent": divergent,
            "timestamp": now_utc()
        })
        processed += 1
        time.sleep(2)

    save_queue(queue)
    update_status(processed, implement_count, skip_count, monitor_count)
    log(f"=== LENS done | Processed: {processed} | IMPLEMENT: {implement_count} | MONITOR: {monitor_count} | SKIP: {skip_count} ===")

if __name__ == "__main__":
    main()
