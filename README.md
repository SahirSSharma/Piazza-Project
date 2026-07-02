# Piazza Syllabus Bot

A general-purpose automated teaching assistant that monitors any Piazza course forum and instantly answers logistics and syllabus questions — freeing instructors and TAs from repetitive FAQ-style posts.

Works with any course: point it at a Piazza network ID and a plain-text syllabus and it's ready to go. Currently configured for COGS 9 — Intro to Data Science, Summer 2026, UCSD (Instructor: Kyle Shannon).

Deployed 24/7 on Railway (cloud). Built with Python, Claude Haiku 4.5, and the unofficial `piazza_api` library.

---

## What it does

The bot polls the class Piazza every 2 minutes. When it finds a new unanswered question, it routes it through a **content gate** powered by Claude Haiku 4.5:

| Gate result | Action |
|---|---|
| `syllabus` — logistics question answerable from the syllabus | Post answer directly to Piazza, email the instructor |
| `content` — course subject matter (concepts, homework, exams) | Skip — leave for instructor/TAs |
| `not_found` — logistics question not covered by the syllabus | Skip — leave for instructor/TAs |
| `skip` — greeting or noise | Ignore |

The bot **only ever answers logistics questions** (grading policies, deadlines, late policy, etc.) and **never engages with course content**. It is grounded exclusively in the provided `syllabus.txt` — it cannot invent information.

---

## Safety design

1. **Strict content gate** — any question touching course subject matter is silently skipped and left for a human. When in doubt, the gate defaults to `content` (skips).
2. **Syllabus-grounded only** — every answer cites the exact sentence from the syllabus that justifies it (`evidence` field). The model is instructed never to invent facts.
3. **No hallucination guard** — if the answer isn't clearly in the syllabus, the gate returns `not_found` and stays silent.
4. **Instructor notification** — every answer the bot posts triggers an email so the instructor can review or override at any time.
5. **No double-posting** — posts as `s_answer` type (Piazza's student-answer slot), so Piazza's own logic marks the question answered and skips it on subsequent scans. A `seen.json` file provides a second layer of deduplication.

---

## How the bot gets Canvas context

The CHEM 11 bot (`assistant_b.py`) supplements its syllabus grounding with live Canvas material — the primary COGS 9 bot (`bot.py`) is syllabus-only and does not use Canvas at all. When the CHEM 11 bot classifies an incoming question as `content` and detects that it references specific course material (a particular assignment, page, or file), it fetches that item from Canvas via the REST API and injects it as additional context before generating the answer. The academic integrity gate is unchanged: if the fetched material is an exam or quiz, the question is silently skipped exactly as it would be without Canvas context. Because UCSD disables personal access tokens for student accounts, authentication is session-cookie based. One-time setup: run `python canvas_login.py`, log into canvas.ucsd.edu in a browser, copy the `canvas_session` cookie value from browser devtools and paste it when prompted — the script writes `canvas_cookies.json` (gitignored). Re-run `canvas_login.py` whenever the session expires. If Canvas is unreachable or the session has expired, the bot falls back first to a local crawled cache at `~/.claude/canvas-cache/`, then to answering without Canvas context — the scan loop never crashes. All Canvas-related environment variables are optional; the feature degrades gracefully if any or all are absent.

---

## Architecture

```
Piazza forum
    ↓  piazza_api (unofficial Python library — read + write)
  bot.py — polling loop (every 120 seconds)
    ↓  new unanswered question detected
  Claude Haiku 4.5 content gate
    ├── syllabus   → post s_answer to Piazza + email notification to instructor
    ├── content    → skip (leave for humans)
    ├── not_found  → skip (leave for humans)
    └── skip       → ignore
```

**Key invariant:** the gate is the only component that calls the AI. The polling loop itself is zero-cost — it only makes an API call when there is a new unanswered question.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main polling loop — entry point for Railway deployment |
| `syllabus.txt` | Plain-text syllabus — the bot's only allowed knowledge source |
| `test_gate.py` | Offline gate classifier eval — run this before deploying to verify accuracy |
| `dump_piazza.py` | Read-only admin tool — dumps all posts to verify Piazza connectivity (never writes) |
| `publish.py` | Manual override — post a specific answer from the command line |
| `.env_template` | Template for your `.env` file — copy to `.env` and fill in values |
| `Procfile` | Railway deployment config |
| `requirements.txt` | Python dependencies (`anthropic`, `piazza-api`) |

---

## Quickstart (local)

```bash
# 1. Create and activate a virtual environment
python3 -m venv venv && source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your .env from the template (never committed to git)
cp .env_template .env
# Then open .env and fill in your values

# 4. Add your course syllabus as plain text
#    This is the bot's only knowledge source — paste in any course syllabus
cp /path/to/syllabus.txt syllabus.txt

# 5. Test the gate classifier offline (no Piazza connection needed)
python test_gate.py

# 6. Run the bot
python bot.py
```

To switch courses: update `PIAZZA_NETWORK` in `.env` and replace `syllabus.txt`. No code changes required.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | API key from console.anthropic.com |
| `PIAZZA_EMAIL` | Yes | Piazza account email for the bot |
| `PIAZZA_PASSWORD` | Yes | Piazza account password |
| `PIAZZA_NETWORK` | Yes | Network ID from the Piazza class URL |
| `NOTIFY_EMAIL` | No | Gmail address for post confirmations |
| `GMAIL_APP_PASSWORD` | No | Gmail App Password (Settings → Security → App Passwords) |
| `POLL_INTERVAL` | No | Seconds between scans (default: `120`) |
| `SYLLABUS_PATH` | No | Path to syllabus file (default: `syllabus.txt`) |
| `CANVAS_COOKIES_PATH` | No | Path to the persisted Canvas session-cookie file written by `canvas_login.py` (default: `canvas_cookies.json`). Feature degrades gracefully if absent. |
| `CHEM_CANVAS_COURSE_ID` | No | Numeric Canvas course ID for CHEM 11. Used for live API calls; local-cache fallback works without it. |
| `CANVAS_API_BASE` | No | Canvas REST API base URL (default: `https://canvas.ucsd.edu/api/v1`). |
| `CANVAS_LOCAL_CACHE_DIR` | No | Path to the local crawled Canvas cache used as fallback when the session is expired or Canvas is unreachable (default: `~/.claude/canvas-cache`). |
| `CANVAS_CACHE_TTL_HOURS` | No | How long (in hours) to cache live Canvas API responses in memory (default: `6`). |

---

## Railway deployment (24/7 cloud)

1. Push this repo to GitHub
2. Create a new Railway project and connect the GitHub repo
3. Add all environment variables in Railway → Variables
4. Railway auto-deploys on every push; the bot runs as a persistent background worker

---

## Cost estimate

| Item | Monthly cost |
|---|---|
| Railway Hobby plan (hosting) | ~$5–6 |
| Anthropic API — Claude Haiku 4.5 ($1/MTok in, $5/MTok out) | ~$0.002/question → ~$0.30–1.00 |
| **Total** | **~$6–7/month** |

The API is only called when there is a new unanswered post. Polling itself is free.

---

## Switching courses

Update two things and push:
1. Set `PIAZZA_NETWORK` to the new class's network ID (found in the Piazza URL)
2. Replace `syllabus.txt` with the new course syllabus (plain text)

Railway auto-redeploys and the bot picks up the new configuration on its next startup. No code changes required.
