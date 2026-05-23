print(">>> APPLY SCRIPT HAS STARTED <<<", flush=True)

"""
apply_approved_fix.py — Engage Mate AI  (Deployment Script — Assistants API)
=============================================================================
Pipeline position: runs AFTER auto_triage.py and human review.

  1. Fetch Airtable records where Status == 'Approved'.
  2. Parse the Proposed Fix (jsonPatch) — extract target_assistant_id.
  3. GET the FULL live assistant JSON from https://api.vapi.ai/assistant/{id}.
  4. Apply the patch in-memory against that full dict via a generic
     Read-Modify-Write cycle (see apply_operation_in_place). Paths in
     `field` may include [key=value] array filters — e.g.
     "model.messages[role=system].content" or "tools[name=foo]" — so
     every leaf is reachable without per-field special cases.
       - "append_instruction"  → merge text as ONE bullet into the
                                 ### DYNAMIC DIRECTIVES ### section of
                                 the string leaf at `field` (typically
                                 the system message content). Performs
                                 semantic + exact dedup against existing
                                 bullets; on a duplicate the in-memory
                                 assistant is untouched and the Vapi
                                 PATCH is skipped. The block is CREATED
                                 at the end of the prompt if absent.
                                 Legacy fallback: if `field` resolves
                                 to the messages ARRAY, dig for the
                                 system message and merge into its
                                 content — keeps old patches that
                                 target bare `model.messages` working.
       - "replace" / "add" /
         "remove"              → traverse to the leaf and mutate.
  5. Rollback safety: capture the CURRENT value at the target `field`
     path (using the same path engine that performs the mutation),
     serialise it, and save to Airtable ('Rollback State' column)
     BEFORE writing to Vapi. Works for any op / any field — a scalar,
     a list, or a dict all round-trip as readable text. Missing fields
     are recorded as '[Field did not exist]' so the run never crashes
     on an optional-field target.
  6. PATCH https://api.vapi.ai/assistant/{id} with the ENTIRE modified
     top-level object (e.g. if field='model.messages' the payload is
     {"model": <full modified model dict>}). This preserves every sibling
     key at that top level (provider, tools, temperature, voice, etc.) —
     a nested delta like {"model": {"messages": ...}} would REPLACE the
     whole model object and strip siblings, yielding Vapi 400s.
  7. On success (HTTP 200) → set Airtable Status to 'Applied'.
     On failure → set Status to 'Error' and log the API response.

Usage:
    python apply_approved_fix.py                        # process ALL Approved records
    python apply_approved_fix.py --limit 1              # one record only (test mode)
    python apply_approved_fix.py --record-id recXXXXXX  # target a single record
    python apply_approved_fix.py --dry-run               # preview changes, don't write to Vapi
"""

import argparse
import io
import json
import os
import re
import sys
import unicodedata
import urllib.parse

import requests
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

VAPI_API_KEY       = os.getenv("VAPI_API_KEY")
AIRTABLE_API_KEY   = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID   = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE     = os.getenv("AIRTABLE_TABLE_NAME")

VAPI_BASE      = "https://api.vapi.ai"
AIRTABLE_BASE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"

# Airtable field names (must match auto_triage.py)
F_STATUS       = "Status"
F_CALL_ID      = "Call ID"
F_PROPOSED_FIX = "Proposed Fix"
F_RATIONALE    = "Rationale"
F_ASSISTANT_ID   = "Assistant ID"
F_ROLLBACK_STATE = "Rollback State"

# Rollback snapshot config — before every mutation we capture the current
# value at `field` and write it to F_ROLLBACK_STATE. Used to manually
# revert a bad patch.
ROLLBACK_SENTINEL  = "[Field did not exist]"
ROLLBACK_MAX_CHARS = 100000  # Airtable long-text safe limit

# Status vocabulary — this script reads 'Approved' and writes
# 'Applied' or 'Error'. All other statuses are owned by auto_triage.py.
STATUS_APPROVED = "Approved"
STATUS_APPLIED  = "Applied"
STATUS_ERROR    = "Error"

# ── Unicode sanitization ──────────────────────────────────────────────────────
# Values flowing from Qwen → Airtable → Vapi occasionally carry literal
# `\uXXXX` escape sequences (Qwen's JSON output smuggled as text),
# fancy punctuation (em-dashes, smart quotes), or zero-width / ASCII
# control chars. The Vapi dashboard and many TTS voices render or parse
# those poorly. Every string we write back to Vapi passes through
# sanitize_text (scalars) or sanitize_value (dict/list trees).

# Fancy Unicode punctuation → ASCII. Conservative: only chars that
# cause rendering / TTS issues are mapped. Legitimate other Unicode
# (accents, non-Latin scripts) is preserved as valid UTF-8.
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

