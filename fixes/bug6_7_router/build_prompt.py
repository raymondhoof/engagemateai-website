"""BUG-6 + BUG-7 — surgically fix the Ellie Router system prompt.

BUG-6: the "Once you have collected a piece of information..." directive
appears 3x (two stray `### [Auto-Update] ###` blocks + DYNAMIC DIRECTIVES
bullet #1). Remove ALL three `### [Auto-Update] ###` blocks; their only
unique content ("never speak internal routing/tool logic; keep transitions
conversational") is folded into DYNAMIC DIRECTIVES as ONE bullet. The
"captured info" rule already survives as DYNAMIC DIRECTIVES bullet #1.

BUG-7: BUG-1 converts route_caller to a NATIVE handoff (no custom
persona/confidence/transfer_request function). So every prompt instruction
that tells the model to "call route_caller with persona, confidence,
transfer_request" / gate on "confidence >= 0.7" is now wrong and must go.
The triage section is rewritten to the proven Canvas pattern: triage
naturally, clarify up to 2x, then SILENTLY hand off to the right
specialist (the handoff tool's destination descriptions do the routing).
Behavioural intent preserved (person->Uncertain, ack guardrail, route-now
signals, 2-question clarify).

Header (company/emergency/response guidelines) and the DYNAMIC DIRECTIVES
block are preserved verbatim (only the 3 Auto-Update blocks removed + 1
bullet added). Fancy punctuation -> ASCII via the SAME map as
apply_approved_fix.py (fixes existing mojibake, keeps pipeline-consistent).

Pure transform. python fixes/bug6_7_router/build_prompt.py
"""
import json, os, re, unicodedata, difflib

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(os.path.dirname(HERE), "_snapshots")
ROUTER_ID = "6236e418-b4d0-480f-aeb6-c245af67d273"

_PUNCT = {0x2013: "-", 0x2014: "-", 0x2015: "-", 0x2018: "'", 0x2019: "'",
          0x201A: "'", 0x201B: "'", 0x201C: '"', 0x201D: '"', 0x201E: '"',
          0x201F: '"', 0x2026: "...", 0x00A0: " ", 0x2022: "*", 0x00B7: "*"}
_ZW = re.compile("[​-‍⁠﻿]")


def sanitize(t):
    t = unicodedata.normalize("NFC", t).translate(_PUNCT)
    return _ZW.sub("", t)


NEW_TRIAGE = """RECEPTIONIST TRIAGE
Behave like a receptionist first. Greet briefly, find out why they're calling, then silently hand the call to the right specialist. Never mention routing, handoffs, tools, internal variables, or departments. Never say "transfer", "connect", "patch through", or "one moment while I connect you".

Who handles what (hand off as soon as it's clear):
- Prospect: touring/viewing/scheduling, availability, rent, moving in, or applications for a property they do NOT yet rent.
- Tenant: a current resident - maintenance, repairs, rent payment, or lease questions.
- Owner: property management services, renting out their property, or investment/owner consultation.
- Vendor: ONLY an explicit request to become an approved vendor / service provider (not general maintenance mentions).
- Uncertain: intent still unclear after clarifying, OR the caller asks for Debbie / Miss Gomes / a manager / a specific person / a human.

ROUTE-IMMEDIATELY SIGNALS (hand to Prospect right away, skip clarifying questions):
Caller mentions a showing/tour/viewing AND any of: "no one is here / nobody showed up / agent not here"; lockbox / code / can't get in; "I'm here at the property" for a scheduled viewing.

PERSON / DEBBIE REQUEST (priority): if the caller asks for Debbie / a manager / a specific person / a human, hand off to Uncertain immediately (it takes a message). Never offer or imply a live transfer.

CLARIFY BEFORE ROUTING: if intent is unclear and they did NOT ask for a person, ask up to TWO short clarifying questions before handing off. Only after two clarifying questions with intent still unclear, hand off to Uncertain.

ACKNOWLEDGEMENT-ONLY GUARDRAIL: if the caller's last message is only filler ("yeah", "yes", "yep", "ok", "okay", "hello", "hi", "are you there", "you there", "ready", "mm-hmm", "mmhm", "mhm") and intent is unclear: respond with exactly "Got it." then ask exactly "What can I help you with today?" Add nothing else and do not hand off yet.

When intent is clear: say ONE short acknowledgement (max 12 words), then hand off to the matching specialist. Do not narrate or announce the handoff."""

