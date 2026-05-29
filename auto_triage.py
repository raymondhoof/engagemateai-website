"""
auto_triage.py — Engage Mate AI  (Human-in-the-Loop Diagnostic Edition)
=======================================================================
Pipeline:
  1. Pull Airtable records where Status == 'Incoming'.
  2. Parse each raw Transcript field into three variables:
        Call_ID          — UUID from the email body
        Client_Note      — client's reply text (between headers and the
                           forwarded-message marker)
        Clean_Transcript — dialogue between the 'Transcript' and
                           'Recording URL' markers
  3. Fetch the LIVE Vapi Workflow JSON.
  4. ESCAPE HATCH: if Clean_Transcript is empty, skip the LLM and write
     the stub response.
  5. Otherwise send {compiled qwen_rules/*.md as system, Client_Note +
     Clean_Transcript + Workflow JSON as user} to Llama 4 Scout via
     Groq's /chat/completions endpoint. The rulebook is built at runtime
     from every .md file in qwen_rules/, read in alphabetical order and
     joined with markdown dividers — see load_vapi_rules().
  6. Write Call_ID / jsonPatch / rationale back to Airtable with Status:
       - 'Needs Review'      — model returned a non-empty jsonPatch
       - 'Positive Feedback' — client praise / no fix needed (empty patch)
       - 'Skipped'           — escape hatch (no dialogue / junk email)
       - 'Error'             — Groq timeout / unparseable JSON
     ('Applied' is written by a separate deployment script, not here.)
     60s cooldown between records to stay under the 30k TPM free-tier
     limit on Groq.

Usage:
    python auto_triage.py                           # process ALL Incoming records
    python auto_triage.py --limit 1                 # test mode — one record only
    python auto_triage.py --record-id recXXXXXXXX   # target a single record
"""

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv

# Optional dependency — `pip install json-repair` to enable. When present,
# parse_qwen_response uses it as a last-resort fallback to repair
# malformed JSON (missing quotes, trailing commas, unescaped strings).
# Without it, the parser still works — it just skips the repair pass.
try:
    from json_repair import repair_json
    _JSON_REPAIR_AVAILABLE = True
except ImportError:
    _JSON_REPAIR_AVAILABLE = False

# Force UTF-8 stdout/stderr on Windows (avoids charmap crash on arrows/emojis)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Logging ───────────────────────────────────────────────────────────────────
# Required because the script runs unattended via Windows Task Scheduler.
# Outputs to BOTH the console (stdout) AND a rotating log file alongside
# the script. The log file lives next to auto_triage.py — NOT the CWD,
# which Task Scheduler may set unpredictably to System32 or similar.
LOG_FILE_NAME    = "autopilot.log"
LOG_FORMAT       = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATE_FORMAT  = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES    = 2 * 1024 * 1024   # 2 MB per file
LOG_BACKUP_COUNT = 5                 # keeps autopilot.log + .1 .. .5


def _setup_logging() -> logging.Logger:
    """Configure the 'autopilot' logger once. Idempotent across re-imports."""
    logger = logging.getLogger("autopilot")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT)

    log_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), LOG_FILE_NAME
    )
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    # Pin the console handler to the now-UTF-8-wrapped stdout so emoji /
    # arrow / em-dash glyphs survive on Windows consoles.
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False
    return logger


log = _setup_logging()

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

VAPI_API_KEY      = os.getenv("VAPI_API_KEY")
AIRTABLE_API_KEY  = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID  = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE    = os.getenv("AIRTABLE_TABLE_NAME")
VAPI_ASSISTANT_ID = os.getenv("VAPI_ASSISTANT_ID")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")

GROQ_API_URL       = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL         = "meta-llama/llama-4-scout-17b-16e-instruct"
INTER_RECORD_SLEEP = 120       # seconds — Scout free tier is 30k TPM (vs 12k on 70B); single ~16-20k-tok request now fits one window
RULES_DIR          = "qwen_rules"

VAPI_BASE         = "https://api.vapi.ai"
AIRTABLE_BASE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"

# Airtable field names (verified against live schema)
F_STATUS       = "Status"
F_CALL_ID      = "Call ID"
F_TRANSCRIPT   = "Transcript"
F_PROPOSED_FIX = "Proposed Fix"
F_RATIONALE    = "Rationale"
F_ASSISTANT_ID   = "Assistant ID"
F_ASSISTANT_NAME = "Assistant Name"
# Vapi end-of-call-report dropped into Airtable by Make.com. Second
# evidence source alongside the transcript — endedReason, latency,
# cost, token usage, pipeline/LLM errors. Always a string (Make.com
# writes the raw server log text, or empty if the webhook didn't fire).
F_VAPI_TELEMETRY = "Vapi Telemetry"
# Presence of a value in this field signals a HUMAN flagged the call —
# Make.com writes the Gmail message ID (or any non-empty marker) when a
# client forwards a complaint/feedback. Empty = no human involvement,
# record came in via the automatic Vapi webhook only. The pre-triage
# state-machine filter uses this as the "human override" signal that
# forces Qwen analysis regardless of what telemetry says.
F_EMAIL_ID = "Email ID"

# Fallback written to F_ASSISTANT_NAME when the Vapi lookup fails — keeps
# the triage run from ever crashing on a bad / revoked assistant ID.
UNKNOWN_ASSISTANT_NAME = "Unknown Assistant"

# Airtable Status vocabulary — the single source of truth for this script.
# This state machine is deliberately strict to prevent schema drift.
#   - STATUS_INCOMING:          fetch filter — only these are processed.
#   - STATUS_SKIPPED:           escape-hatch (junk email / no dialogue).
#   - STATUS_ERROR:             Groq timeout, connection failure, or bad JSON.
#   - STATUS_NEEDS_REVIEW:      model produced a non-empty jsonPatch; awaiting human.
#   - STATUS_POSITIVE_FEEDBACK: forwarded client email contains EXPLICIT
#                               praise of the bot (Debbie said it did well).
#   - STATUS_NO_FIX_NEEDED:     ordinary call achieved its goal, caller
#                               hung up early, or no actionable anomaly —
#                               but the client did NOT explicitly praise.
# 'Applied' exists in the schema but is written by a separate deployment
# script, not this one. Do not emit it here.
STATUS_INCOMING          = "Incoming"
STATUS_SKIPPED           = "Skipped"
STATUS_ERROR             = "Error"
STATUS_NEEDS_REVIEW      = "Needs Review"
STATUS_POSITIVE_FEEDBACK = "Positive Feedback"
STATUS_NO_FIX_NEEDED     = "No Fix Needed"

# Escape-hatch payload — emitted when no dialogue is extractable
ESCAPE_HATCH = {
    "jsonPatch": {},
    "rationale": "Insufficient dialogue data to diagnose.",
}

# ── Unicode sanitization ──────────────────────────────────────────────────────
# Qwen occasionally emits literal `\uXXXX` escape sequences as raw text
# (e.g. `"Benz—WA"` instead of the real em-dash codepoint), fancy
# punctuation, or zero-width / ASCII control chars. If we write those
# straight to Airtable, the reviewer and the downstream deployment script
# both see noisy strings that break TTS and render poorly in the Vapi
# dashboard. Clean at the boundary — BEFORE anything lands in Airtable —
# so the sanitized value is what a human reviews and what
# apply_approved_fix.py later pushes to Vapi. (apply_approved_fix.py
# re-sanitizes defensively, but the canonical clean happens here.)

# Fancy Unicode punctuation → ASCII equivalents. Conservative: only chars
# that cause rendering / TTS issues are mapped. Accented letters and
# non-Latin scripts pass through untouched as valid UTF-8.
_ASCII_PUNCT_MAP = {
    0x2013: "-",     # EN DASH          -
    0x2014: "-",     # EM DASH          -
    0x2015: "-",     # HORIZONTAL BAR
    0x2018: "'",     # LEFT SINGLE QUOTE
    0x2019: "'",     # RIGHT SINGLE QUOTE (apostrophe)
    0x201A: "'",     # SINGLE LOW-9 QUOTE
    0x201B: "'",     # SINGLE HIGH-REVERSED-9 QUOTE
    0x201C: '"',     # LEFT DOUBLE QUOTE
    0x201D: '"',     # RIGHT DOUBLE QUOTE
    0x201E: '"',     # DOUBLE LOW-9 QUOTE
    0x201F: '"',     # DOUBLE HIGH-REVERSED-9 QUOTE
    0x2026: "...",   # HORIZONTAL ELLIPSIS
    0x00A0: " ",     # NO-BREAK SPACE
    0x2022: "*",     # BULLET
    0x00B7: "*",     # MIDDLE DOT
}

