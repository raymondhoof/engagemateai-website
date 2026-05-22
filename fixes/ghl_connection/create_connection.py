"""Create (or reuse) a Make Connection holding the GHL Private Integration
Token, so we can replace the inline `Authorization: Bearer pit-...` in 19
modules of scenario 3442510 with `__IMTCONN__: <id>`. Read-write at Make,
but idempotent + safe.

  python fixes/ghl_connection/create_connection.py             # auto: GET first; create only if absent
  python fixes/ghl_connection/create_connection.py --conn-id N # accept a manual UI-created id
"""
import argparse, json, os, sys, urllib.request, urllib.error, ssl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
ART = os.path.join(HERE, "_artifacts")
os.makedirs(ART, exist_ok=True)
ctx = ssl.create_default_context()

E = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    ln = ln.strip()
    if ln and "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        E[k.strip()] = v.strip()

ACCOUNT_NAME = "GHL Keyrenter PIT"
TEAM = E["MAKE_TEAM_ID"]
ZONE = E.get("MAKE_ZONE", "us2.make.com")
H = {"Authorization": "Token " + E["MAKE_API_TOKEN"], "Content-Type": "application/json",
     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EllieFix/1.0"}


def req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    try:
        r = urllib.request.Request(url, data=data, headers=H, method=method)
        with urllib.request.urlopen(r, timeout=40, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as ex:
        return None, "EXC:%s" % ex


def save_artifact(conn):
    with open(os.path.join(ART, "connection.json"), "w", encoding="utf-8") as f:
        json.dump(conn, f, indent=2)
    print("  -> _artifacts/connection.json (id=%s, accountType=%s)" %
          (conn.get("id"), conn.get("accountType") or conn.get("packageName")))


def find_existing():
    st, b = req("GET", f"https://{ZONE}/api/v2/connections?teamId={TEAM}")
    if st != 200:
        return None, ("list err %s %s" % (st, b[:200]))
    data = json.loads(b)
    conns = data.get("connections") or data.get("results") or []
    for c in conns:
        if c.get("accountName") == ACCOUNT_NAME or c.get("name") == ACCOUNT_NAME:
            return c, None
    return None, "no existing %r" % ACCOUNT_NAME


def try_create(account_type):
    body = {
        "accountName": ACCOUNT_NAME,
        "accountType": account_type,
        "teamId": int(TEAM),
        "data": {"token": E["GHL_PRIVATE_TOKEN"]},
    }
    return req("POST", f"https://{ZONE}/api/v2/connections?teamId={TEAM}", body=body)


def fetch_by_id(cid):
    st, b = req("GET", f"https://{ZONE}/api/v2/connections/{cid}")
    if st != 200:
        return None, "fetch err %s %s" % (st, b[:200])
    d = json.loads(b)
    return d.get("connection") or d, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn-id", type=int, default=None,
                    help="accept a manually-created connection id (UI fallback)")
    a = ap.parse_args()

    # manual id path
    if a.conn_id is not None:
        conn, err = fetch_by_id(a.conn_id)
        if err:
            print("manual --conn-id %d: %s" % (a.conn_id, err)); sys.exit(2)
        print("Using manual connection id=%d (%s)" % (a.conn_id, conn.get("accountName")))
        save_artifact(conn); return

    # idempotent: existing?
    print("Checking team %s for existing connection %r ..." % (TEAM, ACCOUNT_NAME))
    existing, miss = find_existing()
    if existing:
        print("Found existing -> id=%s. Reusing (idempotent)." % existing.get("id"))
        save_artifact(existing); return
    print(" ", miss)

    print("Creating connection via POST (try accountType variants)...")
    for at in ("http:bearer", "http2:bearer", "http", "http2"):
        st, b = try_create(at)
        print("  accountType=%r -> HTTP %s  %s" % (at, st, b[:160]))
        if st in (200, 201):
            d = json.loads(b)
            conn = d.get("connection") or d
            print("CREATED. id=%s accountType=%s" % (conn.get("id"), conn.get("accountType")))
            save_artifact(conn); return
        # only continue to next variant on enum/type errors
        if "accountType" not in b and "type" not in b.lower():
            print("  non-enum error; not trying other variants.")
            break

    print("\nAll programmatic attempts failed. UI fallback:")
    print("  1. Make UI -> your team -> Connections -> Create connection")
    print("  2. Choose 'HTTP' (or 'HTTP - Make a request')")
    print("  3. Connection type: 'API Key Auth' / 'Authorization' / 'Bearer Token'")
    print("  4. Token: paste $GHL_PRIVATE_TOKEN from .env (starts pit-...)")
    print("  5. Save. Note the connection id from the URL.")
    print("  6. Re-run: python fixes/ghl_connection/create_connection.py --conn-id <N>")
    sys.exit(3)


if __name__ == "__main__":
    main()
