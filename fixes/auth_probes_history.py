"""Read the Worker's auth.probe KV history (BUG-3 diagnosis).

Each /persona-intent request writes a probe record to KV namespace
VAPI_AUTH_PROBES with key probe:<ISO ts>:<rand>. Records expire after 14 days.

  python fixes/auth_probes_history.py                # recent 20 summarized
  python fixes/auth_probes_history.py --all          # full history
  python fixes/auth_probes_history.py --since 2026-05-22T15:00:00Z
  python fixes/auth_probes_history.py --fails-only   # only mismatches / missing-header
"""
import argparse, json, os, sys, urllib.request, urllib.error, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ctx = ssl.create_default_context()
NS = "0955d0a3276248bc9322039010e45929"


def env():
    d = {}
    for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        ln = ln.strip()
        if ln and "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def req(url):
    e = env()
    h = {"Authorization": "Bearer " + e["CLOUDFLARE_API_TOKEN"], "User-Agent": "EllieFix/1.0"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=40, context=ctx) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e2:
        return e2.code, e2.read().decode("utf-8", "replace")


def list_keys(prefix="probe:", limit=1000):
    e = env(); acct = e["CLOUDFLARE_ACCOUNT_ID"]
    all_keys = []
    cursor = None
    while True:
        url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/storage/kv/namespaces/{NS}/keys?prefix={prefix}&limit=1000"
        if cursor:
            url += f"&cursor={cursor}"
        st, b = req(url)
        if st != 200:
            print(f"list err {st}: {b[:200]}"); sys.exit(1)
        d = json.loads(b)
        all_keys.extend(d.get("result", []))
        cursor = (d.get("result_info") or {}).get("cursor")
        if not cursor or len(all_keys) >= limit:
            break
    return all_keys[:limit]


def get_value(key):
    e = env(); acct = e["CLOUDFLARE_ACCOUNT_ID"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/storage/kv/namespaces/{NS}/values/{key}"
    st, b = req(url)
    if st != 200:
        return None
    try:
        return json.loads(b)
    except Exception:
        return {"_raw": b[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--since", help="ISO timestamp, e.g. 2026-05-22T15:00:00Z")
    ap.add_argument("--fails-only", action="store_true", help="only show records where match=false")
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()

    keys = list_keys(limit=1000 if (a.all or a.since) else max(a.limit, 50))
    keys.sort(key=lambda k: k["name"], reverse=True)  # newest first

    records = []
    for k in keys:
        ts = k["name"].split(":", 1)[1].rsplit(":", 1)[0]
        if a.since and ts < a.since:
            continue
        v = get_value(k["name"])
        if v is None:
            continue
        if a.fails_only and v.get("match"):
            continue
        records.append((k["name"], v))
        if not a.all and len(records) >= a.limit:
            break

    print(f"\n{len(records)} probe(s) (KV total keys={len(keys)})\n")
    fails = sum(1 for _, v in records if not v.get("match"))
    missing = sum(1 for _, v in records if not v.get("hasHeader"))
    print(f"  summary: {fails} mismatched  /  {missing} missing-header  /  {len(records) - fails} matched\n")
    print(f"  {'ts':<30} {'hasHdr':<7} {'match':<6} {'sentFp':<18} {'expectedFp':<18} ip / ua")
    for key, v in records:
        ip = (v.get("ip") or "")[:39]
        ua = (v.get("ua") or "")[:40]
        print(f"  {v.get('ts',''):<30} {str(v.get('hasHeader','?')):<7} {str(v.get('match','?')):<6} {v.get('sentFp',''):<18} {v.get('expectedFp',''):<18} {ip}  {ua}")


if __name__ == "__main__":
    main()
