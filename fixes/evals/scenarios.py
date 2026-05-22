"""9 eval scenarios. 7 verify Router handoff routing against the squad.
2 verify Notice-to-Vacate directive surfaces in Tenant + Uncertain prompts.

Each: {id, name, caller_line, target_type, target_id, check, eval_id?}
  check kinds:
    - {kind:"handoff_dest", expected_assistant_id:"..."}
    - {kind:"content_includes_all", patterns:["regex1","regex2",...]}
  fail_patterns (applied to all): any regex hit in transcript fails.
"""
SQUAD = "80ccd39f-f2a6-4035-aeb9-ddb6eff59875"
ELLIE = {
    "Router":    "6236e418-b4d0-480f-aeb6-c245af67d273",
    "Prospect":  "6892f5e0-8cb0-4362-9bcf-d295df6894d9",
    "Tenant":    "8968c13a-b097-4387-b292-f4ffdd88c5cd",
    "Owner":     "8f33b6fd-c1af-4ad5-9728-83916c805b43",
    "Vendor":    "5d8324fb-2ff5-4a93-b9d9-22e9f02d4a55",
    "Uncertain": "afc690ca-cbb3-4cb0-8dad-7cc5aabec5c0",
}
FAIL_PATTERNS = [
    r"No handoff destination returned",
    r"\{\"error\":\s*\"unauthorized\"\}",
    r"invalid json response body",
    r"(?i)\b(?:transfer|patch through|put you through)\b",
]

SCENARIOS = [
    {
        "id": "1-prospect-routing",
        "name": "Routing: prospect tour request",
        "caller_line": "Hi, I'd like to schedule a tour at 1202 Franklin Street Northeast.",
        "target_type": "squad",
        "target_id": SQUAD,
        "check": {"kind": "handoff_dest", "expected_assistant_id": ELLIE["Prospect"]},
    },
    {
        "id": "2-tenant-maintenance-routing",
        "name": "Routing: tenant maintenance",
        "caller_line": "I'm a current resident at unit 302, the kitchen sink is leaking.",
        "target_type": "squad",
        "target_id": SQUAD,
        "check": {"kind": "handoff_dest", "expected_assistant_id": ELLIE["Tenant"]},
    },
    {
        "id": "3-tenant-vacate-routing",
        "name": "Routing: tenant notice to vacate (NEW BUG-fix path)",
        "caller_line": "I want to give my notice to vacate.",
        "target_type": "squad",
        "target_id": SQUAD,
        "check": {"kind": "handoff_dest", "expected_assistant_id": ELLIE["Tenant"]},
    },
    {
        "id": "4-owner-routing",
        "name": "Routing: owner management services",
        "caller_line": "I own 55 Todd Place and want to talk about your management services.",
        "target_type": "squad",
        "target_id": SQUAD,
        "check": {"kind": "handoff_dest", "expected_assistant_id": ELLIE["Owner"]},
    },
    {
        "id": "5-vendor-routing",
        "name": "Routing: vendor application",
        "caller_line": "I'd like to become an approved vendor for Keyrenter.",
        "target_type": "squad",
        "target_id": SQUAD,
        "check": {"kind": "handoff_dest", "expected_assistant_id": ELLIE["Vendor"]},
    },
    {
        "id": "6-uncertain-debbie-routing",
        "name": "Routing: Debbie request -> Uncertain",
        "caller_line": "Can I speak to Debbie?",
        "target_type": "squad",
        "target_id": SQUAD,
        "check": {"kind": "handoff_dest", "expected_assistant_id": ELLIE["Uncertain"]},
    },
    {
        "id": "7-uncertain-debbie-vacate-routing",
        "name": "Routing: Debbie + vacate -> Uncertain (handoff only)",
        "caller_line": "Can I speak to Debbie? It's about preparing to vacate.",
        "target_type": "squad",
        "target_id": SQUAD,
        "check": {"kind": "handoff_dest", "expected_assistant_id": ELLIE["Uncertain"]},
    },
    {
        "id": "8-tenant-vacate-directive",
        "name": "Directive: Tenant Notice-to-Vacate surfaces portal language",
        "caller_line": "I want to give my notice to vacate.",
        "target_type": "assistant",
        "target_id": ELLIE["Tenant"],
        "check": {"kind": "content_includes_all",
                  "patterns": [r"(?i)resident portal", r"(?i)notice to vacate"]},
    },
    {
        "id": "9-uncertain-vacate-directive",
        "name": "Directive: Uncertain Notice-to-Vacate surfaces portal language",
        "caller_line": "I'm calling for Debbie. I'm preparing to vacate my unit.",
        "target_type": "assistant",
        "target_id": ELLIE["Uncertain"],
        "check": {"kind": "content_includes_all",
                  "patterns": [r"(?i)resident portal", r"(?i)notice to vacate"]},
    },
]
