import os, re, sys, json, html, getpass, smtplib, time
from pathlib import Path
from email.mime.text import MIMEText

# ─── STEP 1: Load credentials ────────────────────────────────────────────────
# On Railway (cloud), credentials come from environment variables set in the
# dashboard. Locally, we load them from .env so the script runs without
# any manual setup. The .env file is gitignored and never committed.
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)  # maxsplit=1 handles passwords containing "="
            os.environ.setdefault(_k.strip(), _v.strip())

import anthropic
from piazza_api import Piazza

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
SYLLABUS_PATH = os.environ.get("SYLLABUS_PATH", "syllabus.txt")
GATE_MODEL    = "claude-haiku-4-5"   # cheapest Claude model; ideal for classification
POLL_LIMIT    = 30                   # number of recent posts to scan each cycle
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))   # seconds between scans
SEEN_FILE     = os.environ.get("SEEN_FILE", "seen.json")      # deduplication log

# ─── STEP 2: The content gate ────────────────────────────────────────────────
# This is the bot's only AI call. It classifies every question into one of four
# buckets. Crucially, it is syllabus-grounded: the model may not invent facts or
# answer anything outside the provided syllabus text.
#
# Pseudocode:
#   given question + syllabus →
#     "syllabus"   → logistics question with a clear syllabus answer → BOT ANSWERS
#     "content"    → course subject matter (concepts, hw, exams)    → SKIP (human)
#     "not_found"  → logistics question NOT in syllabus             → SKIP (human)
#     "skip"       → greeting / noise                               → IGNORE
GATE_SYSTEM = """You triage questions posted to a college course forum. You answer ONLY logistical / syllabus / policy questions, using ONLY the syllabus provided, and you stay out of course content.

Respond with ONLY a raw JSON object, no markdown or code fences:
{{"category": "...", "answer": "...", "source": "...", "evidence": "..."}}

category is exactly one of:
- "syllabus": a logistics/policy/schedule/grading/materials question whose answer is in the syllabus. "answer" = concise friendly answer (exact dates/times/points). "source" = the syllabus section name. "evidence" = the verbatim sentence(s) copied EXACTLY from the syllabus that justify the answer.
- "content": about course subject matter / homework / exam problems / concept explanations. Do not answer. Empty answer/source/evidence.
- "not_found": a reasonable logistics question not covered by the syllabus. Empty answer/source/evidence.
- "skip": greeting, thanks, or noise. Empty fields.

Rules: never invent facts; if a logistics answer isn't clearly in the syllabus use "not_found"; when unsure whether something is content use "content". The "evidence" must be text that actually appears in the syllabus.

SYLLABUS:
{syllabus}"""


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def load_syllabus():
    """Read syllabus.txt — the bot's only allowed knowledge source."""
    if not os.path.exists(SYLLABUS_PATH):
        sys.exit(f"ERROR: {SYLLABUS_PATH} not found.")
    with open(SYLLABUS_PATH, encoding="utf-8") as f:
        return f.read().strip()

def load_seen():
    """Load the set of already-processed post numbers from disk."""
    p = Path(SEEN_FILE)
    return set(json.loads(p.read_text())) if p.exists() else set()

def save_seen(seen):
    """Persist the seen-post set so deduplication survives restarts."""
    Path(SEEN_FILE).write_text(json.dumps(sorted(seen)))

def strip_html(raw):
    """Remove HTML tags and unescape entities for plain-text comparison."""
    return html.unescape(re.sub(r"<[^>]+>", " ", raw or "")).strip()

def question_text_of(post):
    """Extract subject + body from the most recent revision of a post."""
    hist = (post.get("history") or [{}])[0]
    return f"{strip_html(hist.get('subject',''))}\n{strip_html(hist.get('content',''))}".strip()

def needs_answer(post):
    """Return True only if this is an open question with no instructor/student answer yet."""
    if post.get("type") != "question":
        return False
    for child in post.get("children", []):
        # i_answer = instructor answer, s_answer = student answer (what we post as)
        if child.get("type") in ("i_answer", "s_answer"):
            return False
    return True


# ─── STEP 3: Call the gate ────────────────────────────────────────────────────

