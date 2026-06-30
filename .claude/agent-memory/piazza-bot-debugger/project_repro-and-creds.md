---
name: repro-and-creds
description: How draft_only.py obtains inputs/creds, what blocks end-to-end runs, and the credential-free harness for reproducing gate bugs
metadata:
  type: project
---

End-to-end `draft_only.py` runs are blocked without real Piazza access; the gate/parsing path can be reproduced fully without any secrets.

**Why:** A subagent cannot supply Piazza credentials or the class network id, and these are never in env/.env/config in the repo.

**How to apply:**
- `draft_only.py` input topology: `ANTHROPIC_API_KEY` must be an env var (hard exit if missing). `PIAZZA_EMAIL` / `PIAZZA_PASSWORD` are read from env if present, otherwise prompted interactively (`input()` / `getpass`). The class **network id is ALWAYS interactive `input()`** — there is no env fallback, so the script cannot run unattended even if both Piazza env vars are set. Stop and report when you reach login without creds; never fabricate them.
- `test_gate.py` is the credential-free reproduction harness: it imports nothing from `piazza_api` and runs the **identical** `gate()` + `GATE_SYSTEM` against built-in questions (or a file arg). Use it (or stub the anthropic client with a fake messages.create) to reproduce/verify any model-output parsing bug without Piazza.
- piazza-api 0.15.0 (installed) signatures verified to match the code: `Piazza.user_login(email, password)`, `Piazza.network(network_id)`, `Network.iter_all_posts(limit=None, sleep=0)` — no method drift as of this writing.
- Read-only/refusal posture lives in `GATE_SYSTEM` (content questions -> "content"/leave for human) and the script only ever writes to local `drafts.txt`; it has no Piazza write calls. Keep it that way. See [[gate-json-parsing]].
