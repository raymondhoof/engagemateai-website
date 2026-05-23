"""BUG-3 investigation round 2 — dump failed-call full JSON for any hidden
Vapi-side request metadata, and try corrected CF Workers Observability query."""
import json, os, datetime, urllib.request, urllib.error, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ctx = ssl.create_default_context()
E = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    ln = ln.strip()
    if ln and "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        E[k.strip()] = v.strip()


def req(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    try:
        r = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(r, timeout=30, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as ex:
        return None, "EXC:%s" % ex


# ---- 1. Failed call deep dump ----
print("=" * 78)
print("Failed Uncertain call 019e4781 — top-level keys + any tool-request metadata")
print("=" * 78)
vh = {"Authorization": "Bearer " + E["VAPI_API_KEY"], "User-Agent": "EllieFix/1.0"}
st, b = req("GET", "https://api.vapi.ai/call/019e4781-5be4-7556-9127-a7487f7ec2bf", vh)
call = json.loads(b)
print("top-level keys:", list(call.keys()))
# look for anything that might contain request headers (Vapi sometimes attaches
# tool-execution detail under artifact, phoneCallProviderDetails, monitor, etc.)
for k in ("artifact", "artifactPlan", "monitor", "transport",
          "phoneCallProviderDetails", "toolCalls"):
    if k in call:
        print("\n  has key %r:" % k, json.dumps(call[k])[:400])
# scan messages for any URL/headers/status metadata around the unauthorized result
for i, m in enumerate(call.get("messages") or []):
    if m.get("role") in ("tool_call_result", "tool") and "unauthorized" in str(m.get("result", "")):
        print("\n  unauthorized tool_call_result @ msg[%d] full keys: %s" % (i, list(m.keys())))
        print("    full message:", json.dumps(m)[:800])
        # also show the preceding tool_calls message (which has the toolCalls array)
        if i > 0:
            prev = call["messages"][i - 1]
            print("\n  preceding tool_calls msg keys: %s" % list(prev.keys()))
            print("    preceding:", json.dumps(prev)[:600])
        break

# ---- 2. CF Workers Observability with corrected body shape ----
print("\n" + "=" * 78)
print("CF Workers Observability — query with corrected filter shape")
print("=" * 78)
acct = E["CLOUDFLARE_ACCOUNT_ID"]
H = {"Authorization": "Bearer " + E["CLOUDFLARE_API_TOKEN"], "Content-Type": "application/json",
     "User-Agent": "EllieFix/1.0"}
URL = "https://api.cloudflare.com/client/v4/accounts/%s/workers/observability/telemetry/query" % acct
# window covering the failed calls today
t_from = int(datetime.datetime(2026, 5, 20, 14, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
t_to = int(datetime.datetime(2026, 5, 20, 23, 30, tzinfo=datetime.timezone.utc).timestamp() * 1000)

bodies = [
    # no-filter, just see if any events come back
    {"queryId": "events",
     "timeframe": {"from": t_from, "to": t_to},
     "parameters": {"datasets": ["cloudflare-workers"], "filters": [], "limit": 20}},
    # filter wrapped in kind:"group"
    {"queryId": "events",
     "timeframe": {"from": t_from, "to": t_to},
     "parameters": {"datasets": ["cloudflare-workers"],
                    "filters": [{"kind": "group", "operation": "AND",
                                 "filters": [{"key": "$metadata.service",
                                              "operation": "eq", "value": "vapi-backend"}]}],
                    "limit": 20}},
    # alternative query types
    {"queryId": "calculations",
     "timeframe": {"from": t_from, "to": t_to},
     "parameters": {"datasets": ["cloudflare-workers"], "filters": [], "limit": 5}},
]
for body in bodies:
    st, b = req("POST", URL, H, body)
    print("\n  body=%s ..." % json.dumps(body)[:140])
    print("    -> %s  %s" % (st, b[:400]))