def gate(client, syllabus, question):
    """
    Send question + syllabus to Claude Haiku and parse the JSON classification.

    Returns a dict with keys: category, answer, source, evidence.
    Falls back to {"category": "skip"} on any parse failure.
    """
    resp = client.messages.create(
        model=GATE_MODEL,
        max_tokens=700,
        system=GATE_SYSTEM.format(syllabus=syllabus),
        messages=[{"role": "user", "content": question}]
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    # Strip accidental markdown code fences the model may include
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Last-resort: extract the first JSON object from the response
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return {"category": "skip"}


# ─── STEP 4: Post answer to Piazza ───────────────────────────────────────────

def post_answer(network, post, answer):
    """
    Post the answer as a student answer (s_answer type).

    Using s_answer instead of a followup means Piazza's needs_answer() check
    will return False for this post on subsequent scans — preventing double-posts
    on restart even before seen.json is consulted.

    Falls back to create_followup() if the account lacks s_answer permission.
    """
    cid = post.get("id")
    try:
        network._rpc.content_student_answer(cid, answer, revision=0)
        return "s_answer"
    except Exception:
        network.create_followup(post, answer)
        return "followup"

def send_confirmation(nr, question, answer, source, post_type):
    """Email the instructor a summary of every answer the bot posts."""
    sender   = os.environ.get("NOTIFY_EMAIL", "")
    app_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not sender or not app_pass:
        return
    subject = f"Piazza Bot Posted: @{nr} — {question[:55].replace(chr(10), ' ')}"
    body = (
        f"The bot just posted an answer on Piazza.\n\n"
        f"Post: @{nr}\n"
        f"Type: {post_type}\n\n"
        f"QUESTION:\n{question}\n\n"
        f"ANSWER POSTED:\n{answer}\n\n"
        f"SOURCE: {source}\n"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = sender
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(sender, app_pass)
            s.send_message(msg)
        print("  [confirmation email sent]")
    except Exception as e:
        print(f"  [email failed: {e}]")


# ─── STEP 5: The scan loop ────────────────────────────────────────────────────

def scan_once(network, client, syllabus, seen):
    """
    Single scan pass — the core of the bot.

    Pseudocode for each post:
      1. Skip if already processed (seen.json)
      2. Skip if it already has an answer (needs_answer check)
      3. Extract plain-text question
      4. Call gate() → classify: syllabus / content / not_found / skip
      5. If "syllabus" and answer exists → post to Piazza + send email
      6. Otherwise → leave for instructors/TAs
    """
    posted = 0
    for post in network.iter_all_posts(limit=POLL_LIMIT):
        nr = post.get("nr", "?")
        if nr in seen:
            continue

        # Mark seen BEFORE posting — prevents a double-post if the script
        # crashes mid-answer and restarts before seen.json is flushed.
        seen.add(nr)

        if not needs_answer(post):
            continue

        q = question_text_of(post)
        if not q:
            continue

        r   = gate(client, syllabus, q)
        cat = r.get("category", "skip")
        print(f"  @{nr} [{cat}] {q[:65].replace(chr(10), ' ')}")

        if cat == "syllabus" and r.get("answer"):
            answer = r["answer"]
            source = r.get("source", "")
            try:
                ptype = post_answer(network, post, answer)
                posted += 1
                print(f"    ✓ posted ({ptype}): {answer[:100]}")
                send_confirmation(nr, q, answer, source, ptype)
            except Exception as e:
                print(f"    ✗ post failed: {e}")
        elif cat == "content":
            print("    -> course content, skipping (leave for instructor/TAs)")
        elif cat == "not_found":
            print("    -> not in syllabus, skipping (leave for instructor/TAs)")

    return posted


# ─── MAIN: startup and polling loop ───────────────────────────────────────────

def main():
    # Accept PIAZZA_NETWORK_COGS9 or PIAZZA_NETWORK (either naming works)
    if os.environ.get("PIAZZA_NETWORK_COGS9") and not os.environ.get("PIAZZA_NETWORK"):
        os.environ["PIAZZA_NETWORK"] = os.environ["PIAZZA_NETWORK_COGS9"]

    for var in ("ANTHROPIC_API_KEY", "PIAZZA_EMAIL", "PIAZZA_PASSWORD", "PIAZZA_NETWORK"):
        if not os.environ.get(var):
            sys.exit(f"ERROR: {var} (or PIAZZA_NETWORK_COGS9) is not set.")

    syllabus = load_syllabus()
    client   = anthropic.Anthropic()

    # Login to Piazza once; the session persists for the lifetime of the process
    p = Piazza()
    try:
        p.user_login(email=os.environ["PIAZZA_EMAIL"], password=os.environ["PIAZZA_PASSWORD"])
    except Exception as e:
        sys.exit(f"Piazza login failed: {e}")

    network = p.network(os.environ["PIAZZA_NETWORK"])
    seen    = load_seen()

    print(f"Connected. Polling every {POLL_INTERVAL}s. Ctrl+C to stop.\n")

    # Main loop: scan → sleep → repeat
    while True:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] Scanning...")
        try:
            n = scan_once(network, client, syllabus, seen)
            save_seen(seen)
            print(f"  → {n} posted." if n else "  → Nothing new.")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"  [scan error: {e}]")
        print(f"  Next scan in {POLL_INTERVAL}s.\n")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
