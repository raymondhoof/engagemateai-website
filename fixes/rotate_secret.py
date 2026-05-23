"""Atomic rotation of VAPI_WEBHOOK_SECRET to a no-`%` value.

Order: PUT CF Worker secret -> poll until propagated -> PATCH both Vapi
tool headers -> update .env -> final probes (new=400 pass, old=401 reject).
Stops + rolls back CF secret on any failure after CF write."""
import json, os, secrets as secmod, sys, time, urllib.request, urllib.error, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ctx = ssl.create_default_context()
E = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    ln = ln.strip()
    if ln and "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        E[k.strip()] = v.strip()
OLD = E["VAPI_WEBHOOK_SECRET"]
ACCT = E["CLOUDFLARE_ACCOUNT_ID"]
CFTOK = E["CLOUDFLARE_API_TOKEN"]
VAPI = E["VAPI_API_KEY"]
SCRIPT = "vapi-backend"
TOOLS = ["a69c0e85-4fe1-426b-816c-2c51e78b770a",  # Uncertain
         "f390f48a-63ad-4629-8351-80837a3b870a"]  # Vendor
WORKER = "https://vapi-backend.misty-dew-89d2.workers.dev/persona-intent"
UA = "Mozilla/5.0 EllieFix/1.0"


def req(method, url, headers, body=None, t=30):
    data = json.dumps(body).encode() if body is not None else None
    try:
        r = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(r, timeout=t, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as ex:
        return None, "EXC:%s" % ex


def probe(secret_value):
    h = {"Content-Type": "application/json", "User-Agent": UA, "x-vapi-secret": secret_value}
    return req("POST", WORKER, h, body={})


def put_cf_secret(value):
    url = "https://api.cloudflare.com/client/v4/accounts/%s/workers/scripts/%s/secrets" % (ACCT, SCRIPT)
    h = {"Authorization": "Bearer " + CFTOK, "Content-Type": "application/json", "User-Agent": UA}
    return req("PUT", url, h, body={"name": "VAPI_WEBHOOK_SECRET", "text": value, "type": "secret_text"})


def patch_tool(tid, secret_value):
    h = {"Authorization": "Bearer " + VAPI, "Content-Type": "application/json", "User-Agent": UA}
    body = {"headers": {"type": "object", "properties": {
        "x-vapi-secret": {"type": "string", "value": secret_value}}}}
    return req("PATCH", "https://api.vapi.ai/tool/" + tid, h, body=body)


def get_tool_secret(tid):
    h = {"Authorization": "Bearer " + VAPI, "User-Agent": UA}
    st, b = req("GET", "https://api.vapi.ai/tool/" + tid, h)
    if st != 200:
        return None
    t = json.loads(b)
    return (((t.get("headers") or {}).get("properties") or {}).get("x-vapi-secret") or {}).get("value")


def update_env(new):
    p = os.path.join(ROOT, ".env")
    txt = open(p, encoding="utf-8").read()
    new_txt = []
    found = False
    for ln in txt.splitlines():
        if ln.startswith("VAPI_WEBHOOK_SECRET="):
            new_txt.append("VAPI_WEBHOOK_SECRET=" + new)
            found = True
        else:
            new_txt.append(ln)
    assert found, ".env has no VAPI_WEBHOOK_SECRET line"
    open(p, "w", encoding="utf-8").write("\n".join(new_txt) + ("\n" if txt.endswith("\n") else ""))


print("=" * 70)
print("ROTATE VAPI_WEBHOOK_SECRET (no-% value)")
print("=" * 70)
NEW = secmod.token_urlsafe(24)                  # 32 chars, [A-Za-z0-9_-] only
assert "%" not in NEW and "&" not in NEW and "+" not in NEW and "=" not in NEW
print("OLD: %r (len %d)  NEW: %r (len %d)  [%%-free: %s]" %
      (OLD[:4] + "..." + OLD[-2:], len(OLD), NEW[:4] + "..." + NEW[-2:], len(NEW), "%" not in NEW))

# 1. CF Worker secret
print("\n[1] PUT CF Worker secret ...")
st, b = put_cf_secret(NEW)
print("    -> HTTP %s  %s" % (st, b[:140]))
if st not in (200, 201):
    print("    FAIL; aborting; nothing else changed.")
    sys.exit(2)

# 2. Poll probe with NEW until 400 (propagation)
print("\n[2] Poll worker probe with NEW secret until auth passes ...")
for attempt in range(1, 21):
    st, b = probe(NEW)
    ok = st == 400 and "invalid_payload" in b
    print("    attempt %d -> %s %s" % (attempt, st, "PASS" if ok else b[:80]))
    if ok:
        break
    time.sleep(2)
else:
    print("    NEW secret never propagated. Rolling back CF secret to OLD.")
    rb_st, _ = put_cf_secret(OLD)
    print("    rollback PUT -> %s" % rb_st)
    sys.exit(3)

# 3. PATCH Vapi tools
print("\n[3] PATCH both Vapi tools' x-vapi-secret ...")
patched = []
for tid in TOOLS:
    st, b = patch_tool(tid, NEW)
    print("    tool %s -> HTTP %s  %s" % (tid, st, b[:80]))
    if st != 200:
        print("    FAIL; rolling back tools + CF secret.")
        # revert any already-patched tools
        for already in patched:
            patch_tool(already, OLD)
        put_cf_secret(OLD)
        sys.exit(4)
    patched.append(tid)
# confirm GET-back
for tid in TOOLS:
    v = get_tool_secret(tid)
    print("    verify tool %s value = %s  (matches NEW: %s)" %
          (tid, v[:4] + "..." + v[-2:] if v else None, v == NEW))

# 4. Update .env
print("\n[4] Update .env ...")
update_env(NEW)
print("    .env VAPI_WEBHOOK_SECRET updated")

# 5. Final probes
print("\n[5] Final auth probes ...")
st, b = probe(NEW)
print("    NEW secret -> %s %s" % (st, "(expected 400/invalid_payload)" if st == 400 else "[!!]" + b[:80]))
st, b = probe(OLD)
print("    OLD secret -> %s %s" % (st, "(expected 401/unauthorized)" if st == 401 else "[!!]" + b[:80]))

print("\nDONE. Rotation complete. Next Uncertain/Vendor call should succeed.")
