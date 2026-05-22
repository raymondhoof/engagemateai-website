"""Post-deploy real-traffic check: did the failure signatures disappear for
inbound calls AFTER the cutover? Read-only."""
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
MH = {"Authorization": "Token " + E["MAKE_API_TOKEN"], "User-Agent": "Mozilla/5.0 EllieFix/1.0"}
CUT = "2026-05-19T15:56:15Z"
ELL = {"6236e418-b4d0-480f-aeb6-c245af67d273": "Router",
       "6892f5e0-8cb0-4362-9bcf-d295df6894d9": "Prospect",
       "8968c13a-b097-4387-b292-f4ffdd88c5cd": "Tenant",
       "8f33b6fd-c1af-4ad5-9728-83916c805b43": "Owner",
       "5d8324fb-2ff5-4a93-b9d9-22e9f02d4a55": "Vendor",
       "afc690ca-cbb3-4cb0-8dad-7cc5aabec5c0": "Uncertain"}


def g(u, h):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=30, context=ctx) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


st, b = g("https://api.vapi.ai/call?limit=30", VH)
calls = json.loads(b) if st == 200 else []
post = [c for c in calls if (c.get("createdAt") or "") > CUT]
print("Vapi: %d recent calls; %d created AFTER cutover %s" % (len(calls), len(post), CUT))
for c in post:
    cid = c.get("id")
    st2, fb = g("https://api.vapi.ai/call/%s" % cid, VH)
    full = json.loads(fb) if st2 == 200 else {}
    msgs = full.get("messages") or []
    badhandoff = any(m.get("role") in ("tool_call_result", "tool") and
                     "No handoff destination returned" in str(m.get("result", "")) for m in msgs)
    badjson = any(m.get("role") in ("tool_call_result", "tool") and
                  ("invalid json response body" in str(m.get("result", "")) or
                   '"Accepted"' in str(m.get("result", ""))) for m in msgs)
    handoffs = [ (m.get("toolCalls") or [{}])[0].get("function", {}).get("name")
                 for m in msgs if m.get("role") == "tool_calls"]
    print("\n %s | %s | asst=%s | ended=%s" %
          (c.get("createdAt"), cid, ELL.get(c.get("assistantId"), c.get("assistantId")), c.get("endedReason")))
    print("   bad-handoff-signature: %s | bad-json-signature: %s" % (badhandoff, badjson))

st, b = g("https://us2.make.com/api/v2/scenarios/3442510/logs?pg[limit]=12", MH)
if st == 200:
    rows = json.loads(b).get("scenarioLogs", [])
    pl = [r for r in rows if (r.get("timestamp") or "") > CUT]
    print("\nMake 3442510: %d execs after cutover" % len(pl))
    for r in pl[:10]:
        print("  ", r.get("timestamp"), "status=", r.get("status"), "ops=", r.get("operations"))
    if not pl and rows:
        print("  (none post-cutover; most recent exec %s status %s)"
              % (rows[0].get("timestamp"), rows[0].get("status")))
