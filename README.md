# COGS 9 Piazza Bot

An automated teaching assistant that monitors the COGS 9 Piazza forum and instantly answers logistics and syllabus questions — freeing instructors and TAs from repetitive FAQ-style posts.

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

The bot **only ever answers logistics questions** (grading policies, deadlines, late policy, etc.) and **never engages with course content**. It is grounded exclusively in `syllabus.txt` — it cannot invent information.

---

## Safety design

1. **Strict content gate** — any question touching course subject matter is silently skipped and left for a human. When in doubt, the gate defaults to `content` (skips).
2. **Syllabus-grounded only** — every answer cites the exact sentence from the syllabus that justifies it (`evidence` field). The model is instructed never to invent facts.
3. **No hallucination guard** — if the answer isn't clearly in the syllabus, the gate returns `not_found` and stays silent.
4. **Instructor notification** — every answer the bot posts triggers an email so the instructor can review or override at any time.
5. **No double-posting** — posts as `s_answer` type (Piazza's student-answer slot), so Piazza's own logic marks the question answered and skips it on subsequent scans. A `seen.json` file provides a second layer of deduplication.

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
| `Procfile` | Railway deployment config (`worker: python bot.py`) |
| `requirements.txt` | Python dependencies (`anthropic`, `piazza-api`) |

---

## Quickstart (local)

```bash
# 1. Create and activate a virtual environment
python3 -m venv venv && source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create a .env file (never committed to git)
ANTHROPIC_API_KEY=sk-ant-...
PIAZZA_EMAIL=your@email.com
PIAZZA_PASSWORD=yourpassword
PIAZZA_NETWORK=<network_id_from_piazza_url>
NOTIFY_EMAIL=your@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

# 4. Add the course syllabus as plain text
cp /path/to/syllabus.txt syllabus.txt

# 5. Test the gate classifier offline (no Piazza connection needed)
python test_gate.py

# 6. Run the bot
python bot.py
```

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

## Updating the syllabus

Edit `syllabus.txt` and push to GitHub. Railway auto-redeploys and the bot picks up the new syllabus on its next startup.

---

Built for COGS 9 — Intro to Data Science, Summer 2026, UCSD.
Instructor: Kyle Shannon.
