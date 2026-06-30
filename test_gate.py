import os, re, sys, json
import anthropic

SYLLABUS_PATH = "syllabus.txt"
GATE_MODEL = "claude-haiku-4-5"

BUILTIN_QUESTIONS = [
    "When is the second exam and is it open book?",
    "How many points do I need for the full Daily Work credit?",
    "When are Dr. Stallings' office hours and what's the Zoom password?",
    "How does the grade improvement plan work?",
    "When is homework set 5 due?",
    "What grade is an 84%?",
    "Can you explain how to balance this equation: H2 + O2 -> H2O?",
    "What's the molar mass of glucose?",
    "What's the wifi password in the chemistry building?",
    "Where do I park on campus for the final?",
    "thanks so much!!",
]

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
        return json.loads(m.group(0)) if m else {"category": "skip"}

def wrap(text, indent="     "):
    out, line = [], ""
    for word in (text or "").split():
        if len(line) + len(word) + 1 > 78:
            out.append(indent + line); line = word
        else:
            line = (line + " " + word).strip()
    if line: out.append(indent + line)
    return "\n".join(out)

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY is not set.")
    questions = BUILTIN_QUESTIONS
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            questions = [ln.strip() for ln in f if ln.strip()]
    syllabus = load_syllabus()
    client = anthropic.Anthropic()
    counts = {"syllabus": 0, "content": 0, "not_found": 0, "skip": 0}
    print("\n" + "=" * 82)
    print(f" GATE TEST  -  {len(questions)} questions  -  model {GATE_MODEL}")
    print("=" * 82)
    for i, q in enumerate(questions, 1):
        r = gate(client, syllabus, q)
        cat = r.get("category", "skip")
        counts[cat] = counts.get(cat, 0) + 1
        label = {"syllabus": "ANSWER", "content": "LEAVE FOR HUMAN",
                 "not_found": "FLAG FOR STAFF", "skip": "IGNORE"}.get(cat, cat.upper())
        print(f"\n[{i}] {q}")
        print(f"    -> {label}  ({cat})")
        if cat == "syllabus":
            print("    answer:"); print(wrap(r.get("answer", "")))
            print(f"    source:   {r.get('source','')}")
            print("    evidence:"); print(wrap(r.get("evidence", ""), indent="       | "))
    print("\n" + "=" * 82)
    total = sum(counts.values()) or 1
    print(" SUMMARY")
    for k in ["syllabus", "content", "not_found", "skip"]:
        c = counts.get(k, 0)
        print(f"   {k:<11} {c:>3}   ({100*c//total}%)")
    answered = counts.get("syllabus", 0)
    print(f"\n   The bot would auto-handle {answered}/{total} ({100*answered//total}%) of these.")
    print("=" * 82 + "\n")

if __name__ == "__main__":
    main()