# Zero-width + BOM chars (invisible but break tokenizers / grep).
# Hex-escape form keeps the source file pure ASCII.
_ZERO_WIDTH_RE = re.compile(r"[\u200B-\u200D\u2060\uFEFF]")
# ASCII control chars except newline (\n), carriage return (\r), tab (\t).
_CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
# Literal 6-char "\uXXXX" that slipped through as text rather than being
# decoded — happens when Qwen double-escapes output.
_LITERAL_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def sanitize_text(text):
    """
    Return a clean UTF-8 version of `text` suitable for Airtable / Vapi.
    Non-strings pass through untouched so this is safe to call on mixed
    JSON values via sanitize_value().
    """
    if not isinstance(text, str):
        return text
    if "\\u" in text:
        text = _LITERAL_UNICODE_ESCAPE_RE.sub(
            lambda m: chr(int(m.group(1), 16)), text
        )
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_ASCII_PUNCT_MAP)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    return text


def sanitize_value(value):
    """
    Recursively walk a JSON-like value (Qwen's parsed output) and apply
    sanitize_text at every string leaf. Dicts, lists, and tuples are
    rebuilt; numbers, bools, and None pass through.
    """
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_value(v) for v in value)
    if isinstance(value, dict):
        return {k: sanitize_value(v) for k, v in value.items()}
    return value


# ── Pre-triage state machine ─────────────────────────────────────────────────
# Qwen is expensive (GPU spin-up + 16k context + 5-min cooldown between
# records) and it has no business trying to "fix" infrastructure
# failures — a silence timeout is not a prompt bug, a pipeline outage
# is not a logic bug. Every Incoming record is gated through three rules
# BEFORE we touch Qwen:
#
#   Rule 1 — infra error substring in telemetry AND Email ID empty
#            → Skip (rationale: "Infrastructure error - no prompt patch needed")
#   Rule 2 — no infra error, Email ID empty, Success Evaluation != failure
#            → Skip (rationale: "Clean call, awaiting potential human feedback")
#   Rule 3 — Email ID NOT empty OR Success Evaluation == failure
#            → fall through to the full Qwen pipeline
#
# Interaction with qwen_rules/02_triage_logic.md §7.1 (silence-timed-out proactive fix):
# §7.1 now only fires when a human flagged the call (Email ID present),
# because an unflagged silence timeout is filtered out by Rule 1. If
# that's undesired, §7.1 should be loosened or this filter tightened —
# see the roadmap.

# Substring patterns — ANY one of these appearing inside endedReason
# classifies the call as an infrastructure failure. Ordered by frequency
# in the current deployment.
_INFRASTRUCTURE_ERROR_PATTERNS: tuple[str, ...] = (
    "pipeline-error",
    "call-start-error",
    "silence-timed-out",
)

# Make.com dumps telemetry as a formatted text blob — one key per line,
# e.g.  "Ended Reason: pipeline-error-openai-llm-failed\nSuccess
# Evaluation: false". Accept `:` or `=` separator and underscore/space
# spelling so the filter survives reasonable formatting drift.
_TELEMETRY_ENDED_REASON_RE = re.compile(
    r"(?:ended[\s_]*reason|endedReason)\s*[:=]\s*([^\r\n]+)",
    re.IGNORECASE,
)
_TELEMETRY_SUCCESS_EVAL_RE = re.compile(
    r"(?:success[\s_]*evaluation|successEvaluation)\s*[:=]\s*([^\r\n]+)",
    re.IGNORECASE,
)


def parse_telemetry(text: str) -> tuple[str, str]:
    """
    Extract (endedReason, successEvaluation) from Make.com's telemetry
    blob. Both values come back lowercased, with surrounding quotes /
    trailing commas stripped; absent fields return ''. Substring matches
    downstream tolerate minor formatting drift, so light normalization
    is sufficient — we don't need a full JSON parser here.
    """
    if not text:
        return ("", "")
    m = _TELEMETRY_ENDED_REASON_RE.search(text)
    ended_reason = (
        m.group(1).strip().strip('"').strip("'").strip(",").lower() if m else ""
    )
    m = _TELEMETRY_SUCCESS_EVAL_RE.search(text)
    success_eval = (
        m.group(1).strip().strip('"').strip("'").strip(",").lower() if m else ""
    )
    return (ended_reason, success_eval)


def is_infrastructure_error(ended_reason: str) -> bool:
    """endedReason contains one of the known infra-failure substrings."""
    if not ended_reason:
        return False
    return any(p in ended_reason for p in _INFRASTRUCTURE_ERROR_PATTERNS)


def success_evaluation_is_failure(success_eval: str) -> bool:
    """
    True iff successEvaluation EXPLICITLY says the call failed. Vapi's
    rubric can emit `true` / `false` / `unknown` / numeric scores / JSON
    objects depending on the assistant's configuration; we flag only
    unambiguous negative verdicts. Anything else (empty, `unknown`,
    `true`, a score, a JSON blob) leaves the record in the "no failure
    signal" bucket so an ambiguous rubric never triggers a false
    positive triage.
    """
    if not success_eval:
        return False
    return success_eval in {"false", "fail", "failed", "failure", "no"}


# ── Extraction: raw email text → 3 variables ──────────────────────────────────

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# "Vapi Call ID" label followed (loosely) by a UUID — tolerates emoji prefix
CALL_ID_LABELED_RE = re.compile(
    r"Vapi\s+Call\s+ID[^\w-]*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

# Client signature anchor — the Gmail API now delivers clean text where
# the note starts at index 0. The signature block begins with Debbie's name
# (optionally bold-wrapped with asterisks). Everything before it is the note.
SIGNATURE_RE = re.compile(r"^\*?Debbie\s+Gomes\*?$", re.IGNORECASE | re.MULTILINE)

# Keyrenter email boundary — at least ONE of these markers must appear
# for a raw body to be considered a valid Keyrenter client reply. Junk
# emails (LinkedIn, spam, random newsletters) lack all three and will
# be rejected by extract_client_note, which routes them to Skipped.
#   • "---------- Forwarded message ---------" (Gmail-forwarded Vapi alert)
#   • "Begin forwarded message:" (Apple Mail forwarded)
#   • "From: Raymond Vargas" (automated Vapi→email sender line)
#   • "AI Assistant Call Sum(m)ary" (the original alert's header — tolerant
#     of the existing typo in the template)
KEYRENTER_BOUNDARY_RE = re.compile(
    r"-{3,}\s*Forwarded message"
    r"|Begin\s+forwarded\s+message"
    r"|From:\s*Raymond\s+Vargas"
    r"|AI\s+Assistant\s+Call\s+Sum+ary",
    re.IGNORECASE,
)

# Gmail header patterns used by extract_client_note to strip the top-of-email
# metadata (sender name, timestamp, 'to me, X' line) that precedes the body.
GMAIL_DATE_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[a-z]*\s+\d{1,2}",
    re.IGNORECASE,
)
GMAIL_TO_RE = re.compile(r"^to\s+\S", re.IGNORECASE)
# A bare name line: 1–4 capitalized words, no sentence punctuation.
GMAIL_NAME_RE = re.compile(
    r"^[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,3}$"
)

# Clean Transcript — from the 'Transcript' header to the 'Recording URL' header.
# Both may carry emoji prefixes (📜, 📼). We anchor on 'Transcript' followed by
# a newline (so the word 'transcript' appearing mid-sentence is ignored) and
# stop at the next 'Recording URL' occurrence.
TRANSCRIPT_BLOCK_RE = re.compile(
    r"Transcript\s*\n(.+?)Recording\s+URL",
    re.DOTALL | re.IGNORECASE,
)


def _strip_quote_markers(text: str) -> str:
    """Remove leading '> ' email quote markers and trailing whitespace."""
    lines = [re.sub(r"^\s*>\s?", "", ln).rstrip() for ln in text.splitlines()]
    return "\n".join(lines).strip()


def extract_call_id(raw: str) -> str:
    """UUID labeled as Vapi Call ID wins; otherwise first UUID in the text."""
    if not raw:
        return ""
    m = CALL_ID_LABELED_RE.search(raw)
    if m:
        return m.group(1).lower()
    m = UUID_RE.search(raw)
    return m.group(0).lower() if m else ""


def extract_client_note(raw: str) -> str:
    """
    Extract the client's pure reply text (Index-0 format).

    The Gmail API trigger delivers clean text where the note starts at
    position 0.  The signature block (*Debbie Gomes* / Debbie Gomes)
    and everything after (title, links, forwarded message) is stripped.

    Shape handled:

        This is perfect.  Even though the caller was very confusing...
        *Debbie Gomes*
        Property Manager, Keyrenter Washington, DC
        [...signature links...]
        ---------- Forwarded message ---------
        From: Raymond Vargas <hello@engagemateai.com>
        [...rest of the Vapi transcript...]

    Returns only: "This is perfect.  Even though the caller was very
    confusing..." — her pure note, no signature, no forwarded headers.
    """
    if not raw:
        return ""

    # 1. SAFE-FAIL: require at least one known Keyrenter boundary.
    boundary = KEYRENTER_BOUNDARY_RE.search(raw)
    if not boundary:
        return ""

    # 2. Cut at the signature line (*Debbie Gomes* or Debbie Gomes).
    #    If absent, fall back to the first Keyrenter boundary.
    sig = SIGNATURE_RE.search(raw)
    cut_point = sig.start() if sig else boundary.start()
    note = raw[:cut_point]

    # 3. Strip quote markers and trailing whitespace / dashes
    note = _strip_quote_markers(note)
    note = re.sub(r"\n-{3,}\s*$", "", note).strip()

    return note


