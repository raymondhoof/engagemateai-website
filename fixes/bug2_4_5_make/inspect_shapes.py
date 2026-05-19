"""Inspect the EXACT snapshot blueprint shapes so the transformer targets
real structures, not assumptions. Read-only."""
import json, os

SNAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_snapshots")
raw = json.load(open(os.path.join(SNAP, "blueprint_3442510.before.json"), encoding="utf-8"))

print("TOP-LEVEL keys:", list(raw.keys()))
# unwrap
bp = raw
for path in ("response.blueprint", "blueprint"):
    cur = raw
    ok = True
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            ok = False
            break
    if ok:
        bp = cur
        print("blueprint found at:", path)
        break
print("BLUEPRINT keys:", list(bp.keys()))
flow = bp.get("flow", [])
print("flow top-level modules:", len(flow))


def walk(fl, depth=0):
    for m in fl:
        yield m, depth
        for r in m.get("routes", []) or []:
            yield from walk(r.get("flow", []), depth + 1)


mods = list(walk(flow))
print("total modules incl nested:", len(mods))

# trigger
print("\n--- TRIGGER (flow[0]) verbatim ---")
print(json.dumps(flow[0], indent=2)[:900])

# all WebhookRespond
wr = [m for m, _ in mods if m.get("module") == "gateway:WebhookRespond"]
print("\n--- WebhookRespond modules: %d ; ids=%s ---" %
      (len(wr), [m.get("id") for m in wr]))
print("one verbatim:", json.dumps(wr[0], indent=2)[:1100] if wr else "NONE")

# BasicRouter version check
brs = [m for m, _ in mods if m.get("module") == "builtin:BasicRouter"]
print("\n--- BasicRouters: %d ---" % len(brs))
for b in brs:
    print("  id=%s version=%s routes=%d mapper=%s" %
          (b.get("id"), b.get("version"), len(b.get("routes", [])), b.get("mapper")))

# AppFolio modules + filter shape
print("\n--- AppFolio module #3 verbatim (filter/onerror shape) ---")
for m, _ in mods:
    if m.get("id") == 3:
        print(json.dumps(m, indent=2)[:1600])

# property-match filter module #5
print("\n--- Module #5 filter verbatim ---")
for m, _ in mods:
    if m.get("id") == 5:
        print(json.dumps({"id": m.get("id"), "module": m.get("module"),
                           "filter": m.get("filter")}, indent=2)[:1400])

# #210 fail hack
print("\n--- #210 fail-hack verbatim ---")
for m, _ in mods:
    if m.get("id") == 210:
        print(json.dumps(m, indent=2)[:1200])

# metadata
print("\n--- blueprint.metadata ---")
print(json.dumps(bp.get("metadata"), indent=2)[:500])
# any existing onerror anywhere?
oe = [(m.get("id"), m.get("onerror")) for m, _ in mods if m.get("onerror")]
print("\nmodules with onerror:", oe)
# designer coords sample
print("sample metadata.designer:", json.dumps(flow[0].get("metadata"))[:200])