# Zero-width + BOM chars (invisible but confuse tokenizers & grep).
# Range covers U+200B (ZWSP) through U+200D (ZWJ), plus U+2060 (word
# joiner) and U+FEFF (BOM). Hex-escape form keeps the source file ASCII.
_ZERO_WIDTH_RE = re.compile(r"[\u200B-\u200D\u2060\uFEFF]")
# ASCII control chars except newline (\n), carriage return (\r), tab (\t).
_CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
# Literal 6-char "\uXXXX" that slipped through as text rather than being
# decoded to the real codepoint (happens when Qwen double-escapes output).
_LITERAL_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def sanitize_text(text):
    """
    Return a clean UTF-8 version of `text` suitable for writing to Vapi.
    Non-strings pass through untouched so this is safe to call recursively.

      1. Decode literal `\\uXXXX` → real char (fixes Qwen output smuggled
         as text, e.g. "Benz\\u2014WA" in a keyword list).
      2. Unicode NFC normalization.
      3. Translate fancy punctuation to ASCII (em/en dashes, smart
         quotes, ellipsis, NBSP). Legitimate other Unicode preserved.
      4. Strip zero-width / BOM / ASCII control chars (keeps \\n \\r \\t).
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
    Recursively walk a JSON-like value, applying sanitize_text at every
    string leaf. Used on the patch `value` before it mutates the
    assistant dict — catches fancy chars inside nested payloads such as
    `{"keywords": ["Benz—WA", "Smith\\u2019s"]}` without requiring the
    caller to know the shape of the patch.
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


# ── Dynamic Directives block ──────────────────────────────────────────────────
# Production system prompts are large and fragile — blindly appending a
# "### [Auto-Update] ###" block after every client email causes the prompt
# to balloon with duplicated / contradictory instructions. Instead, we
# carve out ONE named section, `### DYNAMIC DIRECTIVES ###`, and every
# triage-driven instruction is merged into it as a single bullet. Every
# new bullet is checked (exact + semantic) against the existing bullets
# before insertion.
#
# Shape maintained by merge_directive():
#
#     ### DYNAMIC DIRECTIVES ###
#
#     <!-- Auto-maintained … Client Supremacy note. -->
#
#     - First directive (from client email A).
#     - Second directive (from client email B).
#
#     ### END DYNAMIC DIRECTIVES ###
#
# CLIENT SUPREMACY: the note comment above the bullets makes it explicit
# that each bullet originates from a client note. The auto_triage.py
# prompt still treats the Client Note as the authoritative source of
# the directive text — this script only persists the result surgically.

DIRECTIVES_HEADER = "### DYNAMIC DIRECTIVES ###"
DIRECTIVES_FOOTER = "### END DYNAMIC DIRECTIVES ###"
DIRECTIVES_NOTE = (
    "<!-- Auto-maintained by Engage Mate AI triage. Each bullet below "
    "originates from a client note (Client Supremacy). Do not hand-edit "
    "bullets — the deployment script will de-duplicate and re-render "
    "this block on the next approved fix. -->"
)

DIRECTIVE_ACTION_SKIPPED  = "skipped-duplicate"
DIRECTIVE_ACTION_UPGRADED = "upgraded"
DIRECTIVE_ACTION_APPENDED = "appended"
DIRECTIVE_ACTION_CREATED  = "created-block"

# Jaccard threshold at which two directives count as semantically
# equivalent even without a substring match. Set at 0.80 — high enough
# to keep genuinely different rules apart, low enough to catch re-phrased
# duplicates ("Do not ask twice" vs. "You must not ask the same thing twice").
_DEDUP_JACCARD_THRESHOLD = 0.80

# Patterns Qwen may have accidentally embedded in its `value` despite the
# prompt rule — strip them so the directive itself stays clean.
_LEADING_MARKER_RE = re.compile(r"^#{2,}[^\n]*#{2,}\s*", re.MULTILINE)
_LEADING_BULLET_RE = re.compile(r"^[-*•]\s+")


def _normalize_for_dedup(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for comparison."""
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokens(s: str) -> set:
    """Token set of the normalized string (used for Jaccard overlap)."""
    return set(_normalize_for_dedup(s).split())


def _directives_equivalent(a: str, b: str) -> bool:
    """
    Symmetric equivalence check between two directive bullets. True when:
      - exact match after normalization, OR
      - one normalized form is a substring of the other, OR
      - Jaccard token overlap ≥ _DEDUP_JACCARD_THRESHOLD.
    """
    na = _normalize_for_dedup(a)
    nb = _normalize_for_dedup(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb)
    union   = len(ta | tb)
    if union == 0:
        return False
    return (overlap / union) >= _DEDUP_JACCARD_THRESHOLD


def _find_block_bounds(content: str):
    """
    Return (start, end) for the DYNAMIC DIRECTIVES block in `content`:
    `start` is the first char of the header; `end` is just past the
    footer (or len(content) if the footer is missing). Returns None if
    the header is absent. Only the FIRST block is tracked — if the
    prompt somehow has multiple headers, the earliest one wins.
    """
    header_idx = content.find(DIRECTIVES_HEADER)
    if header_idx == -1:
        return None
    scan_from = header_idx + len(DIRECTIVES_HEADER)
    footer_idx = content.find(DIRECTIVES_FOOTER, scan_from)
    if footer_idx == -1:
        return (header_idx, len(content))
    return (header_idx, footer_idx + len(DIRECTIVES_FOOTER))


