import os, re, sys, json, html, getpass
from pathlib import Path

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
POLL_LIMIT = 20
OUTPUT_FILE = "drafts.txt"

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

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY is not set.")
    syllabus = load_syllabus()
    client = anthropic.Anthropic()
    print("\n--- READ-ONLY mode: this script never posts to Piazza ---")
    email = os.environ.get("PIAZZA_EMAIL") or input("Piazza email: ")
    password = os.environ.get("PIAZZA_PASSWORD") or getpass.getpass("Piazza password: ")
    network_id = (os.environ.get("PIAZZA_NETWORK") or input("Class network id (from piazza.com/class/THIS_PART): ")).strip()
    p = Piazza()
    try:
        p.user_login(email=email, password=password)
    except Exception as e:
        sys.exit(f"Login failed: {e}\nIf you sign in with Google/SSO, set a direct password via 'Forgot Password' on piazza.com.")
    network = p.network(network_id)
    print(f"Connected. Scanning up to {POLL_LIMIT} recent posts...\n")
    counts = {"syllabus": 0, "content": 0, "not_found": 0, "skip": 0}
    drafted = 0
    out = open(OUTPUT_FILE, "w", encoding="utf-8")
    for post in network.iter_all_posts(limit=POLL_LIMIT):
        if not needs_answer(post):
            continue
        q = question_text_of(post)
        if not q:
            continue
        r = gate(client, syllabus, q)
        cat = r.get("category", "skip")
        counts[cat] = counts.get(cat, 0) + 1
        nr = post.get("nr", "?")
        header = f"@{nr}  [{cat}]"
        print(f"{header}  {q[:70].replace(chr(10),' ')}")
        out.write(f"{header}\nQUESTION: {q}\n")
        if cat == "syllabus" and r.get("answer"):
            drafted += 1
            print(f"   DRAFT: {r['answer'][:110]}")
            out.write(f"DRAFT ANSWER: {r['answer']}\nSOURCE: {r.get('source','')}\nEVIDENCE: {r.get('evidence','')}\n")
        elif cat == "content":
            print("   -> leave for a human")
        elif cat == "not_found":
            print("   -> flag for staff")
        out.write("-" * 60 + "\n")
    out.close()
    print(f"\nScanned. Buckets: {counts}")
    print(f"Drafted {drafted} syllabus answers. Full details saved to {OUTPUT_FILE}.")
    print("Nothing was posted to Piazza.\n")

if __name__ == "__main__":
    main()
