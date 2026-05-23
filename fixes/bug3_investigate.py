"""BUG-3 investigation — controlled probes of the live Worker to identify
exactly what secret-string mutation produces the call-time 401. Read-only.
Empty body -> auth-only test (no GHL/SMS side effects)."""
import json, os, urllib.request, urllib.error, urllib.parse, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ctx = ssl.create_default_context()
E = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    ln = ln.strip()
    if ln and "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        E[k.strip()] = v.strip()

W = "https://vapi-backend.misty-dew-89d2.workers.dev/persona-intent"
UA = "Mozilla/5.0 EllieFix/1.0"
real = E["VAPI_WEBHOOK_SECRET"]            # "<REDACTED-OLD-SECRET-19c>%"


def probe(label, secret_value):
    h = {"Content-Type": "application/json", "User-Agent": UA, "x-vapi-secret": secret_value}
    try:
        r = urllib.request.Request(W, data=b"{}", headers=h, method="POST")
        with urllib.request.urlopen(r, timeout=20, context=ctx) as resp:
            body = resp.read().decode("utf-8", "replace")[:80]
            print("  %-45s -> %s  %s" % (label, resp.status, body))
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:80]
        print("  %-45s -> %s  %s" % (label, e.code, body))
        return e.code, body
    except Exception as e:
        print("  %-45s -> EXC %s" % (label, e))
        return None, str(e)


print("Worker secret-string variant probes (auth-only, no side effects)")
print("real .env secret = %r (hex %s)" % (real, real.encode().hex()))
print()

# Variants designed to expose how Vapi/CF might mangle the trailing %
variants = [
    ("baseline (raw, what .env has)",     real),                               # <REDACTED-OLD-SECRET-19c>%
    ("trailing % URL-encoded -> %25",     real[:-1] + "%25"),                 # <REDACTED-OLD-SECRET-19c>%25
    ("trailing % stripped",               real[:-1]),                          # <REDACTED-OLD-SECRET-19c>
    ("trailing % doubled",                real + "%"),                         # <REDACTED-OLD-SECRET-19c>%%
    ("trailing % double-encoded",         real[:-1] + "%2525"),                # <REDACTED-OLD-SECRET-19c>%2525
    ("trailing % replaced with space",    real[:-1] + " "),                    # <REDACTED-OLD-SECRET-19c><sp>
    ("trailing whitespace appended",      real + " "),                         # <REDACTED-OLD-SECRET-19c>% <sp>
    ("full string URL-encoded once",      urllib.parse.quote(real, safe="")),  # <REDACTED-OLD-SECRET-19c>%25
    ("trailing CR appended",              real + "\r"),                        # mangling marker
    ("control: deliberately wrong",       "obviously-wrong-value"),
]
for label, val in variants:
    probe(label, val)
