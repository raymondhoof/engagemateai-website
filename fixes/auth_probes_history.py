"""
Fetch and display auth.probe history from the Cloudflare KV namespace.

Usage:
  python fixes/auth_probes_history.py               # all probes
  python fixes/auth_probes_history.py --fails-only  # mismatches only
  python fixes/auth_probes_history.py --last 20     # last N entries
"""
import argparse
import json
import os
import sys
import urllib.request

ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "7b9263fa350986bedc464134a391b451")
KV_NS_ID   = os.environ.get("AUTH_PROBES_KV_NS_ID",  "0955d0a3276248bc9322039010e45929")
CF_TOKEN   = os.environ.get("CLOUDFLARE_API_TOKEN", "")

if not CF_TOKEN:
    # Try loading from .env in repo root
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("CLOUDFLARE_API_TOKEN="):
                    CF_TOKEN = line.split("=", 1)[1]
                    break
    if not CF_TOKEN:
        print("ERROR: CLOUDFLARE_API_TOKEN not set. Set env var or add to .env", file=sys.stderr)
        sys.exit(1)

BASE = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/storage/kv/namespaces/{KV_NS_ID}"
HEADERS = {"Authorization": f"Bearer {CF_TOKEN}"}


def cf_get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def cf_get_raw(path: str) -> str:
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    with urllib.request.urlopen(req) as r:
        return r.read().decode()


def list_keys(prefix="probe:") -> list[str]:
    keys = []
    cursor = None
    while True:
        qs = f"?limit=1000&prefix={prefix}"
        if cursor:
            qs += f"&cursor={cursor}"
        data = cf_get(f"/keys{qs}")
        keys += [k["name"] for k in data.get("result", [])]
        cursor = data.get("result_info", {}).get("cursor")
        if not cursor:
            break
    return sorted(keys)


def fetch_record(key: str) -> dict:
    from urllib.parse import quote
    raw = cf_get_raw(f"/values/{quote(key, safe='')}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def print_record(rec: dict, key: str) -> None:
    match = rec.get("match", True)
    flag = "✓" if match else "✗"
    print(f"\n{flag} {rec.get('ts', key)}")
    print(f"  ip={rec.get('ip')}  ua={rec.get('ua', '')[:60]}")
    print(f"  sent={rec.get('sentFp')}  expected={rec.get('expectedFp')}")
    print(f"  hasHeader={rec.get('hasHeader')}  contentType={rec.get('contentType')}")
    if "allHeadersRedacted" in rec:
        hdr = rec["allHeadersRedacted"]
        print(f"  headers: {json.dumps(hdr)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fails-only", action="store_true", help="Show mismatches only")
    parser.add_argument("--last", type=int, default=0, help="Show last N records")
    args = parser.parse_args()

    print("Fetching probe keys from KV…")
    keys = list_keys("probe:")
    if not keys:
        print("No probe records found.")
        return

    if args.last:
        keys = keys[-args.last:]

    total = len(keys)
    shown = 0
    fails = 0

    for key in keys:
        rec = fetch_record(key)
        match = rec.get("match", True)
        if not match:
            fails += 1
        if args.fails_only and match:
            continue
        print_record(rec, key)
        shown += 1

    print(f"\n--- {total} total probes | {fails} mismatches | {shown} shown ---")


if __name__ == "__main__":
    main()