def extract_clean_transcript(raw: str) -> str:
    """
    Dialogue lines between the 'Transcript' header and the 'Recording URL'
    header. Strips email quote markers and dashed separators. Keeps only
    lines that contain dialogue or useful context.
    """
    if not raw:
        return ""

    m = TRANSCRIPT_BLOCK_RE.search(raw)
    if not m:
        return ""

    block = _strip_quote_markers(m.group(1))

    # SAFE-FAIL: a real Vapi transcript has AI:/User: turn prefixes.
    # If neither exists, this block isn't actually dialogue (could be
    # metadata, a placeholder, or a broken extraction) — return "" so
    # the record routes to the Skipped escape hatch.
    if "AI:" not in block and "User:" not in block:
        return ""

    # Drop pure separator lines (----), leftover header emoji fragments,
    # and any line that has zero alphanumeric characters (e.g. lone "📼")
    filtered: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            filtered.append("")
            continue
        if re.fullmatch(r"-{3,}", stripped):
            continue
        if not re.search(r"[A-Za-z0-9]", stripped):
            continue
        filtered.append(stripped)

    # Collapse runs of blank lines
    out_lines: list[str] = []
    prev_blank = False
    for ln in filtered:
        if not ln:
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        out_lines.append(ln)

    return "\n".join(out_lines).strip()


# ── Transcript normalization (multi-source) ──────────────────────────────────

# Role mapping: Vapi webhook messages use "assistant"/"bot" for the AI side
# and "user"/"human"/"customer" for the caller.
_ROLE_MAP_AI   = {"assistant", "bot", "ai"}
_ROLE_MAP_USER = {"user", "human", "customer"}


def _try_parse_json(text: str):
    """Try to parse text as JSON. Returns parsed object or None."""
    stripped = text.strip()
    if not stripped or stripped[0] not in ("{", "["):
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None


def _normalize_message_array(messages: list) -> str:
    """Convert a list of {role, content/message} dicts to 'AI: …\\nUser: …' lines."""
    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role_raw = str(msg.get("role", "")).strip().lower()
        content  = str(msg.get("content") or msg.get("message") or "").strip()
        if not content:
            continue
        if role_raw in _ROLE_MAP_AI:
            lines.append(f"AI: {content}")
        elif role_raw in _ROLE_MAP_USER:
            lines.append(f"User: {content}")
        else:
            # Unknown role — preserve as-is with a label
            lines.append(f"{role_raw}: {content}")
    return "\n".join(lines)


def normalize_transcript(raw: str) -> str:
    """
    Detect the transcript format and return uniform 'AI: …\\nUser: …' text.

    Three formats handled:
      1. **JSON array** — stringified list of message objects from a Vapi
         webhook, e.g.  [{"role":"assistant","content":"Hello"},…]
      2. **JSON object** — a single wrapper object with a messages/transcript
         key that holds the array.
      3. **Plain text** — already in 'AI: …\\nUser: …' (Gmail summary) or
         a full email body.  Returned as-is so the email extractors can
         handle it.
    """
    if not raw or not raw.strip():
        return raw

    parsed = _try_parse_json(raw)

    # Case 1: top-level JSON array of message objects
    if isinstance(parsed, list):
        result = _normalize_message_array(parsed)
        if result:
            return result
        # Empty array or no usable messages — fall through to raw
        return raw

    # Case 2: JSON object with a nested messages/transcript key
    if isinstance(parsed, dict):
        for key in ("messages", "transcript", "conversation"):
            nested = parsed.get(key)
            if isinstance(nested, list):
                result = _normalize_message_array(nested)
                if result:
                    return result
        # Object had no recognizable message array — fall through
        return raw

    # Case 3: plain text — return untouched for email extractors
    return raw


# ── Headers ───────────────────────────────────────────────────────────────────

def vapi_headers() -> dict:
    return {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}

