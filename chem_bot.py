import os, re, sys, json, html, smtplib, time
from pathlib import Path
from email.mime.text import MIMEText

# ─── STEP 1: Load credentials ────────────────────────────────────────────────
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import anthropic
from piazza_api import Piazza

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
# Uses CHEM_* env vars so both bots can run simultaneously without interfering.
SYLLABUS_PATH = os.environ.get("CHEM_SYLLABUS_PATH", "chem_syllabus.txt")
SEEN_FILE     = os.environ.get("CHEM_SEEN_FILE", "chem_seen.json")
POLL_LIMIT    = 30
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))

# Two-model pipeline:
#   GATE_MODEL    — cheap Haiku classifier (routes every question)
#   CONTENT_MODEL — Sonnet for chemistry answers (accuracy matters for a grade)
GATE_MODEL    = "claude-haiku-4-5"
CONTENT_MODEL = "claude-sonnet-4-6"

# Stop automatically after this many content answers are posted.
# Per the CHEM 11 syllabus: 10 substantial contributions = all 40 Piazza points.
CONTENT_LIMIT      = int(os.environ.get("CHEM_CONTENT_LIMIT", "10"))
CONTENT_COUNT_FILE = os.environ.get("CHEM_COUNT_FILE", "chem_content_count.json")

# ─── STEP 2: The gate ────────────────────────────────────────────────────────
# Classifies each question. CHEM 11-specific twist (from syllabus page 4):
#   - "content" questions are SUBSTANTIAL contributions → bot answers them
#   - "syllabus" questions are NOT substantial but still useful to answer
#   - "exam"    questions are never answered (academic integrity)
#
# Pseudocode:
#   given question + syllabus →
#     "syllabus"  → logistics/policy answerable from syllabus → BOT ANSWERS (from syllabus)
#     "content"   → chemistry subject matter question         → BOT ANSWERS (chemistry knowledge)
#     "exam"      → appears to be an exam/quiz problem        → SKIP (academic integrity)
#     "not_found" → logistics not in syllabus                 → SKIP
#     "skip"      → noise/greeting                           → IGNORE
GATE_SYSTEM = """You triage questions on a college chemistry course forum (CHEM 11, intro chemistry for non-science majors at UCSD).

Respond with ONLY a raw JSON object, no markdown or code fences:
{{"category": "...", "answer": "...", "source": "..."}}

category is exactly one of:
- "syllabus": a logistics/policy/schedule/grading question whose answer is clearly in the syllabus. "answer" = concise, accurate answer. "source" = syllabus section name. Leave "answer" and "source" empty if not in syllabus.
- "content": a chemistry subject-matter question (concepts, reactions, calculations, nomenclature, periodic table, stoichiometry, molecular structure, etc.). Set "answer" to "" — a second model will generate the chemistry answer. "source" = "".
- "exam": the question appears to be asking for help solving an exam or quiz problem currently in progress (e.g., "the exam asks...", "question 3 says...", "on the test right now..."). Never answer. "answer" = "". "source" = "".
- "not_found": a legitimate logistics question NOT covered by the syllabus. "answer" = "". "source" = "".
- "skip": greeting, thank-you, off-topic noise, or meta-comment. "answer" = "". "source" = "".

Rules:
- Never invent syllabus facts; use "not_found" if unsure.
- When in doubt about "content" vs "exam", use "exam" (err on the side of caution).
- Chemistry questions about homework problems are "content" (helping understand concepts is fine).
- Questions about specific exam questions during an exam window are "exam" (do not answer).

SYLLABUS:
{syllabus}"""

