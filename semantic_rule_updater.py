"""
semantic_rule_updater.py — Engage Mate AI
==========================================
Pull the next pending Vapi changelog from Airtable and semantic-merge
it into qwen_rules/03_vapi_constraints.md.

Pipeline:
  1. Read qwen_rules/03_vapi_constraints.md  (current rulebook).
  2. Query the Airtable table "Vapi Changelogs" for records where
     Status == 'Pending', sort by createdTime ASC, take the oldest.
  3. If no pending records exist, print a message and exit 0.
  4. Extract that record's "Update Text" field. Empty / missing
     text fails loudly with the record ID (operator-error, not
     auto-handled — surface it so the bad row gets fixed).
  5. Delta extraction. Make.com scrapes the entire Vapi changelog
     every run, but we only want Groq to see what's NEW. Read the
     last-merged H2 header from qwen_rules/last_processed_header.txt,
     find that exact header in the incoming text, and slice off
     everything from that header downward (already-merged history).
     If the resulting slice is empty, no new updates have been posted
     since last time — mark the Airtable record 'Applied' and exit
     WITHOUT calling Groq. First run (memory file absent) keeps only
     the topmost block to establish a baseline.
  6. Send {Editor system prompt, current file, DELTA TEXT ONLY} to
     Groq's llama-3.3-70b-versatile via the OpenAI-compatible chat
     completions endpoint for a SEMANTIC MERGE — outdated rules are
     rewritten or removed in place, new rules are slotted into the
     topically-correct existing section. Never appended blindly.
  7. Apply safety gates: strip an outer markdown fence if Groq wrapped
     the document, refuse to overwrite if the merged file is below
     MIN_MERGED_CHARS (sub-floor result = fragment / truncation).
  8. Overwrite qwen_rules/03_vapi_constraints.md.
  9. Update qwen_rules/last_processed_header.txt with the topmost H2
     header from the delta so the next run cuts at the new boundary.
 10. Only AFTER both file writes succeed, PATCH the Airtable record's
     Status to 'Applied'. If the PATCH itself fails, the files are
     already updated — we surface the record ID loudly so the
     operator can flip it manually rather than silently rolling
     back a successful merge.

Failure modes (all exit non-zero, leaving the constraints file
unchanged unless the merge had already succeeded):
  - Missing AIRTABLE_API_KEY / AIRTABLE_BASE_ID /
    GROQ_API_KEY env vars
  - Constraints file missing                       -> exit 1
  - Airtable fetch HTTP / network failure          -> exit 1
  - Pending record has empty "Update Text"          -> exit 1
  - Incoming changelog has no H2 (## ...) headers   -> exit 1
  - last_processed_header.txt header not found in
    incoming changelog (renamed/truncated/edited)   -> exit 1
  - Groq API unreachable / auth / HTTP / timeout    -> exit 1
  - Groq returned an empty response                 -> exit 1
  - Merge output below MIN_MERGED_CHARS floor       -> exit 1
  - Constraints file write OS error                 -> exit 1
  - Memory file write OS error                     -> exit 2
                                                       (constraints file
                                                        IS updated; memory
                                                        is now stale)
  - Airtable PATCH back to Applied failed           -> exit 2
                                                       (files ARE updated;
                                                        operator must flip
                                                        the record manually)

Usage:
    python semantic_rule_updater.py
"""

import io
import os
import re
import sys
import urllib.parse

import requests
from dotenv import load_dotenv

# Force UTF-8 stdout/stderr on Windows (mirrors auto_triage.py)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

# Airtable table name — referenced verbatim in the URL path.
AIRTABLE_TABLE     = "Vapi Changelogs"
AIRTABLE_BASE_URL  = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"

# Field names on the "Vapi Changelogs" table.
F_STATUS      = "Status"
F_UPDATE_TEXT = "Update Text"

# Status vocabulary — single source of truth for the queue state machine.
STATUS_PENDING = "Pending"
STATUS_APPLIED = "Applied"

# Groq API — OpenAI-compatible chat completions endpoint.
# llama-3.3-70b-versatile is Groq's top-tier 70B model; it serves the
# editor role at near-instant latency, replacing the previous local
# Ollama / Qwen 2.5-Coder pipeline. GROQ_API_KEY is loaded once at
# import time so missing-credential failures surface in the env
# validation step before any network call is made.
GROQ_URL      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL    = "llama-3.3-70b-versatile"
GROQ_API_KEY  = os.getenv("GROQ_API_KEY")

