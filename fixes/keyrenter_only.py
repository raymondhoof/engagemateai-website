"""Filter to KEYRENTER squad calls only (post-cutover) + re-probe Worker
auth + re-fetch live Vapi tool x-vapi-secret values to diagnose."""
import json, os, base64, urllib.request, urllib.error, ssl

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
CV = "c3e98feb-04eb-4e12-afbf-4c39aecbccf1"
CUT = "2026-05-19T15:56:15Z"
ELL = {"6236e418-b4d0-480f-aeb6-c245af67d273": "Router",
       "6892f5e0-8cb0-4362-9bcf-d295df6894d9": "Prospect",
       "8968c13a-b097-4387-b292-f4ffdd88c5cd": "Tenant",
       "8f33b6fd-c1af-4ad5-9728-83916c805b43": "Owner",
       "5d8324fb-2ff5-4a93-b9d9-22e9f02d4a55": "Vendor",
       "afc690ca-cbb3-4cb0-8dad-7cc5aabec5c0": "Uncertain"}


def g(u, h=VH):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=30, context=ctx) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


# --- Worker auth re-probe ---
print("=" * 78)
print("BUG-3 RE-PROBE (live Worker auth right now)")
print("=" * 78)
sec = E["VAPI_WEBHOOK_SECRET"]
W = "https://vapi-backend.misty-dew-89d2.workers.dev/persona-intent"
ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EllieFix/1.0"
for tag, hdr in (("no-secret", {"Content-Type": "application/json", "User-Agent": ua}),
                 ("with-secret(.env)", {"Content-Type": "application/json", "User-Agent": ua,
                                        "x-vapi-secret": sec})):
    r = urllib.request.Request(W, data=b"{}", headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=20, context=ctx) as resp:
            print("  %s -> %s  %s" % (tag, resp.status, resp.read()[:120]))
    except urllib.error.HTTPError as e:
        print("  %s -> %s  %s" % (tag, e.code, e.read()[:120]))

# --- Re-fetch live Vapi tool x-vapi-secret values ---
print("\n" + "=" * 78)
print("Live Vapi tool x-vapi-secret values (Vendor + Uncertain)")
print("=" * 78)
print(".env  secret: %r (hex %s)" % (sec, sec.encode().hex()))
_, b = g("https://api.vapi.ai/tool")
for t in json.loads(b):
    if t.get("type") == "apiRequest" and "misty-dew" in str(t.get("url", "")):
        h = ((t.get("headers") or {}).get("properties") or {}).get("x-vapi-secret") or {}
        v = h.get("value")
        same = v == sec
        print("  tool %s  url=%s" % (t["id"], t.get("url")))
        print("    x-vapi-secret = %r (hex %s)  matches .env=%s"
              % (v, (v.encode().hex() if v else None), same))

# --- Keyrenter-only post-cutover calls ---
print("\n" + "=" * 78)
print("KEYRENTER squad calls AFTER cutover")
print("=" * 78)
_, b = g("https://api.vapi.ai/call?limit=40")
calls = [c for c in json.loads(b)
         if (c.get("createdAt") or "") > CUT and c.get("squadId") == KR]
print("count: %d" % len(calls))
for c in calls:
    cid = c.get("id")
    _, fb = g("https://api.vapi.ai/call/%s" % cid)
    full = json.loads(fb)
    msgs = full.get("messages") or []
    aseen = []
    for m in msgs:
        aid = m.get("assistantId") or (m.get("metadata") or {}).get("assistantId")
        if aid and ELL.get(aid) and ELL[aid] not in aseen:
            aseen.append(ELL[aid])
    tools = []
    for m in msgs:
        if m.get("role") == "tool_calls":
            for tc in (m.get("toolCalls") or []):
                tools.append("CALL " + ((tc.get("function") or {}).get("name") or "?"))
        elif m.get("role") in ("tool_call_result", "tool"):
            res = m.get("result")
            rs = res if isinstance(res, str) else json.dumps(res)
            tools.append("RES  %s -> %s" % (m.get("name"), (rs or "")[:100]))
    print("\n %s | %s | ended=%s" % (c.get("createdAt"), cid, c.get("endedReason")))
    print("   assistants seen: %s" % (aseen or "(none in msg metadata)"))
    print("   tools:")
    for t in tools[:14]:
        print("     " + t)
    # transcript first/last line for persona context
    tr = full.get("transcript", "")
    if tr:
        lines = [l for l in tr.split("\n") if l.strip()]
        print("   first user line:", repr(next((l for l in lines if l.startswith("User:")), "")[:160]))
