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

try:
    import canvas_context
except Exception:
    canvas_context = None

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
# Uses CHEM_* env vars so both bots can run simultaneously without interfering.
BASE_DIR           = Path(__file__).parent
SYLLABUS_PATH      = BASE_DIR / os.environ.get("CHEM_SYLLABUS_PATH", "syllabus_b.txt")
SEEN_FILE          = BASE_DIR / os.environ.get("CHEM_SEEN_FILE", "chem_seen.json")
POLL_LIMIT         = 30
POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL", "120"))

# Two-model pipeline:
#   GATE_MODEL    — cheap Haiku classifier (routes every question)
#   CONTENT_MODEL — Sonnet for chemistry answers (accuracy matters for a grade)
GATE_MODEL    = "claude-haiku-4-5"
CONTENT_MODEL = "claude-haiku-4-5"

# Stop automatically after this many answers are posted.
CONTENT_LIMIT      = int(os.environ.get("CHEM_CONTENT_LIMIT", "10"))
CONTENT_COUNT_FILE = BASE_DIR / os.environ.get("CHEM_COUNT_FILE", "chem_content_count.json")

_scan_count_b = 0
_last_post_b = None   # dict or None

# ─── STEP 2: The gate ────────────────────────────────────────────────────────
# Classifies each question before the bot decides whether to answer.
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
{{"category": "...", "answer": "...", "source": "...", "context_ref": "..."}}

category is exactly one of:
- "syllabus": a logistics/policy/schedule/grading question whose answer is clearly in the syllabus. "answer" = concise, accurate answer. "source" = syllabus section name. Leave "answer" and "source" empty if not in syllabus.
- "content": a chemistry subject-matter question (concepts, reactions, calculations, nomenclature, periodic table, stoichiometry, molecular structure, etc.). Set "answer" to "" — a second model will generate the chemistry answer. "source" = "". Set "context_ref" to a short string naming the specific course material the student is referencing (e.g., "Chapter 1 HW Item 7 part A") when the question clearly references a specific assignment, problem set, or course artifact; otherwise "".
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
# Only called when the gate returns "content". Uses Haiku for fast, concise answers.
CONTENT_SYSTEM = """You are answering a Piazza question for CHEM 11 (Introduction to General Chemistry for non-science majors) at UCSD. Textbook: Fundamentals of General, Organic, and Biological Chemistry, 8th ed. by McMurry et al. Topics: Matter & Measurements, Atoms & the Periodic Table, Ionic Compounds, Molecular Compounds, Chemical Reactions, Stoichiometry, Energy/Rates/Equilibrium, Gases/Liquids/Solids, Solutions.

Answer at the scope of a first year college student. Keep your responses as if you were typing a quick message to a classmate between classes. The message should pass as a serious sounding real person, not an AI. Keep it mostly formal, polite, and straight to the point. Basically just answer it as briefly yet properly as possible.

REQUIRED FORMAT — every rule below is mandatory:
Do not use em dashes (—) or en dashes (–). Use a comma or period instead.
Do not use semicolons (;). Split into separate sentences instead.
Do not use bullet points, numbered lists, or any list formatting.
Do not use bold, italics, headers, or any markdown.
Do not open with a greeting of any kind (no Hi, Hello, Hey, Dear, etc.).
Do not close with any follow-up, sign-off, or pleasantry (no "Hope this helps", "Let me know", "Feel free", "Any questions", "Good luck", etc.).
Do not identify yourself as an AI or reference Claude, AI, or language models in any way.
Do not open with a compliment on the question (no "Great question", "Good question", etc.).
Do not use filler phrases like "Furthermore", "It is important to note", "In conclusion", or "In summary".
Do not sound condescending or superior. You are a peer, not a teacher.
Do not give advice, tips, or suggestions. Only answer what was directly asked.
Do not use rhetorical questions to lead into your answer. Start with the answer itself.
Write between 30 and 250 words.
End your response with the last relevant word of your answer and nothing after it."""


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


# ─── STEP 5: Answer quality checklist ────────────────────────────────────────

_CHECKLIST = [
    (r"[—–]",                                                   "em/en dash found"),
    (r";",                                                       "semicolon found"),
    (r"(?m)^\s*[-•*]\s",                                        "bullet point found"),
    (r"(?m)^\s*\d+\.\s",                                        "numbered list found"),
    (r"\*\*|__|\*[^*\s]|_[^_\s]",                              "markdown formatting found"),
    (r"(?i)^(hi|hello|hey|dear|greetings)[,!\s]",              "starts with greeting"),
    (r"(?i)(hope this helps|let me know|feel free|any questions|good luck|don't hesitate)", "sign-off phrase found"),
    (r"(?i)(as an ai|i'm an ai|language model|i'm claude|ai assistant)", "AI self-identification found"),
    (r"(?i)(great question|excellent question|good question|that's a great)", "sycophantic opener found"),
    (r"(?i)(furthermore|it is important to note|it's important to note|in conclusion|in summary)", "AI filler phrase found"),
    (r"(?i)(you should|you need to|make sure you|remember to|always remember|don't forget|i recommend|i suggest|my advice|pro tip)", "advice/tip found"),
]