def _parse_directives(block_text: str) -> list:
    """
    Extract directive bullets from the full DYNAMIC DIRECTIVES block
    text. Markdown bullets (`- `) are the source of truth. Continuation
    lines (wrapped prose under a bullet) are folded back onto the bullet.
    Header / footer markers and `<!-- … -->` comments are dropped.
    """
    directives: list = []
    current: list = []
    for raw in block_text.splitlines():
        s = raw.strip()
        if not s:
            if current:
                directives.append(" ".join(current).strip())
                current = []
            continue
        if s.startswith("###"):
            if current:
                directives.append(" ".join(current).strip())
                current = []
            continue
        if s.startswith("<!--"):
            if current:
                directives.append(" ".join(current).strip())
                current = []
            continue
        if s.startswith("- "):
            if current:
                directives.append(" ".join(current).strip())
                current = []
            current.append(s[2:])
        else:
            if current:
                current.append(s)
    if current:
        directives.append(" ".join(current).strip())
    return [d for d in directives if d]


def _render_block(directives: list) -> str:
    """Render the full DYNAMIC DIRECTIVES block text from a directive list."""
    lines = [DIRECTIVES_HEADER, "", DIRECTIVES_NOTE, ""]
    for d in directives:
        lines.append(f"- {d.strip()}")
    lines.extend(["", DIRECTIVES_FOOTER])
    return "\n".join(lines)


def _strip_directive_artifacts(text: str) -> str:
    """
    Remove any stray `### marker ###` headers or leading bullet markers
    Qwen may have embedded in a directive `value` despite prompt rules.
    Keeps the directive text pure so it renders cleanly inside the block.
    """
    text = _LEADING_MARKER_RE.sub("", text).strip()
    text = _LEADING_BULLET_RE.sub("", text).strip()
    return text


def merge_directive(content: str, new_directive: str):
    """
    Merge `new_directive` into `content`'s DYNAMIC DIRECTIVES block.

    Returns (new_content, action, meta):
      new_content — the full prompt text to write back. Unchanged
                    (== content) on 'skipped-duplicate'.
      action      — one of DIRECTIVE_ACTION_SKIPPED / _UPGRADED /
                    _APPENDED / _CREATED.
      meta        — { matched / upgraded_from / directive_count / reason /
                    new_directive } — for terminal logging.

    Merge rules (in evaluation order):
      1. Empty / whitespace-only new directive → skipped ('empty-value').
      2. No block present → CREATE block at the very end of the prompt.
      3. Semantic match to any existing bullet → SKIPPED, unless the new
         directive is a strict superset (contains an existing bullet's
         normalized form AND is longer), in which case UPGRADE that slot.
      4. Otherwise → APPEND as a new bullet, preserving order.
    """
    new_directive = _strip_directive_artifacts(sanitize_text(new_directive).strip())
    meta: dict = {"new_directive": new_directive}

    if not new_directive:
        meta["reason"] = "empty-value"
        return (content, DIRECTIVE_ACTION_SKIPPED, meta)

    bounds = _find_block_bounds(content)

    if bounds is None:
        # Block missing — create it at the end of content.
        new_block = _render_block([new_directive])
        base = content.rstrip()
        new_content = (base + "\n\n" + new_block + "\n") if base else (new_block + "\n")
        meta["directive_count"] = 1
        return (new_content, DIRECTIVE_ACTION_CREATED, meta)

    block_start, block_end = bounds
    block_text = content[block_start:block_end]
    existing = _parse_directives(block_text)

    norm_new = _normalize_for_dedup(new_directive)
    for idx, ex in enumerate(existing):
        if _directives_equivalent(new_directive, ex):
            norm_ex = _normalize_for_dedup(ex)
            # Upgrade ONLY when the new directive strictly contains the
            # existing normalized form AND is longer — i.e. same rule,
            # more detail. Avoids churn on cosmetic re-wordings.
            if norm_ex and norm_ex in norm_new and len(new_directive) > len(ex):
                meta["upgraded_from"] = ex
                meta["slot"] = idx
                existing[idx] = new_directive
                new_block = _render_block(existing)
                new_content = content[:block_start] + new_block + content[block_end:]
                meta["directive_count"] = len(existing)
                return (new_content, DIRECTIVE_ACTION_UPGRADED, meta)
            meta["matched"] = ex
            meta["slot"] = idx
            return (content, DIRECTIVE_ACTION_SKIPPED, meta)

    # Truly new — append.
    existing.append(new_directive)
    new_block = _render_block(existing)
    new_content = content[:block_start] + new_block + content[block_end:]
    meta["directive_count"] = len(existing)
    return (new_content, DIRECTIVE_ACTION_APPENDED, meta)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def vapi_headers() -> dict:
    return {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}

