"""Fetch CF Workers Observability events around the failed-call timestamps,
to confirm the Worker received the requests and what it logged."""
import json, os, datetime, urllib.request, urllib.error, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ctx = ssl.create_default_context()
E = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    ln = ln.strip()
    if ln and "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        E[k.strip()] = v.strip()
acct = E["CLOUDFLARE_ACCOUNT_ID"]
H = {"Authorization": "Bearer " + E["CLOUDFLARE_API_TOKEN"], "Content-Type": "application/json",
     "User-Agent": "EllieFix/1.0"}
URL = "https://api.cloudflare.com/client/v4/accounts/%s/workers/observability/telemetry/query" % acct


def post(body):
    r = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=H, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=40, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "EXC:%s" % e


# narrow window around the 22:28 failure (call 019e4781) — 22:25..22:35Z today
f1 = int(datetime.datetime(2026, 5, 20, 22, 25, tzinfo=datetime.timezone.utc).timestamp() * 1000)
t1 = int(datetime.datetime(2026, 5, 20, 22, 35, tzinfo=datetime.timezone.utc).timestamp() * 1000)
print("Window: 2026-05-20T22:25..22:35Z   ms=%d..%d" % (f1, t1))

st, b = post({
    "queryId": "events",
    "timeframe": {"from": f1, "to": t1},
    "parameters": {"datasets": ["cloudflare-workers"], "filters": [], "limit": 50},
})
print("HTTP %s" % st)
if st != 200:
    print(b[:500]); raise SystemExit
d = json.loads(b)
events = (d.get("result") or {}).get("events") or (d.get("result") or {}).get("runs") or []
# inspect the result structure
print("result keys:", list((d.get("result") or {}).keys()))
print("first 1000 chars of result:", json.dumps(d.get("result"))[:1000])

# also try the "run" pointer-style: poll the run's events endpoint
runid = ((d.get("result") or {}).get("run") or {}).get("id")
print("\nrun.id =", runid)
if runid:
    # try the standard run-results endpoint
    candidates = [
        "https://api.cloudflare.com/client/v4/accounts/%s/workers/observability/telemetry/query/%s/events" % (acct, runid),
        "https://api.cloudflare.com/client/v4/accounts/%s/workers/observability/telemetry/runs/%s" % (acct, runid),
        "https://api.cloudflare.com/client/v4/accounts/%s/workers/observability/telemetry/runs/%s/events" % (acct, runid),
    ]
    for u in candidates:
        try:
            r = urllib.request.Request(u, headers=H)
            with urllib.request.urlopen(r, timeout=30, context=ctx) as resp:
                print("\nGET %s -> %s" % (u.split("/v4")[1], resp.status))
                print(resp.read()[:1500])
                break
        except urllib.error.HTTPError as e:
            print("\nGET %s -> %s  %s" % (u.split("/v4")[1], e.code, e.read()[:200]))