_MIN_WORDS = 30
_MAX_WORDS = 250

def _check_answer(text):
    """Return list of violation strings. Empty list means the answer passes."""
    failures = []
    for pattern, msg in _CHECKLIST:
        if re.search(pattern, text):
            failures.append(msg)
    words = len(text.split())
    if words < _MIN_WORDS:
        failures.append(f"too short ({words} words, min {_MIN_WORDS})")
    if words > _MAX_WORDS:
        failures.append(f"too long ({words} words, max {_MAX_WORDS})")
    return failures


# ─── STEP 6: Chemistry answer call ───────────────────────────────────────────

def answer_chemistry(client, question, context=None, max_attempts=3):
    """
    Generate a chemistry answer using Haiku.
    Only called after the gate confirms the question is course content (not an exam).
    Retries up to max_attempts times if the answer fails the quality checklist.
    When context is a non-empty string, it is prepended to the user message so the
    model can reference specific course material without changing CONTENT_SYSTEM.
    """
    if context:
        user_message = (
            f"COURSE MATERIAL CONTEXT (for reference, may be partial):\n{context}"
            f"\n\nQUESTION:\n{question}"
        )
    else:
        user_message = question
    answer = ""
    for attempt in range(1, max_attempts + 1):
        resp = client.messages.create(
            model=CONTENT_MODEL,
            max_tokens=400,
            system=CONTENT_SYSTEM,
            messages=[{"role": "user", "content": user_message}]
        )
        answer = "".join(b.text for b in resp.content if b.type == "text").strip()
        violations = _check_answer(answer)
        if not violations:
            return answer
        print(f"    [checklist] attempt {attempt}/{max_attempts} failed: {violations}")
    print(f"    [checklist] all attempts exhausted — using last answer anyway")
    return answer


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

def send_confirmation(nr, question, answer, category, post_type, post_count, limit):
    sender   = os.environ.get("NOTIFY_EMAIL", "")
    app_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not sender or not app_pass:
        return
    progress = f"{post_count}/{limit}"
    done_msg = " — OBJECTIVE COMPLETE" if post_count >= limit else ""
    subject = f"[CHEM11 Bot] {progress} Posted @{nr} [{category}]{done_msg}"
    body = (
        f"CHEM 11 bot posted an answer on Piazza.\n\n"
        f"Progress: {progress} answers toward Piazza goal{done_msg}\n\n"
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


# ─── INSTRUMENTATION ─────────────────────────────────────────────────────────

def write_status_b(seen, post_count, status="running", objective_complete=False):
    """Write assistant_b_status.json. Wrapped in try/except — never crashes the bot."""
    global _scan_count_b, _last_post_b
    try:
        data = {
            "bot": "B",
            "course": "CHEM 11",
            "status": status,
            "pid": os.getpid(),
            "last_scan": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scan_count": _scan_count_b,
            "posts_answered": post_count,
            "post_limit": CONTENT_LIMIT,
            "objective_complete": objective_complete,
            "seen_count": len(seen),
            "poll_interval": POLL_INTERVAL,
            "last_post": _last_post_b,
        }
        (BASE_DIR / "assistant_b_status.json").write_text(json.dumps(data))
    except Exception as e:
        print(f"  [status write error: {e}]")

def append_activity_b(nr, question, category, answer):
    """Append one entry to activity_log.json (shared with Bot A). Max 100 entries."""
    try:
        log_path = BASE_DIR / "activity_log.json"
        try:
            entries = json.loads(log_path.read_text()) if log_path.exists() else []
            if not isinstance(entries, list):
                entries = []
        except Exception:
            entries = []
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "bot": "B",
            "nr": nr,
            "question": question[:300],
            "category": category,
            "answer": answer[:500],
            "posted": True,
        }
        entries.append(entry)
        entries = entries[-100:]
        log_path.write_text(json.dumps(entries))
    except Exception as e:
        print(f"  [activity log error: {e}]")

def append_scan_log_b(nr, cid, question, category, gate_result, posted):
    """Log every classified post to scan_log.json (shared with Bot A). Max 200 entries."""
    try:
        log_path = BASE_DIR / "scan_log.json"
        try:
            entries = json.loads(log_path.read_text()) if log_path.exists() else []
            if not isinstance(entries, list):
                entries = []
        except Exception:
            entries = []
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "bot": "B",
            "course": "CHEM 11",
            "nr": nr,
            "cid": cid,
            "question": question[:400],
            "category": category,
            "gate_answer": (gate_result.get("answer") or "")[:500],
            "source": gate_result.get("source") or "",
            "posted": posted,
        }
        entries.append(entry)
        entries = entries[-200:]
        log_path.write_text(json.dumps(entries))
    except Exception as e:
        print(f"  [scan log error: {e}]")


# ─── STEP 7: Scan loop ───────────────────────────────────────────────────────