# ─── STEP 3: The chemistry answerer ──────────────────────────────────────────
# Only called when the gate returns "content". Uses Sonnet for better accuracy
# on science questions. Answers are written at the level of a CHEM 11 student
# (non-science major, intro level).
CONTENT_SYSTEM = """You are a helpful and encouraging chemistry tutor for CHEM 11 (Introduction to General Chemistry for non-science majors) at UCSD.

The course uses: Fundamentals of General, Organic, and Biological Chemistry, 8th ed. by McMurry, Ballantine, Hoeger, and Peterson.

Topics covered:
  CH 1: Matter & Measurements (units, significant figures, density)
  CH 2: Atoms & the Periodic Table (atomic structure, electron configuration, periodic trends)
  CH 3: Ionic Compounds (ions, formulas, nomenclature)
  CH 4: Molecular Compounds (covalent bonds, Lewis structures, VSEPR, polarity)
  CH 5: Classification & Balancing of Chemical Reactions
  CH 6: Mole and Mass Relationships (molar mass, stoichiometry, limiting reagent)
  CH 7: Energy, Rates & Equilibrium (thermodynamics, reaction rates, Le Chatelier's principle)
  CH 8: Gases, Liquids & Solids (gas laws, intermolecular forces, phase changes)
  CH 9: Solutions (concentration, solubility, colligative properties)

Guidelines for your response:
- Write for a non-science major — clear, friendly, and jargon-light.
- For calculations: show every step explicitly.
- For conceptual questions: give a brief analogy or real-world connection when helpful.
- Keep answers focused: 2–4 sentences for concepts, step-by-step for problems.
- Be encouraging and positive in tone.
- Do NOT solve exam or quiz questions that appear to be in progress."""


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

def load_content_count():
    """Load the number of content answers posted so far (persists across restarts)."""
    p = Path(CONTENT_COUNT_FILE)
    return json.loads(p.read_text()) if p.exists() else 0

def save_content_count(n):
    Path(CONTENT_COUNT_FILE).write_text(json.dumps(n))

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


# ─── STEP 4: Gate call ───────────────────────────────────────────────────────

def gate(client, syllabus, question):
    """Classify question into: syllabus | content | exam | not_found | skip."""
    resp = client.messages.create(
        model=GATE_MODEL,
        max_tokens=500,
        system=GATE_SYSTEM.format(syllabus=syllabus),
        messages=[{"role": "user", "content": question}]
    )
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


# ─── STEP 5: Chemistry answer call ───────────────────────────────────────────

