import os, re, sys, json, html, getpass, smtplib, time
from pathlib import Path
from email.mime.text import MIMEText

# Load .env from project root so credentials never need to be set manually
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import anthropic
from piazza_api import Piazza

SYLLABUS_PATH = "syllabus.txt"
GATE_MODEL = "claude-haiku-4-5"
POLL_LIMIT = 30
POLL_INTERVAL = 120          # seconds between scans
OUTPUT_FILE = "drafts.txt"
SEEN_FILE = "seen.json"      # tracks processed post numbers across restarts

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
        sys.exit(f"ERROR: {SYLLABUS_PATH} not found in this folder.")
    with open(SYLLABUS_PATH, encoding="utf-8") as f:
        return f.read().strip()

def load_seen():
    p = Path(SEEN_FILE)
    if p.exists():
        return set(json.loads(p.read_text()))
    return set()

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

def send_email(subject, body):
    sender = os.environ.get("NOTIFY_EMAIL", "")
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not sender or not app_password:
        print("  [email skipped — NOTIFY_EMAIL or GMAIL_APP_PASSWORD not set]")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = sender
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(sender, app_password)
            s.send_message(msg)
        print("  [email sent]")
    except Exception as e:
        print(f"  [email failed: {e}]")

def scan_once(network, client, syllabus, seen, out):
    new_drafts = 0
    for post in network.iter_all_posts(limit=POLL_LIMIT):
        nr = post.get("nr", "?")
        if nr in seen:
            continue
        seen.add(nr)
        if not needs_answer(post):
            continue
        q = question_text_of(post)
        if not q:
            continue
        r = gate(client, syllabus, q)
        cat = r.get("category", "skip")
        header = f"@{nr}  [{cat}]"
        print(f"  {header}  {q[:70].replace(chr(10), ' ')}")
        out.write(f"{header}\nQUESTION: {q}\n")
        if cat == "syllabus" and r.get("answer"):
            new_drafts += 1
            answer = r["answer"]
            source = r.get("source", "")
            evidence = r.get("evidence", "")
            print(f"    DRAFT: {answer[:110]}")
            out.write(f"DRAFT ANSWER: {answer}\nSOURCE: {source}\nEVIDENCE: {evidence}\n")
            subject = f"Piazza Draft Ready: @{nr} — {q[:55].replace(chr(10), ' ')}"
            body = (
                f"A new syllabus question on Piazza has a draft answer ready for your review.\n\n"
                f"Post: @{nr}\n\n"
                f"QUESTION:\n{q}\n\n"
                f"DRAFT ANSWER:\n{answer}\n\n"
                f"SOURCE: {source}\n\n"
                f"EVIDENCE:\n{evidence}\n\n"
                f"---\nThis is a draft only. Nothing has been posted to Piazza.\n"
                f"Review drafts.txt for the full log.\n"
            )
            send_email(subject, body)
        elif cat == "content":
            print("    -> leave for a human")
        elif cat == "not_found":
            print("    -> flag for staff")
        out.write("-" * 60 + "\n")
        out.flush()
    return new_drafts

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY is not set.")
    syllabus = load_syllabus()
    client = anthropic.Anthropic()

    print("\n--- READ-ONLY mode: this script never posts to Piazza ---")
    piazza_email = os.environ.get("PIAZZA_EMAIL") or input("Piazza email: ")
    piazza_password = os.environ.get("PIAZZA_PASSWORD") or getpass.getpass("Piazza password: ")
    network_id = (os.environ.get("PIAZZA_NETWORK") or input("Class network id (from piazza.com/class/THIS_PART): ")).strip()

    p = Piazza()
    try:
        p.user_login(email=piazza_email, password=piazza_password)
    except Exception as e:
        sys.exit(f"Login failed: {e}\nIf you sign in with Google/SSO, set a direct password via 'Forgot Password' on piazza.com.")

    network = p.network(network_id)
    seen = load_seen()

    print(f"Connected. Polling every {POLL_INTERVAL}s. Press Ctrl+C to stop.\n")

    out = open(OUTPUT_FILE, "a", encoding="utf-8")
    try:
        while True:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] Scanning {POLL_LIMIT} recent posts...")
            try:
                new_drafts = scan_once(network, client, syllabus, seen, out)
                save_seen(seen)
                if new_drafts:
                    print(f"  → {new_drafts} new draft(s) written and emailed.")
                else:
                    print("  → Nothing new.")
            except Exception as e:
                print(f"  [scan error: {e}]")
            print(f"  Next scan in {POLL_INTERVAL}s...\n")
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nStopped by user. Drafts saved to drafts.txt.")
    finally:
        out.close()

if __name__ == "__main__":
    main()
