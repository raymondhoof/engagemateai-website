"""BUG-1 builder — produce the corrected route_caller tool + Keyrenter squad
PATCH bodies, derived from the live snapshot, mirroring the WORKING Canvas
Living handoff pattern.

Root cause (proven by diff vs Canvas squad c3e98feb, which works):
  A) Keyrenter Router squad-member carries `assistantDestinations` with NO
     descriptions -> shadows the handoff tool -> "No handoff destination
     returned." Canvas members have NO assistantDestinations.
  B) route_caller has a custom `function`/`parameters` block (persona/
     confidence enums). VAPI native `handoff` routes on destination
     `description`, not a custom function. Canvas handoff tools have NO
     `function` and set `contextEngineeringPlan:{"type":"all"}` per dest.

Fix (mirrors Canvas exactly):
  - Tool: drop `function` (PATCH sends function:null = VAPI unset, the
    established convention in apply_approved_fix.py), keep type=handoff,
    keep destinations, add contextEngineeringPlan:{"type":"all"} to each,
    rewrite descriptions to clean intent criteria (no phantom confidence).
  - Squad: every member becomes just {assistantId} (strip the Router
    member's assistantDestinations), matching Canvas.

Pure transform. Reads fixes/_snapshots/, writes fixes/bug1_squad_handoff/.
  python fixes/bug1_squad_handoff/build.py
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(os.path.dirname(HERE), "_snapshots")

# Clean, intent-based destination descriptions (criteria, not a phantom
# 'confidence' variable). Keyed by destination assistantId.
DEST_DESCRIPTIONS = {
    "6892f5e0-8cb0-4362-9bcf-d295df6894d9":
        "Transfer here if the caller is a PROSPECT: asking about touring/"
        "viewing, scheduling, availability, rent, moving in, or an application/"
        "application status for a property they do not yet rent.",
    "8968c13a-b097-4387-b292-f4ffdd88c5cd":
        "Transfer here if the caller is a current TENANT/resident: maintenance, "
        "repairs, rent payment, lease questions, or any current-resident concern.",
    "8f33b6fd-c1af-4ad5-9728-83916c805b43":
        "Transfer here if the caller is a property OWNER: property management "
        "services, renting out their property, or investment/owner consultation.",
    "5d8324fb-2ff5-4a93-b9d9-22e9f02d4a55":
        "Transfer here ONLY if the caller explicitly wants to become a VENDOR / "
        "approved service provider (a vendor application). Not for general "
        "maintenance mentions.",
    "afc690ca-cbb3-4cb0-8dad-7cc5aabec5c0":
        "Transfer here if intent is unclear/general, the caller is vague, or "
        "they ask for Debbie / a manager / a specific person / a human. This "
        "assistant takes a message for the team.",
}


def load(name):
    with open(os.path.join(SNAP, name), encoding="utf-8") as f:
        return json.load(f)


def main():
    tool = load("tool_route_caller.before.json")
    squad = load("squad_keyrenter.before.json")

    # ---- Tool fix ----
    dests = []
    for d in tool.get("destinations", []):
        aid = d.get("assistantId")
        nd = {
            "type": "assistant",
            "assistantId": aid,
            "description": DEST_DESCRIPTIONS.get(aid, d.get("description", "")),
            "contextEngineeringPlan": {"type": "all"},
        }
        dests.append(nd)
    if len(dests) != 5:
        raise SystemExit("Expected 5 destinations, got %d — aborting." % len(dests))

    tool_patch = {
        "type": "handoff",
        "function": None,          # VAPI unset convention (see apply_approved_fix.py)
        "destinations": dests,
    }

    # ---- Squad fix ----
    members = squad.get("members", [])
    if len(members) != 6:
        raise SystemExit("Expected 6 squad members, got %d — aborting." % len(members))
    new_members = [{"assistantId": m["assistantId"]} for m in members]
    squad_patch = {"members": new_members}

    with open(os.path.join(HERE, "tool_route_caller.fixed.json"), "w", encoding="utf-8") as f:
        json.dump(tool_patch, f, indent=2)
    with open(os.path.join(HERE, "squad_keyrenter.fixed.json"), "w", encoding="utf-8") as f:
        json.dump(squad_patch, f, indent=2)

    print("BUG-1 artifacts built:")
    print("  tool_route_caller.fixed.json  — PATCH /tool/%s" % tool["id"])
    print("     - function: REMOVED (was custom persona/confidence schema)")
    print("     - destinations: 5, each + contextEngineeringPlan{type:all}, clean descriptions")
    print("  squad_keyrenter.fixed.json    — PATCH /squad/%s" % squad["id"])
    had = sum(1 for m in members if m.get("assistantDestinations"))
    print("     - members: 6, assistantDestinations stripped from %d member(s) (Router)" % had)
    print("\nBefore/after (Router member):")
    print("  before keys:", list(members[0].keys()))
    print("  after  keys:", list(new_members[0].keys()), "(matches Canvas working pattern)")


if __name__ == "__main__":
    main()