def answer_chemistry(client, question):
    """
    Generate a chemistry answer using Sonnet.
    Only called after the gate confirms the question is course content (not an exam).
    """
    resp = client.messages.create(
        model=CONTENT_MODEL,
        max_tokens=600,
        system=CONTENT_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


# ─── STEP 6: Post to Piazza ───────────────────────────────────────────────────

def post_answer(network, post, answer):
    """Post as s_answer (prevents double-post on restart). Falls back to followup."""
    cid = post.get("id")
    try:
        network._rpc.content_student_answer(cid, answer, revision=0)
        return "s_answer"
    except Exception:
        network.create_followup(post, answer)
        return "followup"

def send_confirmation(nr, question, answer, category, post_type):
    sender   = os.environ.get("NOTIFY_EMAIL", "")
    app_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not sender or not app_pass:
        return
    subject = f"[CHEM11 Bot] Posted @{nr} [{category}] — {question[:50].replace(chr(10), ' ')}"
    body = (
        f"CHEM 11 bot posted an answer on Piazza.\n\n"
        f"Post:     @{nr}\n"
        f"Category: {category}\n"
        f"Type:     {post_type}\n\n"
        f"QUESTION:\n{question}\n\n"
        f"ANSWER POSTED:\n{answer}\n"
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


# ─── STEP 7: Scan loop ───────────────────────────────────────────────────────

def scan_once(network, client, syllabus, seen, content_count):
    """
    Single scan pass.

    Pseudocode for each post:
      1. Skip if already processed (chem_seen.json)
      2. Skip if already answered (needs_answer check)
      3. Gate classifies: syllabus / content / exam / not_found / skip
      4a. If "syllabus" → answer from syllabus text (gate already generated it)
      4b. If "content"  → call Sonnet to generate chemistry answer → post
      4c. If "exam"     → skip (academic integrity)
      4d. Otherwise     → skip

    Returns (total_posted, content_posted) so the caller can track the
    content-answer limit independently of syllabus answers.
    """
    total_posted   = 0
    content_posted = 0

    for post in network.iter_all_posts(limit=POLL_LIMIT):
        nr = post.get("nr", "?")
        if nr in seen:
            continue

        seen.add(nr)  # mark before posting to prevent double-post on crash/restart

        if not needs_answer(post):
            continue

        q = question_text_of(post)
        if not q:
            continue

        r   = gate(client, syllabus, q)
        cat = r.get("category", "skip")
        print(f"  @{nr} [{cat}] {q[:65].replace(chr(10), ' ')}")

        answer     = None
        is_content = False

        if cat == "syllabus" and r.get("answer"):
            answer = r["answer"]

        elif cat == "content":
            # Check limit before making the expensive Sonnet call
            if content_count + content_posted >= CONTENT_LIMIT:
                print(f"    -> content limit ({CONTENT_LIMIT}) reached — skipping")
                continue
            print("    -> chemistry question — generating answer...")
            try:
                answer     = answer_chemistry(client, q)
                is_content = True
            except Exception as e:
                print(f"    ✗ content answer failed: {e}")

        elif cat == "exam":
            print("    -> appears to be exam question — skipping (academic integrity)")
        elif cat == "not_found":
            print("    -> not in syllabus, skipping")

        if answer:
            try:
                ptype = post_answer(network, post, answer)
                total_posted += 1
                if is_content:
                    content_posted += 1
                print(f"    ✓ posted ({ptype}): {answer[:100]}")
                send_confirmation(nr, q, answer, cat, ptype)
            except Exception as e:
                print(f"    ✗ post failed: {e}")

    return total_posted, content_posted


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    # Accept PIAZZA_NETWORK_CHEM11 or CHEM_PIAZZA_NETWORK (either naming works)
    if os.environ.get("PIAZZA_NETWORK_CHEM11") and not os.environ.get("CHEM_PIAZZA_NETWORK"):
        os.environ["CHEM_PIAZZA_NETWORK"] = os.environ["PIAZZA_NETWORK_CHEM11"]

    for var in ("ANTHROPIC_API_KEY", "PIAZZA_EMAIL", "PIAZZA_PASSWORD", "CHEM_PIAZZA_NETWORK"):
        if not os.environ.get(var):
            sys.exit(f"ERROR: {var} (or PIAZZA_NETWORK_CHEM11) is not set.")

    syllabus = load_syllabus()
    client   = anthropic.Anthropic()

    p = Piazza()
    try:
        p.user_login(email=os.environ["PIAZZA_EMAIL"], password=os.environ["PIAZZA_PASSWORD"])
    except Exception as e:
        sys.exit(f"Piazza login failed: {e}")

    network       = p.network(os.environ["CHEM_PIAZZA_NETWORK"])
    seen          = load_seen()
    content_count = load_content_count()

    if content_count >= CONTENT_LIMIT:
        print(f"Content post limit already reached ({content_count}/{CONTENT_LIMIT}). Nothing to do.")
        sys.exit(0)

    print(f"CHEM 11 bot connected. Polling every {POLL_INTERVAL}s.")
    print(f"Content answers posted: {content_count}/{CONTENT_LIMIT}. Ctrl+C to stop.\n")

    while True:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] Scanning CHEM 11... (content: {content_count}/{CONTENT_LIMIT})")
        try:
            total, new_content = scan_once(network, client, syllabus, seen, content_count)
            save_seen(seen)
            content_count += new_content
            save_content_count(content_count)
            if total:
                print(f"  → {total} posted ({new_content} content).")
            else:
                print("  → Nothing new.")

            # Stop once the Piazza participation goal is met
            if content_count >= CONTENT_LIMIT:
                print(f"\nObjective complete: {content_count} content answers posted.")
                print("All 40 Piazza participation points secured. Bot shutting down.")
                sys.exit(0)

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