def airtable_headers() -> dict:
    return {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

def airtable_table_url() -> str:
    return f"{AIRTABLE_BASE_URL}/{urllib.parse.quote(AIRTABLE_TABLE)}"


# ── Airtable ──────────────────────────────────────────────────────────────────

def fetch_approved_records(limit: int | None = None) -> list[dict]:
    """Fetch all records with Status == 'Approved'."""
    params = {
        "filterByFormula": f"{{{F_STATUS}}}='{STATUS_APPROVED}'",
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
    print(f"[airtable] Found {len(records)} Approved record(s)")
    return records


def fetch_record_by_id(record_id: str) -> dict:
    resp = requests.get(f"{airtable_table_url()}/{record_id}",
                        headers=airtable_headers(), timeout=60)
    resp.raise_for_status()
    return resp.json()


def update_airtable_record(record_id: str, fields: dict,
                           _max_attempts: int = 3, _backoff: int = 5) -> None:
    import time as _time
    for attempt in range(1, _max_attempts + 1):
        try:
            resp = requests.patch(f"{airtable_table_url()}/{record_id}",
                                  headers=airtable_headers(),
                                  json={"fields": fields, "typecast": True}, timeout=60)
            resp.raise_for_status()
            print(f"  [airtable] Updated {record_id} — {list(fields.keys())}")
            return
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt < _max_attempts:
                print(f"  [airtable] Update attempt {attempt} failed ({exc}), "
                      f"retrying in {_backoff}s …")
                _time.sleep(_backoff)
            else:
                print(f"  [airtable] Update failed after {_max_attempts} attempts: {exc}")
                raise


# ── Vapi (Assistants API) ────────────────────────────────────────────────────

def fetch_assistant(assistant_id: str) -> dict:
    """GET the full assistant JSON from Vapi."""
    resp = requests.get(f"{VAPI_BASE}/assistant/{assistant_id}",
                        headers=vapi_headers(), timeout=60)
    resp.raise_for_status()
    data = resp.json()
    name = data.get("name", assistant_id)
    print(f"  [vapi] Fetched assistant '{name}' ({assistant_id})")
    return data


def patch_assistant(assistant_id: str, payload: dict) -> requests.Response:
    """PATCH the assistant. Returns the raw response for status checking."""
    resp = requests.patch(f"{VAPI_BASE}/assistant/{assistant_id}",
                          headers=vapi_headers(),
                          json=payload, timeout=60)
    return resp


# ── System prompt helpers ────────────────────────────────────────────────────

def find_system_message(messages: list[dict]) -> dict | None:
    """Find the first message dict with role == 'system'."""
    for msg in messages:
        if msg.get("role") == "system":
            return msg
    return None


# ── Path traversal engine ────────────────────────────────────────────────────
# Paths are dotted, with optional [key=value] array filters at any segment:
#
#   "model.temperature"                          — nested dict key
#   "messagePlan.firstMessage"                   — nested dict key
#   "tools"                                      — top-level array
#   "tools[name=get_weather]"                    — pick one item in an array
#   "model.messages[role=system]"                — pick one item in an array
#   "model.messages[role=system].content"        — string leaf inside array item
#
# A path is parsed into a sequence of steps:
#   ("key",    name)          → dict lookup
#   ("filter", attr, value)   → find dict in current list where dict[attr]==value
#
# Every op (append_instruction, replace, add, remove) resolves against these
# steps — no per-field special cases, no hard-coded path strings.

_FILTER_RE = re.compile(r"^([^\[\]]*?)\[\s*([^=\[\]]+?)\s*=\s*(.+?)\s*\]$")


def _split_dots(field: str) -> list[str]:
    """Split on '.', but NOT on dots inside [key=value] brackets."""
    segments: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in field:
        if ch == "[":
            depth += 1
            buf.append(ch)
        elif ch == "]":
            depth -= 1
            buf.append(ch)
        elif ch == "." and depth == 0:
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        segments.append("".join(buf))
    return segments


def _parse_path(field: str) -> list[tuple]:
    """Parse a field string into a list of (kind, ...) steps."""
    steps: list[tuple] = []
    for seg in _split_dots(field):
        m = _FILTER_RE.match(seg)
        if m:
            name = m.group(1).strip()
            attr = m.group(2).strip()
            val  = m.group(3).strip()
            # Tolerate quoted filter values: [name="foo bar"] or [name='foo'].
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            if name:
                steps.append(("key", name))
            steps.append(("filter", attr, val))
        else:
            steps.append(("key", seg))
    return steps


def _top_level_of(field: str) -> str:
    """Top-level Assistant key for a path (strips any filter on segment 0)."""
    first = _split_dots(field)[0]
    br = first.find("[")
    return first[:br] if br != -1 else first


def _match_filter(lst: list, attr: str, val: str):
    """Return the first dict in `lst` where str(d[attr]) == val, else None."""
    for item in lst:
        if isinstance(item, dict) and str(item.get(attr)) == val:
            return item
    return None


def _walk_to_parent(obj, steps: list[tuple], create_missing: bool):
    """Walk every step except the last. Returns the parent container or None."""
    node = obj
    for step in steps[:-1]:
        if step[0] == "key":
            if not isinstance(node, dict):
                return None
            nxt = node.get(step[1])
            if nxt is None:
                if not create_missing:
                    return None
                nxt = {}
                node[step[1]] = nxt
            node = nxt
        else:  # filter
            if not isinstance(node, list):
                return None
            node = _match_filter(node, step[1], step[2])
            if node is None:
                return None
    return node


def get_by_path(obj, field: str):
    """Return the value at `field`, or None if any segment/filter fails."""
    node = obj
    for step in _parse_path(field):
        if step[0] == "key":
            if not isinstance(node, dict):
                return None
            node = node.get(step[1])
        else:  # filter
            if not isinstance(node, list):
                return None
            node = _match_filter(node, step[1], step[2])
        if node is None:
            return None
    return node


def set_by_path(obj, field: str, value) -> None:
    """Set `value` at `field`. Creates intermediate dicts for key-only paths.
    Raises ValueError if a filter step cannot be resolved."""
    steps = _parse_path(field)
    if not steps:
        raise ValueError("Empty path")
    parent = _walk_to_parent(obj, steps, create_missing=True)
    if parent is None:
        raise ValueError(
            f"Cannot resolve parent of '{field}' "
            f"(array filter did not match, or intermediate path missing)."
        )
    final = steps[-1]
    if final[0] == "key":
        if not isinstance(parent, dict):
            raise ValueError(
                f"Expected dict parent at '{field}', got {type(parent).__name__}"
            )
        parent[final[1]] = value
    else:  # filter — replace the matching item in the parent list
        if not isinstance(parent, list):
            raise ValueError(
                f"Expected list parent for filter at '{field}', "
                f"got {type(parent).__name__}"
            )
        attr, val = final[1], final[2]
        for i, item in enumerate(parent):
            if isinstance(item, dict) and str(item.get(attr)) == val:
                parent[i] = value
                return
        raise ValueError(f"No item in array at '{field}' where {attr}={val}")


def remove_by_path(obj, field: str) -> None:
    """Delete the leaf at `field`. No-op if any segment is missing."""
    steps = _parse_path(field)
    if not steps:
        return
    parent = _walk_to_parent(obj, steps, create_missing=False)
    if parent is None:
        return
    final = steps[-1]
    if final[0] == "key":
        if isinstance(parent, dict):
            parent.pop(final[1], None)
    else:  # filter — drop the matching item from the parent list
        if isinstance(parent, list):
            attr, val = final[1], final[2]
            for i, item in enumerate(parent):
                if isinstance(item, dict) and str(item.get(attr)) == val:
                    parent.pop(i)
                    return


# ── Patch application ────────────────────────────────────────────────────────

def apply_operation_in_place(
    assistant: dict, operation: str, field: str, value
):
    """
    Apply `operation` to the fetched `assistant` dict **in-place** at
    `field` (a dotted path with optional [key=value] array filters).

    Returns `(top_level_key, modified, meta)`:
      top_level_key — the top-level Assistant key that was touched.
                      Caller builds the PATCH body as
                      `{top_level_key: assistant[top_level_key]}` so
                      every sibling of the modified leaf stays intact.
      modified      — True if the in-memory dict was actually changed.
                      False for idempotent no-ops (append_instruction
                      that matched an existing DYNAMIC DIRECTIVES bullet).
                      Caller uses this to decide whether to PATCH Vapi.
      meta          — diagnostics for terminal logs: {'action': ...,
                      'matched': ..., 'upgraded_from': ...,
                      'directive_count': ...}.

    Every string inside `value` is passed through sanitize_value() BEFORE
    any mutation — "clean UTF-8 on write". Non-string scalars (int,
    float, bool) and None pass through unchanged.

    Supported operations
    --------------------
    append_instruction  Merge `value` as ONE bullet into the
                        ### DYNAMIC DIRECTIVES ### block of the target
                        string leaf. Semantic + exact dedup vs. existing
                        bullets; returns modified=False on a duplicate
                        so the caller can skip the Vapi PATCH. If the
                        block doesn't exist, it's CREATED at the end
                        of the target string. Legacy fallback: if
                        `field` resolves to the messages ARRAY (e.g.
                        bare "model.messages"), dig for the system
                        message inside and merge into its `content`.

    replace             Overwrite the leaf at `field`. Works for any
                        leaf type — scalar (model.model,
                        voice.stability, voice.similarityBoost), list
                        (transcriber.keywords), or dict. Parent objects
                        are preserved: replacing voice.stability leaves
                        every other voice.* key untouched because
                        set_by_path walks to the parent dict and sets
                        one key.

    add                 Append `value` to the array at `field`. Creates
                        an empty list at that path first if missing.

    remove              Delete the leaf at `field`.

    Raises ValueError on: unknown operations, empty `field`, unresolved
    array filters, type mismatches ('add' on a non-array,
    append_instruction on anything except a string or messages array).
    """
    if not field:
        raise ValueError("Patch is missing 'field' — cannot locate target path.")

    # Sanitize every string inside `value` BEFORE it touches the live
    # assistant dict. "Clean UTF-8 on write" — Qwen's output has usually
    # already been through json.loads, but defensive depth catches stray
    # literal `\uXXXX` sequences and fancy punctuation that would
    # otherwise survive to the Vapi API.
    clean_value = sanitize_value(value)

    if operation == "append_instruction":
        if not clean_value:
            raise ValueError(
                "append_instruction patch has empty value — nothing to append"
            )
        if not isinstance(clean_value, str):
            raise ValueError(
                "append_instruction value must be a string (got "
                f"{type(clean_value).__name__})."
            )

        target = get_by_path(assistant, field)
        top_level = _top_level_of(field)

        if isinstance(target, str):
            # New shape: `field` points directly at a string leaf, e.g.
            # "model.messages[role=system].content". Merge into its
            # DYNAMIC DIRECTIVES block.
            new_content, action, meta = merge_directive(target, clean_value)
            if action == DIRECTIVE_ACTION_SKIPPED:
                matched = meta.get("matched") or meta.get("reason") or "(semantic match)"
                print(f"  [patch] SKIPPED — directive already present at '{field}'")
                print(f"          matched: {matched!r}")
                return (top_level, False, {"action": action, **meta})
            set_by_path(assistant, field, new_content)
            label = {
                DIRECTIVE_ACTION_UPGRADED: "UPGRADED an existing directive (longer form)",
                DIRECTIVE_ACTION_APPENDED: "APPENDED a new directive to DYNAMIC DIRECTIVES",
                DIRECTIVE_ACTION_CREATED:  "CREATED the DYNAMIC DIRECTIVES block (was absent)",
            }.get(action, action)
            print(f"  [patch] {label} at '{field}'")
            print(f"  [patch] Prompt length: {len(target)} -> {len(new_content)} chars "
                  f"(delta {len(new_content) - len(target):+d})")
            if action == DIRECTIVE_ACTION_UPGRADED:
                print(f"          upgraded_from: {meta.get('upgraded_from')!r}")
            if "directive_count" in meta:
                print(f"          directives in block: {meta['directive_count']}")
            return (top_level, True, {"action": action, **meta})

        elif isinstance(target, list):
            # Legacy shape: `field` points at a messages ARRAY, not a
            # string leaf. Dig for the system message and merge into its
            # .content — keeps old patches (bare "model.messages") working.
            sys_msg = find_system_message(target)
            if sys_msg is None:
                raise ValueError(
                    f"append_instruction at '{field}' resolved to an array "
                    f"with no system message."
                )
            current_content = sys_msg.get("content", "") or ""
            new_content, action, meta = merge_directive(current_content, clean_value)
            if action == DIRECTIVE_ACTION_SKIPPED:
                matched = meta.get("matched") or meta.get("reason") or "(semantic match)"
                print(f"  [patch] SKIPPED (legacy array path '{field}') — already present")
                print(f"          matched: {matched!r}")
                return (top_level, False, {"action": action, **meta})
            sys_msg["content"] = new_content
            print(f"  [patch] {action.upper()} via legacy array path '{field}'")
            print(f"  [patch] System-message content: "
                  f"{len(current_content)} -> {len(new_content)} chars")
            return (top_level, True, {"action": action, **meta})

        else:
            kind = type(target).__name__ if target is not None else "None"
            raise ValueError(
                f"append_instruction requires the path to resolve to a string "
                f"or array; '{field}' resolved to {kind}."
            )

    elif operation == "replace":
        # Surgical leaf replacement — parent objects stay intact because
        # set_by_path walks to the parent dict and mutates one key. This
        # is how voice.stability / voice.similarityBoost / model.model /
        # transcriber.keywords all work without clobbering siblings.
        old_val = get_by_path(assistant, field)
        set_by_path(assistant, field, clean_value)
        # Log scalar / short values verbosely so the terminal shows the
        # before-and-after. Complex values just show the type.
        if (isinstance(clean_value, (int, float, bool))
                or clean_value is None
                or (isinstance(clean_value, str) and len(clean_value) < 120)):
            print(f"  [patch] REPLACED leaf '{field}': {old_val!r} -> {clean_value!r}")
        else:
            print(f"  [patch] REPLACED '{field}' (type={type(clean_value).__name__})")
        return (_top_level_of(field), True, {"action": "replace"})

    elif operation == "add":
        arr = get_by_path(assistant, field)
        if arr is None:
            # Create an empty list at the path, then fall through to append.
            set_by_path(assistant, field, [])
            arr = get_by_path(assistant, field)
        if not isinstance(arr, list):
            raise ValueError(f"Path '{field}' is not an array — cannot 'add'")
        arr.append(clean_value)
        print(f"  [patch] ADDED item to '{field}' (now {len(arr)} entries)")
        return (_top_level_of(field), True, {"action": "add", "count": len(arr)})

    elif operation == "remove":
        remove_by_path(assistant, field)
        print(f"  [patch] REMOVED '{field}'")
        return (_top_level_of(field), True, {"action": "remove"})

    else:
        raise ValueError(f"Unknown operation: '{operation}'")


# ── Rollback helpers ─────────────────────────────────────────────────────────

def serialize_for_rollback(value) -> str:
    """
    Turn a pre-mutation value captured by `get_by_path` into a readable
    string for the F_ROLLBACK_STATE column.

      - None (field didn't exist) → ROLLBACK_SENTINEL
      - dict / list               → json.dumps(value, indent=2)
      - str                       → returned verbatim (avoids the
                                    \\n-escaping you'd get from json.dumps
                                    on multi-line system prompts)
      - everything else (int, float, bool, …) → str(value)
    """
    if value is None:
        return ROLLBACK_SENTINEL
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


# ── Process one record ────────────────────────────────────────────────────────

def process_record(record: dict, dry_run: bool = False) -> None:
    """Apply one Approved record's patch via the Vapi Assistants API."""
    record_id = record["id"]
    fields    = record.get("fields", {})

    print(f"\n{'='*64}")
    print(f"  Record: {record_id}")
    print(f"{'='*64}")

    # 1. Parse the Proposed Fix JSON
    raw_fix = fields.get(F_PROPOSED_FIX, "") or ""
    if not raw_fix or raw_fix.strip() == "{}":
        print("  [skip] Proposed Fix is empty — nothing to apply")
        update_airtable_record(record_id, {
            F_STATUS: STATUS_ERROR,
            F_RATIONALE: "Proposed Fix was empty. Nothing to deploy.",
        })
        return

    try:
        patch = json.loads(raw_fix)
    except json.JSONDecodeError as exc:
        print(f"  [error] Proposed Fix is not valid JSON: {exc}")
        update_airtable_record(record_id, {
            F_STATUS: STATUS_ERROR,
            F_RATIONALE: f"Proposed Fix JSON parse failed: {str(exc)[:200]}",
        })
        return

    # 2. Extract target assistant ID
    assistant_id = patch.get("target_assistant_id", "")
    operation    = patch.get("operation", "?")
    field        = patch.get("field", "?")

    if not assistant_id:
        print("  [error] No target_assistant_id in patch")
        update_airtable_record(record_id, {
            F_STATUS: STATUS_ERROR,
            F_RATIONALE: "Patch missing target_assistant_id — cannot identify target.",
        })
        return

    print(f"  [patch] assistant={assistant_id} | operation={operation} | field={field}")

    # 3. Fetch the live assistant from Vapi
    try:
        assistant = fetch_assistant(assistant_id)
    except requests.HTTPError as exc:
        print(f"  [error] Vapi assistant fetch failed: {exc}")
        update_airtable_record(record_id, {
            F_STATUS: STATUS_ERROR,
            F_RATIONALE: f"Vapi GET /assistant/{assistant_id} failed: {str(exc)[:250]}",
        })
        return

    # 3.5. Pre-PATCH idempotency gate — if the raw `value` from the patch
    #      is already present as an exact substring of the target
    #      assistant's system prompt, don't touch Vapi at all. Stricter
    #      and earlier than merge_directive()'s semantic dedup (which only
    #      fires for append_instruction). Protects against a re-approval
    #      of the same record, or two approved records carrying the same
    #      rule text verbatim. The Rationale gets a short suffix so the
    #      operator can tell a normal Applied from an idempotent skip.
    raw_value = patch.get("value")
    if isinstance(raw_value, str) and raw_value.strip():
        current_system_content = get_by_path(
            assistant, "model.messages[role=system].content"
        )
        if isinstance(current_system_content, str) \
                and raw_value in current_system_content:
            print("  [idempotency] value already present verbatim in "
                  "model.messages[role=system].content — skipping Vapi PATCH")
            if dry_run:
                print("  [dry-run] Would mark Applied and skip Vapi write")
                return
            existing_rationale = str(fields.get(F_RATIONALE, "") or "")
            skip_suffix = " (Skipped Vapi push: Fix already exists in prompt)"
            new_rationale = (existing_rationale + skip_suffix).strip()
            update_airtable_record(record_id, {
                F_STATUS:    STATUS_APPLIED,
                F_RATIONALE: new_rationale[:ROLLBACK_MAX_CHARS],
            })
            print(f"  [result] status={STATUS_APPLIED} "
                  f"(idempotent — value already in system prompt)")
            return

    # 4. Read-Modify-Write: mutate the full assistant dict in-memory at the
    #    dotted path, then ship back the ENTIRE modified top-level object.
    #
    #    The bug this replaces: we used to send nested deltas like
    #    {"model": {"messages": [...]}}. Vapi's PATCH treats the value of
    #    each top-level key as authoritative, so that payload WIPED OUT
    #    `model.provider`, `model.tools`, `model.temperature`, etc. and
    #    returned HTTP 400. The fix: always send the whole top-level
    #    object, intact, with only our targeted leaf changed.
    try:
        # Rollback safety — snapshot the CURRENT value at `field` BEFORE
        # mutating anything. Universal across ops (replace, add, remove,
        # append_instruction) and path shapes (scalar, dict, list,
        # bracket-filtered item). A missing path records the sentinel
        # rather than crashing — some ops (add, replace) legitimately
        # target a field that doesn't exist yet. The snapshot is only
        # WRITTEN to Airtable once we know the mutation will actually
        # produce a change (step 6) — an idempotent no-op needs no
        # rollback state.
        try:
            original_value = get_by_path(assistant, field)
        except Exception as exc:
            print(f"  [rollback] Path traversal raised on '{field}': {exc}")
            original_value = None

        rollback_snapshot = serialize_for_rollback(original_value)
        if original_value is None:
            print(f"  [rollback] No value at '{field}' — sentinel prepared")
        else:
            print(f"  [rollback] Pre-mutation '{field}' captured "
                  f"({len(rollback_snapshot)} chars)")

        # Apply the patch. Returns (top_level_key, modified, meta).
        # `modified` is False when the merge was a semantic no-op
        # (duplicate directive) — in that case we skip the Airtable
        # rollback write AND the Vapi PATCH, and move the record to
        # Applied so it exits the queue.
        top_level_key, modified, apply_meta = apply_operation_in_place(
            assistant, operation, field, patch.get("value")
        )

    except ValueError as exc:
        print(f"  [error] Patch application failed: {exc}")
        update_airtable_record(record_id, {
            F_STATUS: STATUS_ERROR,
            F_RATIONALE: f"Patch failed: {str(exc)[:300]}",
        })
        return

    # 5. Idempotent short-circuit — no mutation, no rollback write, no
    #    Vapi PATCH. The record still moves to Applied (dry-run skips).
    #    Rationale gets the same ' (Skipped Vapi push: ...)' suffix as
    #    the pre-PATCH gate at step 3.5, so the operator can tell an
    #    idempotent no-op (exact or semantic) from a normal Applied.
    if not modified:
        action  = apply_meta.get("action", "no-op")
        matched = apply_meta.get("matched", "(n/a)")
        print(f"  [patch] IDEMPOTENT — {action} (no Vapi write needed)")
        print(f"  [patch] Existing directive matched: {matched!r}")
        if dry_run:
            print("  [dry-run] Would mark Applied and skip Vapi / rollback writes")
            return
        existing_rationale = str(fields.get(F_RATIONALE, "") or "")
        skip_suffix = " (Skipped Vapi push: Fix already exists in prompt)"
        new_rationale = (existing_rationale + skip_suffix).strip()
        update_airtable_record(record_id, {
            F_STATUS:    STATUS_APPLIED,
            F_RATIONALE: new_rationale[:ROLLBACK_MAX_CHARS],
        })
        print(f"  [result] status={STATUS_APPLIED} (semantic no-op)")
        return

    # 6. Real mutation — persist the rollback snapshot FIRST (mandatory
    #    safety net before any Vapi call), then build the PATCH payload.
    #    Dry-run skips every external write so preview is side-effect-free.
    if not dry_run:
        update_airtable_record(record_id, {
            F_ROLLBACK_STATE: rollback_snapshot[:ROLLBACK_MAX_CHARS],
        })

    # Send the entire, intact, newly-modified top-level object.
    # Using .get() rather than [] so a top-level 'remove' (which deletes
    # the key) serialises as `null` — Vapi's "unset" semantic.
    vapi_payload = {top_level_key: assistant.get(top_level_key)}
    print(f"  [patch] PATCH payload top-level key: '{top_level_key}'")

    # 7. Push to Vapi (or preview in dry-run mode).
    if dry_run:
        print("  [dry-run] Would PATCH assistant — skipping")
        preview = json.dumps(vapi_payload, indent=2, ensure_ascii=False)
        print(f"  [dry-run] Payload preview ({len(preview)} chars, first 800 shown):")
        print(preview[:800] + ("..." if len(preview) > 800 else ""))
        return

    print(f"  [vapi] PATCHing assistant {assistant_id} …", end=" ", flush=True)
    resp = patch_assistant(assistant_id, vapi_payload)

    if resp.status_code == 200:
        print(f"OK (HTTP {resp.status_code})")
        update_airtable_record(record_id, {F_STATUS: STATUS_APPLIED})
        print(f"  [result] status={STATUS_APPLIED}")
    else:
        error_preview = resp.text[:400]
        print(f"FAILED (HTTP {resp.status_code})")
        print(f"  [vapi] Response: {error_preview}")
        update_airtable_record(record_id, {
            F_STATUS: STATUS_ERROR,
            F_RATIONALE: f"Vapi PATCH returned HTTP {resp.status_code}: {error_preview[:300]}",
        })
        print(f"  [result] status={STATUS_ERROR}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Engage Mate AI — Apply Approved Fixes (Assistants API)"
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N Approved records")
    parser.add_argument("--record-id", type=str, default=None,
                        help="Target a single record by ID (bypasses status filter)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview patches without writing to Vapi or updating status")
    args = parser.parse_args()

    # Validate env — VAPI_WORKFLOW_ID no longer needed; assistant ID
    # comes from each record's jsonPatch.target_assistant_id.
    missing = [v for v in ("VAPI_API_KEY", "AIRTABLE_API_KEY",
                           "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME")
               if not os.getenv(v)]
    if missing:
        print(f"[error] Missing .env vars: {', '.join(missing)}")
        sys.exit(1)

    print("=" * 64)
    print("  Engage Mate AI — Apply Approved Fixes (Assistants API)")
    print(f"  Table    : {AIRTABLE_TABLE}")
    print(f"  API      : {VAPI_BASE}/assistant/{{id}}")
    if args.dry_run:
        print("  Mode     : DRY RUN (no Vapi writes)")
    else:
        print("  Mode     : LIVE (will PATCH assistants)")
    if args.record_id:
        print(f"  Target   : {args.record_id}")
    else:
        print(f"  Limit    : {args.limit or 'ALL'}")
    print("=" * 64)

    # Fetch Approved records
    print("\n[step 1] Querying Airtable for Approved records …")
    if args.record_id:
        records = [fetch_record_by_id(args.record_id)]
        print(f"[airtable] Loaded target record {args.record_id}")
    else:
        records = fetch_approved_records(limit=args.limit)

    if not records:
        print("[done] No Approved records to process.")
        return

    # Apply each patch — each record targets its own assistant,
    # so there's no shared state between records.
    total = len(records)
    applied = errors = 0
    print(f"\n[step 2] Applying {total} fix(es) …")

    for i, record in enumerate(records, 1):
        print(f"\n[{i}/{total}]")
        try:
            process_record(record, dry_run=args.dry_run)
            applied += 1
        except Exception as exc:
            print(f"  [error] Unhandled exception: {exc}")
            errors += 1
            try:
                update_airtable_record(record["id"], {
                    F_STATUS: STATUS_ERROR,
                    F_RATIONALE: f"Script error: {str(exc)[:460]}",
                })
            except Exception:
                pass

    # Summary
    print("\n" + "=" * 64)
    print(f"  Run complete — {total} record(s) processed")
    if args.dry_run:
        print(f"  DRY RUN: {applied} previewed, no changes written to Vapi")
    else:
        print(f"  Applied: {applied} | Errors: {errors}")
    print(f"  Airtable status: '{STATUS_APPLIED}' (success), "
          f"'{STATUS_ERROR}' (failure)")
    print(f"  Rollback column: '{F_ROLLBACK_STATE}' — pre-mutation field state saved before patching")
    print("=" * 64)


if __name__ == "__main__":
    main()