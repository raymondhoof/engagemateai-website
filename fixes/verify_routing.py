"""Verify per-persona handoff target. For each post-cutover Keyrenter call,
dump message-level metadata to identify which specialist handled it."""
import json, os, urllib.request, urllib.error, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ctx = ssl.create_default_context()
E = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    ln = ln.strip()
    if ln and "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        E[k.strip()] = v.strip()
VH = {"Authorization": "Bearer " + E["VAPI_API_KEY"], "User-Agent": "EllieFix/1.0"}
KR = "80ccd39f-f2a6-4035-aeb9-ddb6eff59875"
CUT = "2026-05-19T15:56:15Z"
ELL = {"6236e418-b4d0-480f-aeb6-c245af67d273": "Router",
       "6892f5e0-8cb0-4362-9bcf-d295df6894d9": "Prospect",
       "8968c13a-b097-4387-b292-f4ffdd88c5cd": "Tenant",
       "8f33b6fd-c1af-4ad5-9728-83916c805b43": "Owner",
       "5d8324fb-2ff5-4a93-b9d9-22e9f02d4a55": "Vendor",
       "afc690ca-cbb3-4cb0-8dad-7cc5aabec5c0": "Uncertain"}


def g(u):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=VH), timeout=30, context=ctx) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


_, b = g("https://api.vapi.ai/call?limit=40")
calls = [c for c in json.loads(b)
         if (c.get("createdAt") or "") > CUT and c.get("squadId") == KR]
print("Keyrenter post-cutover calls: %d" % len(calls))

for c in calls:
    cid = c.get("id")
    _, fb = g("https://api.vapi.ai/call/%s" % cid)
    full = json.loads(fb)
    msgs = full.get("messages") or []
    # find handoff result + the toolCall args (destinations are in handoff tool config,
    # but the specific destination chosen appears in the handoff call's arguments)
    print("\n%s  ended=%s" % (cid, full.get("endedReason")))
    # scan all message keys to find any that name a target assistant
    seen_keys = set()
    for m in msgs:
        seen_keys.update(m.keys())
    print("  all msg keys observed:", sorted(seen_keys))
    # look at handoff tool_call message arguments + result
    for i, m in enumerate(msgs):
        if m.get("role") == "tool_calls":
            for tc in (m.get("toolCalls") or []):
                fn = (tc.get("function") or {}).get("name") or ""
                if "handoff" in fn.lower() or "route" in fn.lower():
                    args = (tc.get("function") or {}).get("arguments")
                    print("  handoff CALL #%d  fn=%s  args=%s" % (i, fn, str(args)[:300]))
        if m.get("role") in ("tool_call_result", "tool"):
            name = m.get("name") or ""
            if "handoff" in name.lower() or "route" in name.lower():
                print("  handoff RESULT #%d  name=%s  result=%r  metadata=%s"
                      % (i, name, m.get("result"), json.dumps(m.get("metadata"))[:200]))
    # also dump the summary + transcript fragment around handoff to infer destination
    print("  summary:", repr((full.get("summary") or "")[:200]))
    # transcript clue — what does AI say RIGHT AFTER the handoff initiated?
    tr = full.get("transcript", "")
    if "Handoff initiated" not in tr:
        # find handoff timestamp from messages -> show transcript line just after
        pass
    # NEW: dump the entire transcript first 15 lines to see opening turn pattern
    lines = [l for l in tr.split("\n") if l.strip()][:14]
    for l in lines:
        print("   ", l[:140])
    # ONE call full message dump for reverse engineering — save first call's raw msgs
    if calls.index(c) == 0:
        with open(os.path.join(ROOT, "_audit", "call_msgs_full_%s.json" % cid), "w", encoding="utf-8") as f:
            json.dump(msgs, f, indent=2)
        print("  (full messages saved to _audit/call_msgs_full_%s.json)" % cid)
