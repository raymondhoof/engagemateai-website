"""Inspect specific Vapi calls in depth: which assistants spoke, what tools
fired, transcript tail. Read-only."""
import json, os, sys, urllib.request, urllib.error, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ctx = ssl.create_default_context()
E = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    ln = ln.strip()
    if ln and "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        E[k.strip()] = v.strip()
VH = {"Authorization": "Bearer " + E["VAPI_API_KEY"], "User-Agent": "EllieFix/1.0"}
ELL = {"6236e418-b4d0-480f-aeb6-c245af67d273": "Router",
       "6892f5e0-8cb0-4362-9bcf-d295df6894d9": "Prospect",
       "8968c13a-b097-4387-b292-f4ffdd88c5cd": "Tenant",
       "8f33b6fd-c1af-4ad5-9728-83916c805b43": "Owner",
       "5d8324fb-2ff5-4a93-b9d9-22e9f02d4a55": "Vendor",
       "afc690ca-cbb3-4cb0-8dad-7cc5aabec5c0": "Uncertain"}


def get(u):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=VH), timeout=30, context=ctx) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


# pick the 5 most recent + the silence-timed-out + a few middle
TARGETS = sys.argv[1:] or [
    "019e4786-f3b5-7665-b2a6-e701870393f3",  # 22:34
    "019e4781-5be4-7556-9127-a7487f7ec2bf",  # 22:28
    "019e4707-7d8e-7559-9caa-808121cf6704",  # 20:15
    "019e460c-8278-7000-9e69-4125d175c6db",  # 15:41 silence-timed-out
    "019e45fa-43c6-7eeb-bbc2-755d671dad07",  # 15:21
    "019e41ce-1b02-7ff2-887c-6a6c472cf52d",  # 19:54 yesterday
]

for cid in TARGETS:
    st, b = get("https://api.vapi.ai/call/%s" % cid)
    if st != 200:
        print("\n%s -> HTTP %s" % (cid, st)); continue
    c = json.loads(b)
    msgs = c.get("messages") or []
    # which assistants spoke (squad call: assistantId rotates via handoff)
    asst_seen = set()
    for m in msgs:
        aid = m.get("assistantId") or (m.get("metadata") or {}).get("assistantId")
        if aid:
            asst_seen.add(aid)
    # tool calls + results paired
    tools = []
    for m in msgs:
        if m.get("role") == "tool_calls":
            for tc in (m.get("toolCalls") or []):
                tools.append(("CALL", (tc.get("function") or {}).get("name")))
        elif m.get("role") in ("tool_call_result", "tool"):
            res = m.get("result")
            rs = res if isinstance(res, str) else json.dumps(res)
            tools.append(("RESULT", m.get("name"), (rs or "")[:200]))
    print("\n" + "=" * 78)
    print("%s  created=%s  ended=%s  dur=%ss" %
          (cid, c.get("createdAt"), c.get("endedReason"),
           c.get("durationSeconds") or "?"))
    print("squadId=%s  assistantId(top)=%s" % (c.get("squadId"), c.get("assistantId")))
    print("assistants observed in messages: %s" %
          [ELL.get(a, a) for a in asst_seen] if asst_seen else "  (none in message metadata)")
    print("tool sequence (%d entries):" % len(tools))
    for t in tools[:20]:
        print(" ", t)
    tr = c.get("transcript") or ""
    print("transcript tail (last 700 chars):")
    print(repr(tr[-700:]))
