"""Try to fetch Cloudflare Worker logs around the failed-call timestamps.
[observability] is enabled in wrangler.toml, so logs should be queryable.
Read-only — probes a few candidate CF API endpoints."""
import json, os, urllib.request, urllib.error, ssl, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ctx = ssl.create_default_context()
E = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    ln = ln.strip()
    if ln and "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        E[k.strip()] = v.strip()
ACCT = E["CLOUDFLARE_ACCOUNT_ID"]
TOK = E["CLOUDFLARE_API_TOKEN"]
H = {"Authorization": "Bearer " + TOK, "Content-Type": "application/json",
     "User-Agent": "EllieFix/1.0"}


def req(method, url, body=None):
    data = json.dumps(body).encode() if body else None
    try:
        r = urllib.request.Request(url, data=data, headers=H, method=method)
        with urllib.request.urlopen(r, timeout=30, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "EXC:%s" % e


# Sanity: token validity + script existence
print("== sanity ==")
st, b = req("GET", "https://api.cloudflare.com/client/v4/user/tokens/verify")
print("  /user/tokens/verify -> %s  %s" % (st, b[:160]))
st, b = req("GET", f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/workers/scripts/vapi-backend")
print("  /scripts/vapi-backend -> %s  %s" % (st, b[:160]))

# Workers Observability — try plausible endpoint shapes
print("\n== Workers Observability — discovery ==")
candidates = [
    ("GET",  f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/workers/observability/telemetry/keys", None),
    ("GET",  f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/workers/observability/telemetry/values", None),
    ("POST", f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/workers/observability/telemetry/query",
        {"queryId": "events", "timeframe": {"from": int((datetime.datetime(2026, 5, 20, 21, 0, tzinfo=datetime.timezone.utc)).timestamp() * 1000),
                                            "to":   int((datetime.datetime(2026, 5, 20, 23, 30, tzinfo=datetime.timezone.utc)).timestamp() * 1000)},
         "parameters": {"datasets": ["cloudflare-workers"], "filters": [{"key": "$metadata.service", "operation": "eq", "value": "vapi-backend"}], "limit": 50}}),
    ("GET",  f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/workers/observability", None),
    ("GET",  f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/logpush/jobs", None),
]
for method, url, body in candidates:
    st, b = req(method, url, body)
    print("  %s %s -> %s  %s" % (method, url.split("/v4")[1], st, b[:200].replace("\n", " ")))