def airtable_headers() -> dict:
    return {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

def airtable_table_url() -> str:
    return f"{AIRTABLE_BASE_URL}/{urllib.parse.quote(AIRTABLE_TABLE)}"


# ── Airtable ──────────────────────────────────────────────────────────────────

def fetch_incoming_records(limit: int | None = None) -> list[dict]:
    params = {
        # Exact-match on 'Incoming' — Airtable's `=` is equality, not prefix,
        # so any other status (Skipped, Error, Needs Review, Applied) is ignored.
        "filterByFormula": f"{{{F_STATUS}}}='{STATUS_INCOMING}'",
        "pageSize": 100,
    }
    records: list[dict] = []
    offset = None
    while True:
        if offset:
            params["offset"] = offset
        resp = requests.get(airtable_table_url(), headers=airtable_headers(),
                            params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        if limit and len(records) >= limit:
            records = records[:limit]
            break
        offset = data.get("offset")
        if not offset:
            break
    log.info(f"[airtable] Found {len(records)} Incoming record(s)")
    return records


def fetch_record_by_id(record_id: str) -> dict:
    resp = requests.get(f"{airtable_table_url()}/{record_id}",
                        headers=airtable_headers(), timeout=60)
    resp.raise_for_status()
    return resp.json()


def update_airtable_record(record_id: str, fields: dict,
                           _max_attempts: int = 3, _backoff: int = 5) -> None:
    # typecast=True lets Airtable auto-create missing single-select options
    # (e.g. 'Needs Review', 'Positive Feedback', 'Skipped', 'Error')
    # rather than returning HTTP 422.
    for attempt in range(1, _max_attempts + 1):
        try:
            resp = requests.patch(f"{airtable_table_url()}/{record_id}",
                                  headers=airtable_headers(),
                                  json={"fields": fields, "typecast": True}, timeout=60)
            resp.raise_for_status()
            log.info(f"  [airtable] Updated {record_id} — {list(fields.keys())}")
            return
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt < _max_attempts:
                log.warning(f"  [airtable] Update attempt {attempt} failed ({exc}), "
                            f"retrying in {_backoff}s …")
                time.sleep(_backoff)
            else:
                log.error(f"  [airtable] Update failed after {_max_attempts} attempts: {exc}")
                raise


# ── Vapi ──────────────────────────────────────────────────────────────────────

def fetch_assistant_json(assistant_id: str) -> dict:
    resp = requests.get(f"{VAPI_BASE}/assistant/{assistant_id}",
                        headers=vapi_headers(), timeout=60)
    resp.raise_for_status()
    data = resp.json()
    model_info = data.get("model", {})
    log.info(f"  [vapi] Assistant '{data.get('name', assistant_id)}' — "
          f"provider={model_info.get('provider', '?')}, "
          f"model={model_info.get('model', '?')}")
    return data


def fetch_assistant_name(assistant_id: str) -> str:
    """
    Look up the human-readable `name` for an assistant by ID.

    Returns UNKNOWN_ASSISTANT_NAME on ANY failure — network error,
    non-200 status, JSON parse failure, or a missing/empty `name` field.
    This helper must never raise: it runs inside the triage loop, and a
    cosmetic lookup must not derail a record's diagnosis.
    """
    if not assistant_id:
        return UNKNOWN_ASSISTANT_NAME
    try:
        resp = requests.get(
            f"{VAPI_BASE}/assistant/{assistant_id}",
            headers=vapi_headers(),
            timeout=60,
        )
        resp.raise_for_status()
        name = (resp.json().get("name") or "").strip()
        return name or UNKNOWN_ASSISTANT_NAME
    except Exception as exc:
        log.warning(f"  [vapi] Name lookup failed for {assistant_id}: {exc}")
        return UNKNOWN_ASSISTANT_NAME


# ── Rules directory (modular compiler) ───────────────────────────────────────

def load_vapi_rules() -> str:
    """
    Compile the Qwen rulebook by concatenating every .md file inside
    qwen_rules/ in alphabetical filename order, joined by markdown
    dividers. The 0N_ filename prefix enforces the canonical order
    (persona -> triage logic -> Vapi constraints) so cross-references
    like "see §4.5" still resolve in the compiled string. Fails hard
    if the directory is missing or empty — a missing rulebook means
    the next triage would silently run with no constraints.
    """
    base = os.path.dirname(os.path.abspath(__file__))
    rules_dir = os.path.join(base, RULES_DIR)

    if not os.path.isdir(rules_dir):
        log.error(f"[error] Rules directory not found: {rules_dir}")
        sys.exit(1)

    md_files = sorted(
        f for f in os.listdir(rules_dir)
        if f.lower().endswith(".md")
        and os.path.isfile(os.path.join(rules_dir, f))
    )
    if not md_files:
        log.error(f"[error] No .md files found in {rules_dir}")
        sys.exit(1)

    parts: list[str] = []
    for fname in md_files:
        path = os.path.join(rules_dir, fname)
        with open(path, "r", encoding="utf-8") as f:
            parts.append(f.read().strip())
        log.info(f"[rules] Loaded {RULES_DIR}/{fname}")

    compiled = "\n\n---\n\n".join(parts)
    log.info(f"[rules] Compiled {len(md_files)} module(s) -> {len(compiled)} chars")
    return compiled


# ── Groq / Llama ──────────────────────────────────────────────────────────────

def _slim_assistant(assistant_json: dict, limit_chars: int = 10000) -> str:
    """Keep the fields Qwen needs for diagnosis; drop bulky tool schemas."""
    KEEP_TOP = ("id", "name", "model", "voice", "transcriber",
                "firstMessage", "firstMessageMode")

    slim = {k: v for k, v in assistant_json.items() if k in KEEP_TOP}

    # Slim tool entries: keep name + description, drop full parameter schemas
    model = assistant_json.get("model", {})
    if "tools" in model:
        slim_tools = []
        for t in model["tools"]:
            func = t.get("function", {})
            slim_tools.append({
                "type":  t.get("type"),
                "async": t.get("async"),
                "function": {"name": func.get("name"), "description": func.get("description")},
            })
        slim.setdefault("model", {})["tools"] = slim_tools

    out = json.dumps(slim, indent=2)
    if len(out) > limit_chars:
        out = out[:limit_chars] + "\n... [truncated for context length]"
    return out


def build_prompt(client_note: str, clean_transcript: str,
                 workflow_json: dict, rules_text: str,
                 vapi_telemetry: str = "") -> tuple[str, str]:
    """
    Return (system_prompt, user_prompt). Sent to Groq's /chat/completions
    endpoint as separate `system` and `user` messages — see call_qwen for
    the request shape.

    vapi_telemetry: raw end-of-call-report text from the Airtable
    'Vapi Telemetry' column (written by Make.com). Empty string means
    Make.com didn't deliver a report; we tell the model so explicitly
    rather than silently omitting the section.
    """
    workflow_str = _slim_assistant(workflow_json)
    telemetry_text = (vapi_telemetry or "").strip() or "No server telemetry provided."

    system_prompt = (
        rules_text
        + "\n\n---\n\n"
        + "## YOUR ROLE\n\n"
        + "You are a Senior Voice AI Architect operating under strict "
        + "Human-in-the-Loop mode. The Vapi Rulebook above is your EXCLUSIVE "
        + "reference — do not invent schema, model strings, or SSML tags "
        + "outside of it.\n\n"
        + "You must operate in two distinct mental modes, in order: "
        + "**Detective first, Architect second.** The client's complaint is "
        + "a HYPOTHESIS, not a diagnosis. The transcript is the evidence "
        + "that either confirms or overrules that hypothesis.\n\n"
        + "## MANDATORY THREE-STEP PROCESS\n\n"
        + "### STEP 1 — DETECTIVE: Scan the Transcript for Evidence\n\n"
        + "BEFORE you categorize the symptom, read the Clean Transcript "
        + "line by line and hunt for concrete anomalies. Look for:\n\n"
        + "- **Blank or degenerate AI turns**: lines like `AI: .`, `AI: ,`, "
        + "`AI: ...`, `AI: uh`, or any AI turn that is a single punctuation "
        + "mark or filler token. These almost always indicate a missing "
        + "tool filler message — the model emitted nothing while an async "
        + "tool was pending.\n"
        + "- **Sudden cut-offs**: AI sentences that end mid-word, or user "
        + "turns that interrupt dead silence.\n"
        + "- **Repeated identical AI turns**: the same line twice in a row "
        + "(looping).\n"
        + "- **Prompt leak / internal variable exposure**: the AI speaks "
        + "internal routing variables, confidence scores, persona labels, "
        + "or tool logic aloud (e.g. `AI: Persona, prospect, confidence "
        + "0.9` or `AI: transferring to node handleAddress`). These are "
        + "system prompt instructions leaking into spoken output.\n"
        + "- **The user asking for a human / representative / manager** "
        + "followed by the AI continuing the script (missing human-transfer).\n"
        + "- **Mispronounced words** spelled phonetically by the user "
        + "correcting the AI.\n"
        + "- **Long gaps implied by the user** (\"are you still there?\", "
        + "\"hello?\") WITHOUT an AI blank turn nearby — this is real model latency.\n\n"
        + "You MUST correlate the Client Note against this evidence. "
        + "The note is what the client *thinks* happened; the transcript is "
        + "what *actually* happened.\n\n"
        + "**Worked example of correlation.** Client Note says \"there was "
        + "a big delay\". Transcript shows `AI: .` right before a user "
        + "turn. The root cause is NOT LLM latency — it is a missing tool "
        + "filler on an async tool call. The AI emitted a blank period "
        + "because no filler message was configured. A model swap would "
        + "not fix this.\n\n"
        + "Record your findings in the output field `transcriptEvidence` "
        + "— a short list of the specific lines or patterns you found. "
        + "If you found no anomaly, say so.\n\n"
        + "### STEP 2 — Classify the Symptom (Evidence-Driven)\n\n"
        + "Using the transcript evidence from Step 1 (NOT just the client's "
        + "wording), assign EXACTLY ONE category:\n\n"
        + "- `[Latency/Delay]` — transcript evidence shows genuine slow "
        + "model response: user prompts \"hello?\", long pauses with no "
        + "blank-turn artifact, slow replies across multiple turns.\n"
        + "- `[Pronunciation/TTS]` — transcript evidence shows a specific "
        + "word spoken wrong, or the user correcting pronunciation.\n"
        + "- `[Logic/Looping]` — transcript evidence shows wrong routing, "
        + "repeated turns, missed human-transfer, a tool firing with a "
        + "missing parameter, OR a blank/degenerate AI turn (`AI: .`) "
        + "indicating a missing tool filler message.\n"
        + "- `[Prompt Leak]` — transcript evidence shows the AI speaking "
        + "internal variables, confidence scores, persona labels, routing "
        + "logic, or tool names aloud (e.g. `AI: Persona, prospect, "
        + "confidence 0.9`). Fix: `append_instruction` to `model.messages` "
        + "with value `\"NEVER speak internal routing variables, confidence "
        + "scores, persona labels, or tool logic aloud. Keep all "
        + "transitions strictly conversational.\"`\n"
        + "- `[Positive Feedback]` — the forwarded client email contains "
        + "EXPLICIT praise of the bot ('this was perfect', 'great job', "
        + "'love how she handled that'). Use this category ONLY when "
        + "Debbie's note is unambiguously positive. Output "
        + "`\"jsonPatch\": {}`.\n"
        + "- `[No Fix Needed]` — the call is fine but Debbie did NOT "
        + "explicitly praise it. Use this for ordinary successful calls, "
        + "premature hangups, very short clean calls, transcripts where "
        + "the agent achieved its primary goal, the absence of a client "
        + "note (Vapi-webhook records), OR notes that attribute the "
        + "issue to the caller's behaviour ('the caller was confusing'). "
        + "Output `\"jsonPatch\": {}`.\n\n"
        + "**Override rule.** If the Client Note says \"delay\" but the "
        + "transcript shows a blank/degenerate AI turn, the category is "
        + "`[Logic/Looping]` (missing tool filler), NOT `[Latency/Delay]`. "
        + "Evidence wins over the client's guess.\n\n"
        + "### PRAISE / MIXED-FEEDBACK RULES\n\n"
        + "- **Pure praise** (\"This is perfect\", \"Great job\", \"Love "
        + "it\"): classify as `[Positive Feedback]` and output an empty "
        + "`jsonPatch: {}`. Do NOT invent a fix. This is the ONLY case "
        + "that produces `[Positive Feedback]`.\n"
        + "- **User-error attribution** (\"The caller was confusing\", "
        + "\"They weren't speaking clearly\"): classify as "
        + "`[No Fix Needed]` — the client is explaining away the issue, "
        + "not praising the agent.\n"
        + "- **Mixed feedback** (\"I love this, BUT we need to fix the "
        + "delay\" / \"Great overall, however the name is mispronounced\"): "
        + "IGNORE the praise entirely. Focus on the specific critique and "
        + "classify + patch accordingly. Mixed feedback is NEVER "
        + "`[Positive Feedback]` because the client requested a change.\n\n"
        + "### STEP 3 — ARCHITECT: Build the Patch from the Rulebook\n\n"
        + "Once the root cause is identified from the transcript, copy the "
        + "EXACT JSON architecture from the matching Rulebook fix pattern "
        + "(see the Fix Pattern Library — dead-air filler, mispronunciation, "
        + "model swap, transcriber keywords, take-a-message (no transfer), "
        + "param collection, first message, silent log tool, anti-looping).\n\n"
        + "- Copy the exact field names from the Schema Path Quick Reference.\n"
        + "- Copy the exact tool / message JSON shape from the matching fix pattern.\n"
        + "- Use only provider + model string pairs listed in the Rulebook.\n"
        + "- Do NOT invent fields, keys, provider names, model ids, or SSML tags.\n"
        + "- **MODERN ASSISTANT ARCHITECTURE ONLY.** System-prompt fixes "
        + "target `model.messages[role=system].content` — NEVER bare "
        + "`model.messages`. There are NO nodes, NO edges, NO workflow "
        + "structure. Never use `nodes[0]`, `node/<id>`, `workflow.nodes`, "
        + "or any legacy syntax. Valid `field` values are surgical paths "
        + "from §1 of the Rulebook: `model.messages[role=system].content`, "
        + "`firstMessage`, `model.model`, `model.maxTokens`, "
        + "`transcriber.keywords`, `model.tools[name=<NAME>].messages`, "
        + "etc. BARE BASE ARRAYS ARE FORBIDDEN: never `model.messages`, "
        + "`model.tools`, `model`, `voice`, or `transcriber` alone.\n"
        + "- **PLACEHOLDERS MUST BE RESOLVED.** When copying a JSON template "
        + "from the Rulebook, you MUST replace any placeholder tags (e.g., "
        + "`<SHORT WARM PHRASE>`, `<EXISTING_TOOL_NAME>`, "
        + "`<TERM_1>`) with actual, "
        + "contextually appropriate string values derived from the "
        + "transcript and the live Assistant JSON. Never output literal "
        + "`<...>` placeholder tags in your final `jsonPatch`. A patch "
        + "that still contains `<>` tags is INVALID.\n"
        + "- **SURGICAL STRIKE RULE.** You are FORBIDDEN from using "
        + "`\"operation\": \"replace\"` on "
        + "`model.messages[role=system].content`. "
        + "System prompts are large, complex instruction sets with many "
        + "existing rules — replacing them wholesale DESTROYS existing "
        + "logic you cannot see. Instead, use "
        + "`\"operation\": \"append_instruction\"` with a `value` "
        + "containing ONLY the 1-2 new sentences to append (e.g., "
        + "`\"value\": \"If the caller asks about property details, "
        + "collect their email address before proceeding.\"`). The "
        + "deployment script will MERGE your instruction as a single "
        + "bullet into the `### DYNAMIC DIRECTIVES ###` section of the "
        + "system prompt — creating the section if absent, or appending "
        + "to it if present — and will run a semantic + exact de-"
        + "duplication check against existing bullets so an equivalent "
        + "directive is never written twice. For the merge to produce "
        + "clean output, your `value` MUST be ONE plain sentence: "
        + "NO markdown fences, NO leading bullet markers ('-', '*', "
        + "'•'), NO section headers ('###', '## ', '**…**'), NO "
        + "leading or trailing newlines. The block structure is "
        + "maintained by the deployment script — you supply the "
        + "content only.\n\n"
        + "### HARD PROHIBITIONS\n\n"
        + "- NEVER swap the LLM model when the transcript shows a blank/"
        + "degenerate AI turn — that is a filler problem, not a latency "
        + "problem.\n"
        + "- NEVER swap `voice.provider` for a latency issue. Voice "
        + "provider only matters for pronunciation fixes when SSML support "
        + "is required.\n"
        + "- NEVER apply SSML fixes for latency or logic issues.\n"
        + "- NEVER use legacy workflow syntax (`nodes`, `nodes[0]`, "
        + "`node/<id>`, `edges`, `workflow.nodes`). The architecture is "
        + "Assistants — target `model.messages[role=system].content` via "
        + "`append_instruction`.\n"
        + "- NEVER invent model strings, provider names, or SSML tags not "
        + "listed in the Rulebook.\n"
        + "- NEVER use `\"operation\": \"replace\"` on "
        + "`model.messages[role=system].content`. Use `\"append_instruction\"` "
        + "instead — see Surgical Strike Rule.\n\n"
        + "### ABSOLUTE LAWS\n\n"
        + "Obey the six Top-Level Laws in §6 of the loaded Rulebook "
        + "STRICTLY — (1) Debbie Override, (2) Triage Restraint, "
        + "(3) No Lookups, (4) Unknown Callbacks, (5) Silence / Dead "
        + "Air, (6) Caller's Number Is Already Known (metadata fact "
        + "— NEVER ask for callback, ALWAYS confirm the one on file). "
        + "They override every §4 fix pattern and every §7 telemetry "
        + "rule. Read §6 before you classify the symptom or build the "
        + "patch — failing any one law rejects the patch at human "
        + "review.\n\n"
        + "### ANTI-HALLUCINATION GUARDS\n\n"
        + "These rules exist because you have a documented tendency to "
        + "hallucinate. Follow them exactly.\n\n"
        + "1. **NO PLAGIARISM.** Your `rationale` MUST be 100% uniquely "
        + "synthesized for each call. Reference the specific caller by "
        + "name (if visible in the transcript), the specific issue they "
        + "experienced, and the specific fix you are proposing. NEVER "
        + "copy, paraphrase, or echo any example text from these "
        + "instructions. If your rationale could apply to a different "
        + "call without changing a single word, it is WRONG — rewrite "
        + "it with concrete details from THIS call.\n\n"
        + "2. **NO PREAMBLE / POSTAMBLE.** Your entire output must begin "
        + "with `{` and end with `}`. Do not write 'Here is your JSON', "
        + "'Sure', 'Let me analyze', 'Based on the transcript', or ANY "
        + "text before or after the JSON object. Any character outside "
        + "the JSON braces is a failure.\n\n"
        + "3. **ASSISTANT IDs MUST BE REAL — NEVER HALLUCINATE.** The "
        + "`target_assistant_id` in your `jsonPatch` MUST be copied "
        + "character-for-character from the `\"id\"` field of the LIVE "
        + "ASSISTANT JSON provided in the user input. Do NOT guess, "
        + "invent, or approximate an assistant ID. If the LIVE ASSISTANT "
        + "JSON does not contain an `\"id\"` field, omit "
        + "`target_assistant_id` from your jsonPatch entirely and note "
        + "the gap in your rationale.\n\n"
        + "4. **EVIDENCE MUST BE REAL.** Every entry in your "
        + "`transcriptEvidence` array must be a direct quote or close "
        + "paraphrase of an actual line in the provided transcript. "
        + "Never fabricate evidence. If you cite `AI: .` but the "
        + "transcript does not contain that line, your output is "
        + "fraudulent and will be rejected.\n\n"
        + "## OUTPUT SCHEMA (REQUIRED)\n\n"
        + "Return ONLY valid JSON. No markdown. No prose outside the JSON.\n\n"
        + "{\n"
        + '  "transcriptEvidence": ["<specific anomalous line or pattern>", "..."],\n'
        + '  "symptomCategory": "[Latency/Delay]" | "[Pronunciation/TTS]" | "[Logic/Looping]" | "[Prompt Leak]" | "[Positive Feedback]" | "[No Fix Needed]",\n'
        + '  "jsonPatch": {\n'
        + '    "target_assistant_id": "<assistant ID from the LIVE ASSISTANT JSON>",\n'
        + '    "field":              "model.messages[role=system].content" | "<surgical bracket path from §1 - bare arrays forbidden>",\n'
        + '    "operation":          "replace" | "add" | "remove" | "append_instruction",\n'
        + '    "value":              <valid value copied from the Rulebook fix pattern>\n'
        + "  },\n"
        + '  "rationale": "<MAXIMUM 2 SENTENCES, PLAIN ENGLISH — see Rationale Rules>"\n'
        + "}\n\n"
        + "## RATIONALE RULES (STRICTLY ENFORCED)\n\n"
        + "- MAXIMUM 2 sentences.\n"
        + "- **The rationale will be emailed directly to the non-technical "
        + "client.** Keep it conversational, clear, and free of heavy "
        + "developer jargon, while remaining strictly under 2 sentences.\n"
        + "- **FORBIDDEN: section symbols or rulebook citations.** Never "
        + "use `§`, never write phrases like \"per §9.3\", \"Rulebook "
        + "section 9\", \"per section 10.1\", or any cross-reference to "
        + "the Rulebook. The client does not have the Rulebook.\n"
        + "- **FORBIDDEN: developer jargon.** Do not use terms like "
        + "\"jsonPatch\", \"async\", \"node\", \"nodes\", \"edge\", "
        + "\"workflow\", \"schema\", \"payload\", or \"endpoint\" in the "
        + "rationale. Describe the "
        + "fix in terms the client understands: \"the AI\", \"the phone "
        + "system\", \"a short phrase while looking things up\", etc.\n"
        + "- Sentence 1: describe what happened in THIS SPECIFIC call "
        + "using the caller's name and the concrete issue.\n"
        + "- Sentence 2: describe the specific change being made and "
        + "why it fixes the problem.\n"
        + "- **DO NOT copy or paraphrase any example from these "
        + "instructions.** Your rationale must be unique to this call. "
        + "Use the caller's actual name, the actual issue observed, and "
        + "the actual fix proposed. A generic rationale is a FAILED "
        + "rationale.\n"
        + "- For `[No Fix Needed]`: warmly acknowledge the feedback "
        + "using the caller's name if available.\n"
    )

    user_prompt = (
        "=== PRIMARY OBJECTIVE (from client email) ===\n"
        f"{client_note or '(client note not extractable — rely on transcript)'}\n\n"
        "=== CALL TRANSCRIPT (to analyze) ===\n"
        f"{clean_transcript}\n\n"
        "### VAPI SERVER TELEMETRY ###\n"
        f"{telemetry_text}\n\n"
        "=== LIVE ASSISTANT JSON ===\n"
        f"{workflow_str}\n\n"
        "Produce the jsonPatch + rationale now."
    )

    return system_prompt, user_prompt


def _strip_markdown_fences(text: str) -> str:
    """Strip ```json / ``` fences that the model sometimes wraps around output."""
    text = text.strip()
    text = re.sub(r"^```(?:json|JSON)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


# Greedy first-`{` to last-`}` extractor (Strategy 4). DOTALL makes `.`
# match newlines so multi-line JSON is captured as a single match.
_JSON_PAYLOAD_RE = re.compile(r"\{.*\}", re.DOTALL)

# Find ALL fenced code blocks (Strategy 3). Llama occasionally emits the
# diagnostic JSON in one block followed by example patches in additional
# blocks — we try each in order so the first valid object wins.
_FENCED_BLOCK_RE = re.compile(
    r"```(?:json|JSON)?\s*\n?(.*?)\n?```",
    re.DOTALL,
)


def _try_parse_object(candidate: str) -> dict | None:
    """One json.loads attempt. Returns the dict on success, None on any
    failure (decode error, non-dict top level, empty input)."""
    if not candidate or not candidate.strip():
        return None
    try:
        result = json.loads(candidate)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return result if isinstance(result, dict) else None


def _extract_balanced_object(text: str) -> str | None:
    """
    Walk `text` character-by-character to find the FIRST balanced
    `{...}` block, ignoring braces that appear inside string literals.
    Handles nested objects correctly — unlike the greedy regex which
    grabs from the first `{` to the LAST `}` and may capture trailing
    garbage between two separate JSON objects.
    """
    depth     = 0
    start     = -1
    in_string = False
    escape    = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                return text[start:i + 1]
            if depth < 0:
                return None
    return None


def _xray_dump(raw_text: str) -> None:
    """
    Emit the FULL Groq response as a single ERROR log entry when every
    parse strategy fails. Bracketed by `>>> / <<<` markers so it stays
    greppable in the rotating log file. We deliberately avoid truncation
    here — a truncated dump is the whole reason this function exists.
    """
    bar = "=" * 72
    lines = [
        bar,
        "  [parse] ALL JSON EXTRACTION STRATEGIES FAILED — X-RAY DUMP",
        bar,
        f"  [parse] raw length: {len(raw_text)} chars",
        f"  [parse] raw repr (first 200): {raw_text[:200]!r}",
        f"  [parse] raw repr (last 200):  {raw_text[-200:]!r}",
        "  [parse] FULL RAW TEXT (between >>> and <<< markers):",
        ">" * 72,
        raw_text,
        "<" * 72,
    ]
    if not _JSON_REPAIR_AVAILABLE:
        lines.append(
            "  [parse] (json_repair not installed — "
            "`pip install json-repair` would enable a repair fallback)"
        )
    lines.append(bar)
    log.error("\n".join(lines))


def parse_qwen_response(raw_text: str) -> dict | None:
    """
    Multi-strategy JSON extraction from arbitrary Llama-on-Groq output.
    Returns the parsed dict, or None if every strategy fails. On total
    failure, dumps the FULL raw response to stdout for X-Ray inspection.

    Strategies, tried in order:
      0. Empty / whitespace-only response   → log + return None
      1. Fast path: json.loads(cleaned)
      2. Each ```json``` fenced block (in order)
      3. Greedy regex (first `{` to last `}`)
      4. Balanced-brace walker (first complete `{...}`)
      5. json_repair (if installed)         → last resort
    """
    # Strategy 0: empty response. Groq returns 200 OK with empty
    # `content` when the model refuses, hits a safety filter, or has a
    # tokenizer hiccup. Surface this loudly so the operator can see it
    # rather than burying it as a generic "parse failure".
    if not raw_text or not raw_text.strip():
        log.error("  [parse] Groq returned EMPTY content — model produced no output.")
        log.error(f"  [parse] raw repr: {raw_text!r}")
        return None

    cleaned = _strip_markdown_fences(raw_text)

    # Strategy 1: fast path — entire response is already valid JSON.
    parsed = _try_parse_object(cleaned)
    if parsed is not None:
        return parsed

    # Strategy 2: every fenced ```json``` block in the RAW text (not the
    # already-stripped `cleaned`, which only loses the outermost fence).
    for m in _FENCED_BLOCK_RE.finditer(raw_text):
        parsed = _try_parse_object(m.group(1).strip())
        if parsed is not None:
            return parsed

    # Strategy 3: greedy regex — first `{` to last `}` (DOTALL).
    m = _JSON_PAYLOAD_RE.search(cleaned)
    if m:
        parsed = _try_parse_object(m.group(0))
        if parsed is not None:
            return parsed

    # Strategy 4: balanced-brace walker — handles trailing garbage that
    # would confuse Strategy 3 (e.g., two JSON objects emitted back-to-
    # back with text between them).
    candidate = _extract_balanced_object(cleaned)
    if candidate:
        parsed = _try_parse_object(candidate)
        if parsed is not None:
            return parsed

    # Strategy 5: json_repair — last resort. Repairs missing quotes,
    # trailing commas, unescaped strings, etc. Optional dep.
    if _JSON_REPAIR_AVAILABLE:
        try:
            repaired = repair_json(cleaned, return_objects=True)
        except Exception as exc:
            log.warning(f"  [parse] json_repair raised: {exc}")
        else:
            if isinstance(repaired, dict) and repaired:
                log.warning("  [parse] json_repair successfully recovered the payload")
                return repaired

    # All strategies failed — dump the FULL raw response.
    _xray_dump(raw_text)
    return None


def call_qwen(client_note: str, clean_transcript: str,
              workflow_json: dict, rules_text: str,
              vapi_telemetry: str = "") -> dict | None:
    system_prompt, user_prompt = build_prompt(
        client_note, clean_transcript, workflow_json, rules_text,
        vapi_telemetry=vapi_telemetry,
    )

    log.info(f"  [prompt] system={len(system_prompt)} chars | "
          f"user={len(user_prompt)} chars | "
          f"total={len(system_prompt) + len(user_prompt)} chars")

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature": 0.15,
        "max_tokens":  1500,    # cap response — rationale + patch fits comfortably
        "top_p":       0.9,
        "stream":      False,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }

    # Retry envelope — up to 3 attempts (1 initial + 2 retries) with a
    # 3s backoff between. Retries cover transient transport errors
    # (connection drop, read timeout, Groq 5xx / 429) AND extreme
    # parsing failures (model emits prose instead of JSON). Auth or
    # other 4xx codes bail immediately — retrying those just burns
    # the rate-limit budget without any chance of success.
    MAX_ATTEMPTS    = 3
    BACKOFF_SECONDS = 3
    RETRYABLE_HTTP  = {429, 500, 502, 503, 504}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        suffix = "" if attempt == 1 else f" (retry {attempt - 1}/{MAX_ATTEMPTS - 1})"
        log.info(f"  [groq] Sending to {GROQ_MODEL}{suffix} …")
        t0 = time.time()

        try:
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=120)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as exc:
            log.warning(f"  [groq] FAILED — {type(exc).__name__}: {exc}")
        else:
            status = resp.status_code
            body_preview = resp.text[:300] if status >= 400 else ""
            if status >= 400 and status not in RETRYABLE_HTTP:
                # Non-retryable (auth, malformed request, etc.) — abort.
                log.error(f"  [groq] FAILED — HTTP {status} (non-retryable) | body: {body_preview}")
                return None
            if status == 429:
                # Distinct, grep-friendly marker so a TPM/RPM cap hit is
                # obvious in autopilot.log. Surface Groq's rate-limit headers
                # (retry-after + remaining tokens) to confirm whether we're
                # bumping the per-minute ceiling on the active model.
                retry_after = resp.headers.get("retry-after", "?")
                tok_remain  = resp.headers.get("x-ratelimit-remaining-tokens", "?")
                req_remain  = resp.headers.get("x-ratelimit-remaining-requests", "?")
                log.warning(
                    f"  [groq] RATE-LIMIT 429 on {GROQ_MODEL} — "
                    f"retry-after={retry_after}s remaining-tokens={tok_remain} "
                    f"remaining-requests={req_remain} | body: {body_preview}")
            elif status >= 400:
                log.warning(f"  [groq] FAILED — HTTP {status} (retryable) | body: {body_preview}")
            else:
                elapsed = time.time() - t0
                try:
                    body = resp.json()
                    raw_text = body["choices"][0]["message"]["content"]
                except (KeyError, IndexError, ValueError) as exc:
                    log.warning(f"  [groq] FAILED — unexpected Groq response shape: {exc}")
                else:
                    usage = body.get("usage") or {}
                    tok = (f" | tokens: {usage.get('prompt_tokens','?')} in + "
                           f"{usage.get('completion_tokens','?')} out = "
                           f"{usage.get('total_tokens','?')} (cap 30k TPM)")
                    log.info(f"  [groq] done ({elapsed:.1f}s, {len(raw_text)} chars){tok}")
                    parsed = parse_qwen_response(raw_text)
                    if parsed is not None:
                        return parsed
                    # Parse failure falls through to the retry block.

        if attempt < MAX_ATTEMPTS:
            log.warning(f"  [groq] backing off {BACKOFF_SECONDS}s before retry …")
            time.sleep(BACKOFF_SECONDS)

    log.error(f"  [groq] all {MAX_ATTEMPTS} attempts exhausted — giving up on this record")
    return None


# ── Assistant ID extraction from jsonPatch ─────────────────────────────────────

def extract_assistant_id_from_patch(json_patch: dict | None) -> str:
    """Return target_assistant_id from the patch, if present."""
    if not isinstance(json_patch, dict):
        return ""
    return str(json_patch.get("target_assistant_id", "")).strip()


# ── Law 5 guard (defense-in-depth) ───────────────────────────────────────────
# Even with §6 Law 5 spelled out in the rulebook, the model has been
# observed emitting `append_instruction` patches that try to coach the
# system prompt about silence / dead air ("be patient", "wait longer",
# "do not repeat the question"). Silence is a Vapi config issue
# (`voice.silenceTimeout`), NOT a prompt fix — see qwen_rules/02_triage_logic.md
# Law 5. This guard catches the violation client-side and downgrades the
# patch to an empty one so the bad fix never reaches Airtable as a
# Needs Review record.
_LAW_5_BANNED_VALUE_SUBSTRINGS: tuple[str, ...] = (
    "silence",
    "still there",
    "are you still",
    "be patient",
    "wait longer",
    "no response",
    "do not repeat",
    "end the call gracefully",
)


def enforce_law_5(json_patch: dict, rationale: str) -> tuple[dict, str]:
    """
    If the patch is an `append_instruction` to the system prompt whose
    `value` contains silence-coaching language, reject the patch and
    annotate the rationale with the operator-facing flag from Law 5.
    Returns the (possibly downgraded) patch and rationale.
    """
    if not isinstance(json_patch, dict) or not json_patch:
        return json_patch, rationale
    if json_patch.get("operation") != "append_instruction":
        return json_patch, rationale
    field = str(json_patch.get("field", ""))
    if "messages[role=system]" not in field:
        return json_patch, rationale
    value = str(json_patch.get("value", "")).lower()
    if not any(w in value for w in _LAW_5_BANNED_VALUE_SUBSTRINGS):
        return json_patch, rationale

    log.warning(
        "  [law-5-guard] Rejecting silence-related prompt patch — "
        f"value preview: {value[:140]!r}"
    )
    annotated = (
        (rationale.rstrip() + " " if rationale else "")
        + "[Law 5 enforced: Vapi silence/timeout configuration needs adjustment.]"
    )
    return {}, annotated


# ── Triage one record ────────────────────────────────────────────────────────

def triage_record(record: dict, workflow_json: dict, rules_text: str) -> None:
    record_id = record["id"]
    fields    = record.get("fields", {})
    raw_transcript = fields.get(F_TRANSCRIPT, "") or ""

    log.info(f"\n{'='*64}")
    log.info(f"  Record: {record_id}")
    log.info(f"{'='*64}")

    # ── Pre-triage state-machine filter ─────────────────────────────────
    # Three rules gate Qwen (see "Pre-triage state machine" block above):
    #   Rule 1 — infra error + no human flag   → SKIP
    #   Rule 2 — clean call + no human flag    → SKIP
    #   Rule 3 — human flag OR eval=failure    → fall through to Qwen
    # Read Email ID + Vapi Telemetry once here; both are reused below
    # when we build the Qwen prompt (Rule 3 path), so no duplicate reads.
    email_id       = str(fields.get(F_EMAIL_ID) or "").strip()
    vapi_telemetry = str(fields.get(F_VAPI_TELEMETRY) or "").strip()
    ended_reason, success_eval = parse_telemetry(vapi_telemetry)

    has_human_flag  = bool(email_id)
    has_infra_error = is_infrastructure_error(ended_reason)
    eval_is_failure = success_evaluation_is_failure(success_eval)

    log.info(f"  [filter] Email ID           : {email_id or '(empty)'}")
    log.info(f"  [filter] endedReason        : {ended_reason or '(not in telemetry)'}")
    log.info(f"  [filter] Success Evaluation : {success_eval or '(not in telemetry)'}")
    log.info(f"  [filter] flags              : "
          f"human={has_human_flag}  infra={has_infra_error}  eval_failure={eval_is_failure}")

    # Rule 1 — infrastructure error with no human flag. Qwen cannot fix
    # Vapi runtime / provider outages; sending the record would only
    # burn GPU time and risk the model hallucinating a prompt patch
    # for an infra problem (Prompt Drift).
    if has_infra_error and not has_human_flag:
        log.info(f"  [filter] -> RULE 1  (infra error, no human flag) -- SKIP Qwen")
        update_airtable_record(record_id, {
            F_STATUS:    STATUS_SKIPPED,
            F_RATIONALE: "Infrastructure error - no prompt patch needed",
        })
        return

    # Rule 2 — clean call, no human flag. Nothing to fix, nothing to
    # diagnose; mark Skipped so the record exits the Incoming queue
    # and won't be re-processed on the next run.
    if not has_human_flag and not eval_is_failure:
        log.info(f"  [filter] -> RULE 2  (no human flag, no eval failure) -- SKIP Qwen")
        update_airtable_record(record_id, {
            F_STATUS:    STATUS_SKIPPED,
            F_RATIONALE: "Clean call, awaiting potential human feedback",
        })
        return

    # Rule 4 — dead call with no human flag: caller hung up immediately
    # (transcript < 300 chars, ended=customer-ended-call, no email). There
    # is nothing to triage — the agent never got to speak. Skip before Groq.
    raw_transcript_for_rule4 = str(fields.get(F_TRANSCRIPT) or "").strip()
    if (not has_human_flag
            and "customer-ended-call" in ended_reason
            and len(raw_transcript_for_rule4) < 300):
        log.info(f"  [filter] -> RULE 4  (dead call, <300 chars, no human flag) -- No Fix Needed")
        update_airtable_record(record_id, {
            F_STATUS:    STATUS_NO_FIX_NEEDED,
            F_RATIONALE: "Caller disconnected immediately — no dialogue to triage.",
        })
        return

    # Rule 3 — fall through to the full Qwen pipeline. Status vocabulary
    # (Needs Review / Positive Feedback / Error / Skipped) is chosen by
    # the existing handler below based on Qwen's output; 'Applied' is
    # still written exclusively by apply_approved_fix.py post-deploy.
    log.info(f"  [filter] -> RULE 3  (human flag or eval failure) -- triage with Qwen")

    # 0. Normalize — if the Transcript field came from a Vapi webhook
    #    (JSON array or raw dialogue), convert it to plain AI:/User: text
    #    so the downstream email extractors and Qwen see a uniform format.
    normalized = normalize_transcript(raw_transcript)
    if normalized != raw_transcript:
        log.info(f"  [normalize] Vapi webhook detected — converted to plain dialogue ({len(normalized)} chars)")

    # 1. Determine the source: if the Airtable record already has a Call ID,
    #    this is a Vapi webhook record — bypass the email regex entirely.
    #    If Call ID is empty, it's a forwarded Gmail email — use the email
    #    extractors as before.
    airtable_call_id = (fields.get(F_CALL_ID) or "").strip()
    is_webhook       = bool(airtable_call_id)

    if is_webhook:
        # Vapi webhook path — Call ID is already in Airtable, transcript
        # is pure dialogue (normalized above). No email parsing needed.
        call_id          = airtable_call_id
        client_note      = ""
        clean_transcript = normalized
        log.info(f"  [source] Vapi webhook (Call ID present) — skipping email extractors")
    else:
        # Gmail forwarded email path — run the full email extraction pipeline.
        call_id          = extract_call_id(raw_transcript)
        client_note      = extract_client_note(raw_transcript)
        clean_transcript = extract_clean_transcript(raw_transcript)

    # NOTE: `vapi_telemetry` was already read at the top of the function
    # by the pre-triage state-machine filter; we reuse that value below
    # when we build the Qwen prompt.

    log.info(f"  [extract] Call ID       : {call_id or '(none)'}")
    log.info(f"  [extract] Client note   : {len(client_note)} chars")
    log.info(f"  [extract] Clean transcript : {len(clean_transcript)} chars")

    # 2. Escape hatch — no dialogue means no diagnosis possible
    if not clean_transcript:
        log.info("  [escape-hatch] Clean_Transcript is empty — skipping Qwen")
        result = ESCAPE_HATCH
    else:
        # 3. Full diagnostic path
        qwen_out = call_qwen(client_note, clean_transcript,
                             workflow_json, rules_text,
                             vapi_telemetry=vapi_telemetry)
        if qwen_out is None:
            update_airtable_record(record_id, {
                F_STATUS:       STATUS_ERROR,
                F_PROPOSED_FIX: "Groq call failed or response was not parseable JSON.",
                F_RATIONALE:    "Automated triage failed. Re-queue after checking Groq.",
            })
            return
        # Sanitize at the boundary — strip fancy punctuation, decode
        # stray `\uXXXX` text escapes, and normalize to NFC BEFORE the
        # value lands in Airtable. The reviewer sees clean text and the
        # downstream deployment script doesn't have to guess what's real.
        result = sanitize_value(qwen_out)

    # 4. Extract the required keys
    json_patch   = result.get("jsonPatch", {})
    rationale    = result.get("rationale", "")
    symptom      = (result.get("symptomCategory", "") or "").strip()

    # 4a. Defense-in-depth Law 5 guard. Silence is config, not prose —
    # if the model emitted a silence-coaching prompt patch, drop it
    # client-side and annotate the rationale with the operator flag.
    json_patch, rationale = enforce_law_5(json_patch, rationale)

    assistant_id = extract_assistant_id_from_patch(json_patch)

    # 5. Build Airtable update payload — route to the correct status:
    #    No dialogue          → STATUS_SKIPPED          (junk / escape hatch)
    #    Non-empty patch      → STATUS_NEEDS_REVIEW     (fix awaiting human)
    #    Empty patch + praise → STATUS_POSITIVE_FEEDBACK (Debbie explicitly praised the bot)
    #    Empty patch + other  → STATUS_NO_FIX_NEEDED    (ordinary call — no actionable anomaly)
    # Status falls back to STATUS_NO_FIX_NEEDED if the model emits an
    # empty patch but a non-praise category, so a "Positive Feedback"
    # tag never gets attached to a call the client didn't actually praise.
    if not clean_transcript:
        status = STATUS_SKIPPED
    elif json_patch:
        status = STATUS_NEEDS_REVIEW
    elif symptom == "[Positive Feedback]":
        status = STATUS_POSITIVE_FEEDBACK
    else:
        status = STATUS_NO_FIX_NEEDED

    update_fields: dict = {
        F_STATUS:       status,
        F_PROPOSED_FIX: json.dumps(json_patch, indent=2) if json_patch else "{}",
        F_RATIONALE:    rationale,
    }
    if call_id:
        update_fields[F_CALL_ID] = call_id
    if assistant_id:
        # Keep the raw UUID on F_ASSISTANT_ID (primary key for downstream
        # scripts like apply_approved_fix.py). F_ASSISTANT_NAME is
        # cosmetic only — the value comes from a live Vapi lookup and
        # falls back to UNKNOWN_ASSISTANT_NAME if the API call fails.
        assistant_name = fetch_assistant_name(assistant_id)
        log.info(f"  [vapi] Assistant name resolved: '{assistant_name}'")
        update_fields[F_ASSISTANT_ID]   = assistant_id
        update_fields[F_ASSISTANT_NAME] = assistant_name

    # Overwrite the raw-email Transcript field so the Airtable UI shows
    # either the cleaned dialogue or a clear junk-email marker — never
    # the original forwarded email headers.
    if clean_transcript:
        update_fields[F_TRANSCRIPT] = clean_transcript
    else:
        update_fields[F_TRANSCRIPT] = (
            "No conversational dialogue found. Junk email bypassed."
        )

    update_airtable_record(record_id, update_fields)

    # 6. Summary line — `symptom` was extracted above for status routing.
    patch_asst   = json_patch.get("target_assistant_id", "—") if json_patch else "—"
    patch_field  = json_patch.get("field", "—")  if json_patch else "—"
    patch_op     = json_patch.get("operation", "—") if json_patch else "—"
    log.info(f"  [result] status={update_fields[F_STATUS]}")
    log.info(f"  [result] symptom={symptom or '—'}")
    log.info(f"  [result] assistant={patch_asst} | field={patch_field} | op={patch_op}")
    log.info(f"  [result] rationale: {rationale}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Engage Mate AI — Advanced Auto Triage")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N Incoming records")
    parser.add_argument("--record-id", type=str, default=None,
                        help="Target a single record by ID (bypasses status filter)")
    args = parser.parse_args()

    # Validate env
    missing = [v for v in ("VAPI_API_KEY", "AIRTABLE_API_KEY",
                            "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME",
                            "VAPI_ASSISTANT_ID", "GROQ_API_KEY")
               if not os.getenv(v)]
    if missing:
        log.error(f"[error] Missing .env vars: {', '.join(missing)}")
        sys.exit(1)

    log.info("=" * 64)
    log.info("  Engage Mate AI — Advanced Diagnostic Triage")
    log.info(f"  Table    : {AIRTABLE_TABLE}")
    log.info(f"  Model    : {GROQ_MODEL} (Groq)")
    log.info(f"  Mode     : Human-in-the-Loop (no auto-patch)")
    if args.record_id:
        log.info(f"  Target   : {args.record_id}")
    else:
        log.info(f"  Limit    : {args.limit or 'ALL'}")
    log.info(f"  Cooldown : {INTER_RECORD_SLEEP}s between records")
    log.info("=" * 64)

    # Load the Vapi Rulebook once
    log.info("\n[step 0] Loading rule modules …")
    rules_text = load_vapi_rules()

    # Fetch assistant JSON once — retry up to 3x on timeout/connection errors
    log.info("\n[step 1] Fetching live Vapi assistant JSON …")
    _VAPI_FETCH_ATTEMPTS = 3
    _VAPI_FETCH_BACKOFF  = 15
    workflow_json = None
    for _attempt in range(1, _VAPI_FETCH_ATTEMPTS + 1):
        try:
            workflow_json = fetch_assistant_json(VAPI_ASSISTANT_ID)
            break
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as exc:
            if _attempt < _VAPI_FETCH_ATTEMPTS:
                log.warning(f"[step 1] Vapi fetch attempt {_attempt} failed "
                            f"({type(exc).__name__}), retrying in {_VAPI_FETCH_BACKOFF}s …")
                time.sleep(_VAPI_FETCH_BACKOFF)
            else:
                log.error(f"[error] Vapi assistant fetch failed after "
                          f"{_VAPI_FETCH_ATTEMPTS} attempts: {type(exc).__name__}: {exc}")
                sys.exit(1)
        except Exception as exc:
            log.error(f"[error] Vapi assistant fetch failed: {type(exc).__name__}: {exc}")
            sys.exit(1)

    # Fetch records
    log.info("\n[step 2] Querying Airtable …")
    if args.record_id:
        records = [fetch_record_by_id(args.record_id)]
        log.info(f"[airtable] Loaded target record {args.record_id}")
    else:
        records = fetch_incoming_records(limit=args.limit)

    if not records:
        log.info("[done] No records to triage.")
        return

    # Triage loop with cooldown
    total = len(records)
    success = errors = 0
    log.info(f"\n[step 3] Running diagnostics on {total} record(s) …")

    for i, record in enumerate(records, 1):
        log.info(f"\n[{i}/{total}]")
        try:
            triage_record(record, workflow_json, rules_text)
            success += 1
        except Exception as exc:
            log.exception(f"  [error] Unhandled exception: {exc}")
            errors += 1
            try:
                update_airtable_record(record["id"], {
                    F_STATUS:       STATUS_ERROR,
                    F_PROPOSED_FIX: f"Script error: {str(exc)[:460]}",
                    F_RATIONALE:    "Automated triage raised an unexpected exception.",
                })
            except Exception:
                pass

        # 60s cooldown between records — keeps us under Groq's 30k TPM free-tier limit
        if i < total:
            log.info(f"\n  [cooldown] Waiting {INTER_RECORD_SLEEP}s before next record …")
            time.sleep(INTER_RECORD_SLEEP)

    log.info("\n" + "=" * 64)
    log.info(f"  Run complete — {success}/{total} processed, {errors} errored")
    log.info(f"  Airtable status written: "
          f"'{STATUS_NEEDS_REVIEW}' (triaged), "
          f"'{STATUS_POSITIVE_FEEDBACK}' (explicit Debbie praise), "
          f"'{STATUS_NO_FIX_NEEDED}' (ordinary call, no fix needed), "
          f"'{STATUS_SKIPPED}' (junk / no dialogue), "
          f"'{STATUS_ERROR}' (Groq / script failure).")
    log.info("  Review Proposed Fix + Rationale before any patch is applied.")
    log.info("=" * 64)

if __name__ == "__main__":
    main()