"""Check for: (a) Vapi /call recovery + 3 new calls, (b) Airtable Call Logs
records from Debbie's recent email-fix, (c) Debbie Email Fixes scenario
recent execs."""
import json, os, urllib.request, urllib.error, ssl, urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ctx = ssl.create_default_context()
E = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    ln = ln.strip()
    if ln and "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        E[k.strip()] = v.strip()


def g(u, h, t=60):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=t, context=ctx) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as ex:
        return None, str(ex)


# 1. Vapi /call retry
print("== Vapi /call retry ==")
vh = {"Authorization": "Bearer " + E["VAPI_API_KEY"], "User-Agent": "EllieFix/1.0"}
st, b = g("https://api.vapi.ai/call?limit=10", vh, 45)
print("  /call?limit=10 -> %s (%d bytes)" % (st, len(b)))
if st == 200:
    cs = json.loads(b)
    KR = "80ccd39f-f2a6-4035-aeb9-ddb6eff59875"
    CUT = "2026-05-20T23:00:00Z"   # after the previous 5 we'd seen (last was 22:28)
    new_kr = [c for c in cs if (c.get("createdAt") or "") > CUT and c.get("squadId") == KR]
    print("  Keyrenter calls after 2026-05-20T23:00Z: %d" % len(new_kr))
    for c in new_kr:
        print("   ", c.get("createdAt"), c.get("id"), "ended=", c.get("endedReason"))

# 2. Airtable Call Logs — recent + any Debbie-derived "New"/"Approved" records
print("\n== Airtable Call Logs — recent (sorted by Last Modified) ==")
ah = {"Authorization": "Bearer " + E["AIRTABLE_API_KEY"], "User-Agent": "EllieFix/1.0"}
base = E["AIRTABLE_BASE_ID"]
table = urllib.parse.quote(E["AIRTABLE_TABLE_NAME"])
# Newest 8, all fields
url = ("https://api.airtable.com/v0/%s/%s?pageSize=8"
       "&sort%%5B0%%5D%%5Bfield%%5D=Last%%20Modified&sort%%5B0%%5D%%5Bdirection%%5D=desc") % (base, table)
st, b = g(url, ah, 30)
if st == 200:
    recs = json.loads(b).get("records", [])
    print("  %d recent records" % len(recs))
    for r in recs:
        f = r.get("fields", {})
        # show concise summary
        print("\n  rec %s   modified=%s" % (r["id"], r.get("createdTime", "?")))
        for k in ("Status", "Call ID", "Email ID", "Assistant ID",
                  "Rationale", "Proposed Fix", "Sender", "Subject", "Body"):
            v = f.get(k)
            if v:
                s = (v if isinstance(v, str) else json.dumps(v))[:200]
                print("    %s: %s" % (k, s))
else:
    print("  airtable err:", b[:200])

# 3. Debbie Email Fixes scenario recent execs
print("\n== Make 'Debbie Email Fixes' (4744250) recent execs ==")
mh = {"Authorization": "Token " + E["MAKE_API_TOKEN"], "User-Agent": "Mozilla/5.0 EllieFix/1.0"}
st, b = g("https://us2.make.com/api/v2/scenarios/4744250/logs?pg[limit]=6", mh, 30)
if st == 200:
    for row in json.loads(b).get("scenarioLogs", []):
        print("  ", row.get("timestamp"), "status=", row.get("status"), "ops=", row.get("operations"))

# 4. Vapi Agentic Loop (4782279) — the autopilot loop that turns email -> Airtable record
print("\n== Make 'Vapi Agentic Loop' (4782279) recent execs ==")
st, b = g("https://us2.make.com/api/v2/scenarios/4782279/logs?pg[limit]=6", mh, 30)
if st == 200:
    for row in json.loads(b).get("scenarioLogs", []):
        print("  ", row.get("timestamp"), "status=", row.get("status"), "ops=", row.get("operations"))