CONSTRAINTS_REL  = os.path.join("qwen_rules", "03_vapi_constraints.md")
LAST_HEADER_REL  = os.path.join("qwen_rules", "last_processed_header.txt")

# Hard floor for the merged-file size — refuse to overwrite if Groq
# returns anything smaller. The current constraints file is ~19 KB; a
# clean merge that prunes some stale rules might shrink ~20%, but
# anything below 6 KB means Groq returned a fragment, an error message,
# or a half-truncated response. Operators can adjust this if a future
# Vapi platform overhaul legitimately requires a major shrink.
MIN_MERGED_CHARS = 6000

# Outer-fence stripping — Groq sometimes wraps the whole reply in
# ```markdown ... ``` despite being told not to. We peel exactly one
# outer fence; the inner JSON code fences inside the constraints file
# are preserved untouched.
_OPENING_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n")
_CLOSING_FENCE_RE = re.compile(r"\n```\s*$")


# ── System prompt for the Senior Editor role ─────────────────────────────────

EDITOR_SYSTEM_PROMPT = """\
You are a Senior Technical Editor maintaining the Vapi Assistants API
constraints reference for an LLM-driven triage agent. You will be given:

  1. The CURRENT CONSTRAINTS file — the authoritative rulebook today.
  2. An INCOMING UPDATE — raw text scraped from a Vapi changelog,
     release note, or platform announcement.

Your job is to **semantically merge** the incoming information into the
current file and return ONE complete replacement Markdown document.

Hard rules:

- **Never append blindly.** Do NOT paste the incoming text under a
  "Latest Updates" or "Changelog" heading at the bottom. The output
  must read as a single coherent reference, not a layered changelog.
- **Rewrite contradicted rules in place.** If a current rule conflicts
  with the new info — a deprecated endpoint, a renamed field, a
  changed valid value, a removed provider — rewrite that rule on the
  spot to reflect the new truth. DELETE rules the update has made
  obsolete; do not leave them as historical commentary.
- **Integrate where the topic fits.** A new schema path goes into the
  Schema Path Quick Reference (§1). A new provider/model pair goes
  into the Valid String Values tables (§2). A new SSML tag goes into
  the SSML Tag Reference (§3). A new failure mode or anti-pattern
  goes into Hard Rules / Common Anti-Patterns (§5 / §5.1). Do NOT
  create a new top-level section for content that belongs in an
  existing one.
- **Preserve formatting and structure.** Match the existing Markdown
  style: H2/H3 headings, table format, code fences, italic emphasis,
  bullet-list cadence. Keep the existing section numbers exactly
  (§0, §0.1, §0.2, §0.5, §1, §2, §2.1, §2.2, §2.3, §3, §5, §5.1) —
  other modules in the rulebook cross-reference this file by those
  numbers, so renumbering breaks every cross-reference.
- **Preserve the §0.3 / §0.4 gap intentionally.** Sections 0.3 and 0.4
  live in a different module of the rulebook (qwen_rules/01_qwen_persona.md).
  This file deliberately jumps from §0.2 to §0.5. Do NOT fill that gap;
  do NOT renumber §0.5 to §0.3.
- **Output only the merged Markdown document.** No preface ("Here is
  the merged file..."), no postscript ("Let me know if..."), no code
  fences wrapping the entire document. The output must be ready to
  write directly to disk.

If the incoming update is irrelevant — touches nothing the current
file documents — return the CURRENT FILE UNCHANGED, byte for byte.
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def fail(msg: str, code: int = 1) -> None:
    """Print a labeled error to stderr and exit with the given non-zero code."""
    print(f"[error] {msg}", file=sys.stderr)
    sys.exit(code)


def read_text_file(path: str, label: str) -> str:
    """
    Read a UTF-8 text file from disk. Distinguish three failure modes
    explicitly so the operator knows what to fix:
      - file not found     (path / cwd issue)
      - file unreadable    (permissions / encoding)
      - file empty         (touched but never populated)
    """
    if not os.path.isfile(path):
        fail(f"{label} not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        fail(f"Could not read {label} ({path}): {exc}")
    if not content.strip():
        fail(f"{label} is empty: {path}")
    return content


def strip_outer_markdown_fence(text: str) -> str:
    """
    If Groq wrapped the whole reply in ```markdown ... ``` despite the
    system prompt telling it not to, peel exactly one outer fence.
    Inner JSON code fences (legitimately used in the constraints file)
    are preserved.
    """
    s = text.strip()
    s = _OPENING_FENCE_RE.sub("", s, count=1)
    s = _CLOSING_FENCE_RE.sub("", s, count=1)
    return s.strip()


# ── Delta extraction ──────────────────────────────────────────────────────────
# Make.com scrapes the entire Vapi changelog on every run, so the
# Airtable "Update Text" usually contains the full historical stream.
# We only want Groq to see what's NEW since the last successful merge —
# otherwise every run replays years of prose through the LLM, burning
# GPU time and risking drift on rules that are already integrated.
#
# Vapi's convention is newest-first: the most recent update sits at
# the top, older ones beneath. Everything from the most recent
# already-merged H2 header DOWNWARD is history we already saw. The
# boundary header is persisted to qwen_rules/last_processed_header.txt
# as a single line, e.g. "## What's New — May 1, 2024".


def find_h2_headers(text: str) -> list[tuple[int, str]]:
    """
    Return [(start_pos, header_line), ...] for every H2 (## ...) header
    in `text`, in source order. Headers inside fenced code blocks
    (```...```) are excluded so a Python comment like `## TODO` in an
    embedded code sample isn't misread as a section boundary.

    Convention: a header line must start at column 0 with `## ` and
    contain at least one further character. Indented or H3+ lines are
    ignored.
    """
    results: list[tuple[int, str]] = []
    in_code = False
    pos = 0
    for line in text.splitlines(keepends=True):
        if line.startswith("```"):
            in_code = not in_code
        elif (not in_code
              and line.startswith("## ")
              and len(line.strip()) > 3):
            results.append((pos, line.rstrip("\r\n")))
        pos += len(line)
    return results


def read_last_header(path: str) -> str | None:
    """
    Return the persisted last-processed header (stripped), or None if
    the memory file does not exist or is empty. None signals first-run
    baseline — process only the topmost block, discard the rest of
    the historical changelog.
    """
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError as exc:
        fail(f"Could not read memory file ({path}): {exc}")
    return content or None


def write_last_header(path: str, header: str) -> None:
    """
    Persist the topmost header from a successful merge so the next run
    knows where to cut. Failure exits with code 2 — the constraints
    file is already updated by this point, so a stale memory file
    must be surfaced rather than silently masked.
    """
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(header.rstrip() + "\n")
    except OSError as exc:
        fail(f"Could not write memory file ({path}): {exc}", code=2)


def extract_delta(
    text: str,
    last_header: str | None,
    record_id: str,
) -> tuple[str, str | None]:
    """
    Slice `text` to keep only the changelog content NEWER than
    `last_header`. Returns (delta_text, new_topmost_header).

    delta_text         -- the slice to send to Groq. Empty string means
                          "no new updates since last run".
    new_topmost_header -- the H2 header that should be persisted to
                          memory after a successful merge. None if
                          the slice has no H2 boundary (degenerate
                          preamble-only change; the caller must skip
                          the memory update).

    First-run baseline (`last_header is None`):
      Slice up to the START of the SECOND H2 header — keeping only
      the topmost block, discarding the entire historical tail. This
      establishes a baseline so future runs only see deltas. If the
      changelog has only one H2 header, the whole text IS the topmost
      block and is processed in full.

    Subsequent runs:
      Find the H2 whose text equals `last_header` and slice up to its
      start. Everything from that header down is already-merged
      history. If `last_header` is not present in `text`, FAIL LOUDLY
      — the operator should investigate (renamed header, truncated
      Make.com scrape, manually-edited memory file). Silently
      reprocessing the whole history would burn GPU time and risk
      drift on rules already semantically integrated.
    """
    headers = find_h2_headers(text)
    if not headers:
        fail(
            f"Record {record_id}: incoming changelog has no H2 (## ...) "
            f"section headers. Delta extraction requires structured "
            f"updates. Inspect the Airtable record before retrying."
        )

    if last_header is None:
        # First-run baseline. Discard everything from the second header
        # downward; if there's only one header, the whole text IS the
        # topmost block.
        topmost_pos, topmost_line = headers[0]
        if len(headers) == 1:
            delta_text = text
        else:
            cut = headers[1][0]
            delta_text = text[:cut]
        return (delta_text, topmost_line)

    # Subsequent run — find the persisted header verbatim (stripped).
    last_header_norm = last_header.strip()
    for pos, line in headers:
        if line.strip() == last_header_norm:
            delta_text = text[:pos]
            slice_headers = [(p, ln) for (p, ln) in headers if p < pos]
            new_topmost = slice_headers[0][1] if slice_headers else None
            return (delta_text, new_topmost)

    fail(
        f"Record {record_id}: last processed header not found in "
        f"incoming changelog: {last_header!r}. The header may have "
        f"been renamed, the Make.com scrape may not include "
        f"far-enough history, or qwen_rules/last_processed_header.txt "
        f"was edited manually. Reset the memory file (delete it) for "
        f"a fresh first-run baseline, or fix the upstream issue."
    )


# ── Airtable ──────────────────────────────────────────────────────────────────

def airtable_headers() -> dict:
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type":  "application/json",
    }


def airtable_table_url() -> str:
    return f"{AIRTABLE_BASE_URL}/{urllib.parse.quote(AIRTABLE_TABLE)}"


def fetch_oldest_pending() -> dict | None:
    """
    Return the oldest record on the Vapi Changelogs table whose Status
    is exactly 'Pending', or None if the queue is empty.

    "Oldest" is determined by the `createdTime` metadata field that
    Airtable returns on every record — no schema dependency on a
    user-defined "Created Time" column. We filter server-side by
    Status, fetch up to one page (100 records), then sort
    client-side. For a changelog queue this is well within scale —
    if pending ever exceeds 100 the operator has a process problem,
    not a script problem.
    """
    params = {
        "filterByFormula": f"{{{F_STATUS}}}='{STATUS_PENDING}'",
        "pageSize": 100,
    }
    resp = requests.get(
        airtable_table_url(),
        headers=airtable_headers(),
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    records = resp.json().get("records", []) or []
    if not records:
        return None
    records.sort(key=lambda r: r.get("createdTime", ""))
    return records[0]


def mark_record_applied(record_id: str) -> None:
    """
    PATCH the given record's Status to 'Applied'. Raises on HTTP
    failure — the caller is responsible for surfacing the failure
    loudly, since the merged file has already been written by the
    time we get here.

    typecast=True lets Airtable auto-create the 'Applied' single-select
    option if the schema doesn't list it yet (matches the auto_triage.py
    convention for status writes).
    """
    payload = {"fields": {F_STATUS: STATUS_APPLIED}, "typecast": True}
    resp = requests.patch(
        f"{airtable_table_url()}/{record_id}",
        headers=airtable_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()


# ── Groq API ──────────────────────────────────────────────────────────────────

def call_groq(current: str, update: str) -> str | None:
    """
    Send the merge request to Groq's OpenAI-compatible chat completions
    endpoint. Returns the merged Markdown on success or None on any
    failure (network unreachable, request timeout, authentication
    failure, HTTP error, malformed JSON, empty response). The caller
    MUST treat a None return as "do not overwrite the on-disk file."

    The system prompt and user content are passed as separate messages
    (role: system / role: user) per the OpenAI chat schema Groq
    implements — not concatenated into a single prompt string. This
    is the only structural change from the previous Ollama path; the
    delta extraction layer above and the Airtable / safety-gate layers
    below see the same string in / string out contract.
    """
    user_prompt = (
        "=== CURRENT CONSTRAINTS (qwen_rules/03_vapi_constraints.md) ===\n"
        + current
        + "\n\n=== INCOMING VAPI UPDATE ===\n"
        + update
        + "\n\nReturn the FULL merged Markdown file now. Output only "
          "the file content — no preface, no postscript, no fences "
          "wrapping the whole document."
    )

    print(f"  [prompt] system={len(EDITOR_SYSTEM_PROMPT)} chars | "
          f"current={len(current)} chars | update={len(update)} chars | "
          f"total={len(EDITOR_SYSTEM_PROMPT) + len(user_prompt)} chars")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": EDITOR_SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature": 0.1,        # Editor work, not creative writing
        "top_p":       0.9,
        "stream":      False,
        # max_tokens intentionally omitted — explicitly reserving 8K
        # tripped Groq's 413 (request too large) free-tier ceiling.
        # Letting Groq allocate the output window dynamically keeps the
        # 70B model in play without hitting the cap.
    }

    print(f"  [groq] Sending merge request to {GROQ_MODEL} ...")
    try:
        resp = requests.post(
            GROQ_URL,
            headers=headers,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        print("  [groq] FAILED — Groq API not reachable. Check network / DNS.")
        return None
    except requests.exceptions.Timeout:
        print("  [groq] FAILED — Groq API timed out (>120s).")
        return None
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (401, 403):
            print(f"  [groq] FAILED — authentication error (HTTP {status}). "
                  f"Check GROQ_API_KEY in .env.")
        elif status == 429:
            print(f"  [groq] FAILED — rate limit / quota exceeded "
                  f"(HTTP 429): {exc}")
        else:
            print(f"  [groq] FAILED — HTTP {exc}")
        return None
    except requests.exceptions.RequestException as exc:
        print(f"  [groq] FAILED — request error: {exc}")
        return None

    try:
        data = resp.json()
    except ValueError as exc:
        print(f"  [groq] FAILED — could not parse JSON response: {exc}")
        return None

    choices = data.get("choices") or []
    if not choices:
        print("  [groq] FAILED — response contained no choices.")
        return None
    body = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not body:
        print("  [groq] FAILED — empty response body.")
        return None
    return body


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    base = os.path.dirname(os.path.abspath(__file__))
    constraints_path = os.path.join(base, CONSTRAINTS_REL)

    print("=" * 64)
    print("  Engage Mate AI — Semantic Rule Updater")
    print(f"  Source      : Airtable / {AIRTABLE_TABLE}")
    print(f"  Constraints : {CONSTRAINTS_REL}")
    print(f"  Model       : {GROQ_MODEL}")
    print("=" * 64)

    # 1. Validate Airtable + Groq env up front. Failing here means we
    #    never reach the LLM, so a missing key can't burn an API call
    #    or leave the queue half-processed.
    missing = [v for v in ("AIRTABLE_API_KEY", "AIRTABLE_BASE_ID",
                           "GROQ_API_KEY")
               if not os.getenv(v)]
    if missing:
        fail(f"Missing .env vars: {', '.join(missing)}")

    # 2. Read the current constraints file. read_text_file() exits
    #    cleanly on missing / empty.
    current = read_text_file(constraints_path, "Constraints file")
    print(f"  [read] {CONSTRAINTS_REL} : {len(current)} chars")

    # 3. Pull the oldest Pending record from Airtable.
    print(f"  [airtable] Querying {AIRTABLE_TABLE} for {F_STATUS}='{STATUS_PENDING}' ...")
    try:
        record = fetch_oldest_pending()
    except requests.HTTPError as exc:
        fail(f"Airtable fetch failed: {exc}")
    except requests.exceptions.RequestException as exc:
        fail(f"Airtable network error: {exc}")

    if record is None:
        print(f"  [airtable] No pending changelogs. Nothing to merge.")
        print("=" * 64)
        return

    record_id  = record["id"]
    fields     = record.get("fields", {})
    update_text = (fields.get(F_UPDATE_TEXT) or "").strip()
    created_at = record.get("createdTime", "?")

    print(f"  [airtable] Picked oldest pending: {record_id}  (created {created_at})")
    print(f"  [airtable] {F_UPDATE_TEXT}: {len(update_text)} chars")

    if not update_text:
        # Operator-error: a Pending row with no body. Surface it
        # loudly — don't silently auto-mark Skipped, the bad row
        # needs human attention.
        fail(f"Record {record_id} has empty '{F_UPDATE_TEXT}'. "
             f"Fill in the changelog text or change Status off "
             f"'{STATUS_PENDING}' before re-running.")

    # 4. Delta extraction. Make.com dumps the entire historical
    #    changelog every run; we only want Groq to see what's new
    #    since the last successful merge. The boundary header is
    #    persisted in qwen_rules/last_processed_header.txt — empty
    #    or absent on first run triggers the baseline path.
    last_header_path = os.path.join(base, LAST_HEADER_REL)
    last_header = read_last_header(last_header_path)
    print(f"  [delta] last processed header: "
          f"{last_header if last_header else '(none — first-run baseline)'}")

    delta_text, new_topmost = extract_delta(update_text, last_header, record_id)
    print(f"  [delta] slice: {len(delta_text)} chars  "
          f"(topmost: {new_topmost or '(none in slice)'})")

    if not delta_text.strip():
        # Nothing newer than the stored header — the constraints file
        # is already current for this Airtable row. Flip it to Applied
        # so it doesn't keep getting picked up by future runs, and
        # exit WITHOUT calling Groq.
        print("  [delta] No new updates found since last run.")
        try:
            mark_record_applied(record_id)
        except requests.HTTPError as exc:
            fail(
                f"No new content to merge, but Airtable PATCH to "
                f"'{STATUS_APPLIED}' failed: {exc}. Manually flip "
                f"record {record_id} to clear the queue.",
                code=2,
            )
        except requests.exceptions.RequestException as exc:
            fail(
                f"No new content to merge, but Airtable PATCH to "
                f"'{STATUS_APPLIED}' failed (network): {exc}. "
                f"Manually flip record {record_id} to clear the queue.",
                code=2,
            )
        print(f"  [airtable] Record {record_id} marked '{STATUS_APPLIED}' "
              f"(no Groq call).")
        print("=" * 64)
        print(f"  [done] {CONSTRAINTS_REL} unchanged — already current.")
        print("=" * 64)
        return

    # 5. Send the DELTA (not the full historical text) to Groq.
    merged = call_groq(current, delta_text)
    if merged is None:
        fail("Groq merge call failed — constraints file UNCHANGED, "
             f"record {record_id} left as '{STATUS_PENDING}'.")

    # 6. Defensive cleanup — peel any outer ```markdown fence Groq may
    #    have wrapped the whole document in despite being told not to.
    merged = strip_outer_markdown_fence(merged)

    # 7. Sanity gate — refuse to overwrite if the merge collapsed the
    #    file. A real merge of a small delta should land within a
    #    few percent of the original size; a sub-floor result means
    #    Groq returned a fragment, an error message, or got truncated
    #    mid-stream.
    if len(merged) < MIN_MERGED_CHARS:
        fail(
            f"Merge output suspiciously short ({len(merged)} chars, "
            f"floor is {MIN_MERGED_CHARS}). Refusing to overwrite "
            f"{CONSTRAINTS_REL}. Record {record_id} left as "
            f"'{STATUS_PENDING}'. Inspect Groq's output before retrying."
        )

    # 8. Write the merged document back to disk. The constraints file
    #    is only ever flushed via this single open/write call, so the
    #    on-disk file is unchanged unless the OS write succeeds.
    try:
        with open(constraints_path, "w", encoding="utf-8") as f:
            f.write(merged)
    except OSError as exc:
        fail(f"Could not write {constraints_path}: {exc}")

    byte_delta = len(merged) - len(current)
    print(f"  [write] {CONSTRAINTS_REL} updated ({len(merged)} chars, "
          f"delta {byte_delta:+d}).")

    # 9. Update the memory file with the topmost header from the delta
    #    we just merged. write_last_header exits code 2 on failure —
    #    by this point the constraints file is already updated, so a
    #    stale memory file must be surfaced rather than silently
    #    masked.
    if new_topmost:
        write_last_header(last_header_path, new_topmost)
        print(f"  [memory] {LAST_HEADER_REL} -> {new_topmost!r}")
    else:
        # Degenerate: delta has content but no H2 header (preamble-only
        # change). Skip the memory update — next run will re-process
        # the same delta, but Groq is roughly idempotent on re-merging
        # an already-applied change so the file converges either way.
        print(f"  [memory] {LAST_HEADER_REL} unchanged "
              f"(delta has no H2 header)")

    # 10. PATCH the record to 'Applied'. Order matters — only after
    #     BOTH the constraints file AND the memory file are safely on
    #     disk. If THIS step fails, both files are already updated;
    #     we surface the record ID + exit 2 so the operator knows
    #     to flip the row manually rather than silently rolling
    #     back a successful merge.
    try:
        mark_record_applied(record_id)
    except requests.HTTPError as exc:
        fail(
            f"Merge succeeded and {CONSTRAINTS_REL} is on disk, but "
            f"the Airtable PATCH to '{STATUS_APPLIED}' failed: {exc}. "
            f"Manually flip record {record_id} from '{STATUS_PENDING}' "
            f"to '{STATUS_APPLIED}' to clear the queue.",
            code=2,
        )
    except requests.exceptions.RequestException as exc:
        fail(
            f"Merge succeeded and {CONSTRAINTS_REL} is on disk, but "
            f"the Airtable PATCH to '{STATUS_APPLIED}' failed (network): "
            f"{exc}. Manually flip record {record_id} from "
            f"'{STATUS_PENDING}' to '{STATUS_APPLIED}'.",
            code=2,
        )

    print(f"  [airtable] Record {record_id} marked '{STATUS_APPLIED}'.")
    print("=" * 64)
    print("  [done] Merge complete.")
    print("=" * 64)


if __name__ == "__main__":
    main()