def scan_once(network, client, syllabus, seen, post_count):
    """
    Single scan pass.

    Pseudocode for each post:
      1. Skip if already processed (seen file)
      2. Skip if already answered (needs_answer check)
      3. Gate classifies: syllabus / content / exam / not_found / skip
      4a. If "syllabus" → answer from syllabus text (gate already generated it)
      4b. If "content"  → call Sonnet to generate chemistry answer → post
      4c. If "exam"     → skip (academic integrity)
      4d. Otherwise     → skip

    Counts ALL posted answers (syllabus + content) toward the post limit.
    Returns new_posts count for this scan.
    """
    global _scan_count_b, _last_post_b
    new_posts = 0
    _scan_count_b += 1

    for post in network.iter_all_posts(limit=POLL_LIMIT):
        nr = post.get("nr", "?")
        if nr in seen:
            continue

        seen.add(nr)  # mark before posting to prevent double-post on crash/restart

        if not needs_answer(post):
            continue

        # Stop scanning if limit already reached mid-scan
        if post_count + new_posts >= CONTENT_LIMIT:
            print(f"    -> post limit ({CONTENT_LIMIT}) reached — stopping scan")
            break

        q = question_text_of(post)
        if not q:
            continue

        cid = post.get("id")
        r   = gate(client, syllabus, q)
        cat = r.get("category", "skip")
        print(f"  @{nr} [{cat}] {q[:65].replace(chr(10), ' ')}")

        answer = None
        actually_posted = False

        if cat == "syllabus" and r.get("answer"):
            answer = r["answer"]

        elif cat == "content":
            print("    -> chemistry question — generating answer...")
            _ctx = None
            try:
                _folders = list(post.get("folders", [])) + list(post.get("tags", []))
                _context_ref = r.get("context_ref", "")
                _material_tags = {"homework", "hw", "assignment"}
                _should_fetch = (
                    canvas_context is not None
                    and canvas_context.is_configured()
                    and (
                        bool(_context_ref)
                        or any(
                            t in _material_tags or t.startswith("chapter_")
                            for t in _folders
                        )
                    )
                )
                if _should_fetch:
                    try:
                        _result = canvas_context.retrieve(q, tags=_folders)
                        if _result.get("is_assessment"):
                            print("    -> fetched material is an assessment, skipping (academic integrity)")
                            append_scan_log_b(nr, cid, q, cat, r, False)
                            continue
                        if _result.get("found"):
                            _ctx = _result.get("context_block") or None
                    except Exception as _e:
                        print(f"    [canvas_context] retrieve failed, falling back: {_e}")
            except Exception as e:
                print(f"    [canvas context error: {e}]")
                _ctx = None
            try:
                answer = answer_chemistry(client, q, context=_ctx)
            except Exception as e:
                print(f"    ✗ content answer failed: {e}")

        elif cat == "exam":
            print("    -> appears to be exam question — skipping (academic integrity)")
        elif cat == "not_found":
            print("    -> not in syllabus, skipping")

        if answer:
            try:
                ptype = post_answer(network, post, answer)
                new_posts += 1
                current = post_count + new_posts
                actually_posted = True
                _last_post_b = {"nr": nr, "question": q[:300], "category": cat, "answer": answer[:500], "time": time.strftime("%Y-%m-%d %H:%M:%S")}
                append_activity_b(nr, q, cat, answer)
                print(f"    ✓ posted ({ptype}) [{current}/{CONTENT_LIMIT}]: {answer[:100]}")
                send_confirmation(nr, q, answer, cat, ptype, current, CONTENT_LIMIT)
            except Exception as e:
                print(f"    ✗ post failed: {e}")

        append_scan_log_b(nr, cid, q, cat, r, actually_posted)

    write_status_b(seen, post_count + new_posts)
    return new_posts


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

    network    = p.network(os.environ["CHEM_PIAZZA_NETWORK"])
    seen       = load_seen()
    post_count = load_content_count()

    if post_count >= CONTENT_LIMIT:
        print(f"Post limit already reached ({post_count}/{CONTENT_LIMIT}). Nothing to do.")
        write_status_b(seen, post_count, status="complete", objective_complete=True)
        sys.exit(0)

    print(f"Bot B connected. Polling every {POLL_INTERVAL}s.")
    print(f"Answers posted: {post_count}/{CONTENT_LIMIT}. Ctrl+C to stop.\n")

    while True:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] Scanning... ({post_count}/{CONTENT_LIMIT} answers posted)")
        try:
            new_posts = scan_once(network, client, syllabus, seen, post_count)
            save_seen(seen)
            post_count += new_posts
            save_content_count(post_count)
            print(f"  → {new_posts} posted." if new_posts else "  → Nothing new.")

            # Stop once the Piazza participation goal is met
            if post_count >= CONTENT_LIMIT:
                print(f"\nObjective complete: {post_count}/{CONTENT_LIMIT} answers posted.")
                print("All participation points secured. Bot shutting down.")
                write_status_b(seen, post_count, status="complete", objective_complete=True)
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
