import os, re, sys, json, html, getpass, smtplib, time
from pathlib import Path
from email.mime.text import MIMEText

# Load .env from project root (local runs only — cloud sets env vars directly)
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import anthropic
from piazza_api import Piazza

SYLLABUS_PATH = os.environ.get("SYLLABUS_PATH", "syllabus.txt")
GATE_MODEL    = "claude-haiku-4-5"
POLL_LIMIT    = 30
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))
SEEN_FILE     = os.environ.get("SEEN_FILE", "seen.json")

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


def load_syllabus():
    if not os.path.exists(SYLLABUS_PATH):
        sys.exit(f"ERROR: {SYLLABUS_PATH} not found.")
    with open(SYLLABUS_PATH, encoding="utf-8") as f:
        return f.read().strip()

def load_seen():
    p = Path(SEEN_FILE)
    return set(json.loads(p.read_text())) if p.exists() else set()

def save_seen(seen):
    Path(SEEN_FILE).write_text(json.dumps(sorted(seen)))

def strip_html(raw):
    return html.unescape(re.sub(r"<[^>]+>", " ", raw or "")).strip()

def question_text_of(post):
    hist = (post.get("history") or [{}])[0]
    return f"{strip_html(hist.get('subject',''))}\n{strip_html(hist.get('content',''))}".strip()

def needs_answer(post):
    if post.get("type") != "question":
        return False
    for child in post.get("children", []):
        if child.get("type") in ("i_answer", "s_answer"):
            return False
    return True

def gate(client, syllabus, question):
    resp = client.messages.create(model=GATE_MODEL, max_tokens=700,
        system=GATE_SYSTEM.format(syllabus=syllabus),
        messages=[{"role": "user", "content": question}])
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return {"category": "skip"}

def post_answer(network, post, answer):
    """Post answer as a student answer (s_answer). Falls back to followup if permission denied."""
    cid = post.get("id")
    try:
        network._rpc.content_student_answer(cid, answer, revision=0)
        return "s_answer"
    except Exception:
        network.create_followup(post, answer)
        return "followup"

def send_confirmation(nr, question, answer, source, post_type):
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

def scan_once(network, client, syllabus, seen):
    posted = 0
    for post in network.iter_all_posts(limit=POLL_LIMIT):
        nr = post.get("nr", "?")
        if nr in seen:
            continue
        # Mark seen immediately — prevents double-posting on restart since
        # s_answer posts are detected by needs_answer() on subsequent scans.
        seen.add(nr)
        if not needs_answer(post):
            continue
        q = question_text_of(post)
        if not q:
            continue
        r    = gate(client, syllabus, q)
        cat  = r.get("category", "skip")
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
            print("    -> content, skipping")
        elif cat == "not_found":
            print("    -> not in syllabus, skipping")
    return posted

def main():
    for var in ("ANTHROPIC_API_KEY", "PIAZZA_EMAIL", "PIAZZA_PASSWORD", "PIAZZA_NETWORK"):
        if not os.environ.get(var):
            sys.exit(f"ERROR: {var} is not set.")

    syllabus = load_syllabus()
    client   = anthropic.Anthropic()

    p = Piazza()
    try:
        p.user_login(email=os.environ["PIAZZA_EMAIL"], password=os.environ["PIAZZA_PASSWORD"])
    except Exception as e:
        sys.exit(f"Piazza login failed: {e}")

    network = p.network(os.environ["PIAZZA_NETWORK"])
    seen    = load_seen()

    print(f"Connected. Polling every {POLL_INTERVAL}s. Ctrl+C to stop.\n")

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