CONSOLIDATED_BULLET = ("Never say or narrate anything about routing, handoffs, tools, internal "
                       "variables, confidence, or departments. Keep all transitions strictly "
                       "conversational and seamless.")


def main():
    a = json.load(open(os.path.join(SNAP, "assistant_router.before.json"), encoding="utf-8"))
    model = a["model"]
    msgs = model["messages"]
    si = next(i for i, m in enumerate(msgs) if m.get("role") == "system")
    orig = msgs[si]["content"]
    s = sanitize(orig)

    A = "RECEPTIONIST TRIAGE + CLASSIFICATION"
    B = "### [Auto-Update] ###"
    C = "### DYNAMIC DIRECTIVES ###"
    E = "### END DYNAMIC DIRECTIVES ###"
    for marker, label in ((A, "triage header"), (C, "DYNAMIC DIRECTIVES"), (E, "END marker")):
        assert s.count(marker) == 1, "anchor %r count=%d (expected 1)" % (label, s.count(marker))
    assert s.count(B) == 3, "expected 3 Auto-Update blocks, found %d" % s.count(B)

    head = s[:s.index(A)].rstrip()                       # company/emergency/guidelines + ---
    dyn_block = s[s.index(C):s.index(E) + len(E)]         # DYNAMIC DIRECTIVES ... END (verbatim)

    # add the one consolidated bullet just before the END marker (dedup-safe:
    # this exact text is not present anywhere already)
    assert CONSOLIDATED_BULLET not in dyn_block
    dyn_lines = dyn_block.rsplit(E, 1)
    dyn_fixed = dyn_lines[0].rstrip() + "\n- " + CONSOLIDATED_BULLET + "\n\n" + E

    new_content = head + "\n\n" + NEW_TRIAGE + "\n\n" + dyn_fixed + "\n"

    # ---- assertions: dedup + obsolete-mechanics removal ----
    assert "### [Auto-Update] ###" not in new_content, "Auto-Update blocks not fully removed"
    assert new_content.count("Once you have collected a piece of information") == 1, \
        "captured-info directive must appear exactly once"
    for banned in ("route_caller", "confidence >= 0.7", "confidence >= 0.85",
                   "transfer_request", 'persona="prospect"', "confidence < 0.7"):
        assert banned not in new_content, "obsolete mechanic still present: %r" % banned
    assert C in new_content and E in new_content, "DYNAMIC DIRECTIVES block lost"
    assert new_content.count("- ") >= 17, "DYNAMIC DIRECTIVES bullets shrank unexpectedly"

    # full assistant PATCH body (mirror apply_approved_fix.py: send whole model
    # object so siblings provider/tools/temperature/voice survive)
    new_model = json.loads(json.dumps(model))
    new_model["messages"][si]["content"] = new_content
    patch_body = {"model": new_model}

    with open(os.path.join(HERE, "router_system_prompt.fixed.txt"), "w", encoding="utf-8") as f:
        f.write(new_content)
    with open(os.path.join(HERE, "assistant_router.patch_body.json"), "w", encoding="utf-8") as f:
        json.dump(patch_body, f, indent=2, ensure_ascii=False)
    diff = "".join(difflib.unified_diff(
        orig.splitlines(keepends=True), new_content.splitlines(keepends=True),
        "router_prompt.before", "router_prompt.after"))
    with open(os.path.join(HERE, "router_prompt.diff"), "w", encoding="utf-8") as f:
        f.write(diff)

    print("BUG-6+7 prompt fix built for assistant %s" % ROUTER_ID)
    print("  length: %d -> %d chars (delta %+d)" % (len(orig), len(new_content), len(new_content) - len(orig)))
    print("  removed: 3x '### [Auto-Update] ###' blocks; obsolete route_caller/confidence mechanics")
    print("  rewrote: triage section -> native silent-handoff pattern (Canvas-aligned)")
    print("  DYNAMIC DIRECTIVES: preserved + 1 consolidated bullet; 'captured-info' now appears exactly once")
    print("  artifacts: router_system_prompt.fixed.txt, assistant_router.patch_body.json, router_prompt.diff")


if __name__ == "__main__":
    main()
