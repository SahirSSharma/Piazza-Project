---
name: gate-json-parsing
description: gate() malformed-model-output parsing fragility and the safe-degrade contract
metadata:
  type: project
---

`gate()` must never let a malformed model response crash the scan; on unparseable output it returns `{"category": "skip"}` (safe: skip = not answered, not drafted, consistent with read-only/conservative posture).

**Why:** The model occasionally emits non-JSON artifacts (trailing commas, single quotes, prose wrapping one or more `{...}` spans, fenced+trailing-comma). A regex `\{.*\}` repair span can itself be invalid JSON. An earlier version left the *repair* `json.loads` unguarded, so it raised an uncaught `JSONDecodeError` mid-scan and killed the whole run — the first parse was in try/except but the fallback was not.

**How to apply:**
- Any future change to `gate()` parsing must keep BOTH the primary and the repair `json.loads` guarded, degrading to `{"category": "skip"}` rather than raising. Same `gate()` exists in both `draft_only.py` and `test_gate.py` — fix/verify them together if touching parsing.
- Reproduce deterministically without API/creds by stubbing the client's `messages.create` to return a chosen text payload (see [[repro-and-creds]]); feed trailing-comma / multi-brace-prose / single-quote payloads.
- Do not "fix" parsing by loosening the refusal or category contract; robustness only.
