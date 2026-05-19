"""snapshot.py — capture the LIVE pre-change state of every system this PR
touches, into fixes/_snapshots/. This is the rollback baseline. apply.py
refuses to push unless a fresh snapshot exists.

Read-only. Run before anything else.
  python fixes/snapshot.py
"""
import json, os, sys, time, urllib.request, urllib.error, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, "fixes", "_snapshots")
os.makedirs(SNAP, exist_ok=True)
ctx = ssl.create_default_context()

SQUAD_ID = "80ccd39f-f2a6-4035-aeb9-ddb6eff59875"
ROUTE_CALLER_TOOL_ID = "e008d9d3-96d1-4ef7-ac11-e551b981d786"
ROUTER_ASSISTANT_ID = "6236e418-b4d0-480f-aeb6-c245af67d273"
MAKE_SCENARIO_ID = 3442510


def env():
    d = {}
    for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        ln = ln.strip()
        if ln and "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip()
    return d


E = env()


def get(url, headers):
    r = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(r, timeout=40, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "EXC:%s" % e


def save(name, content):
    p = os.path.join(SNAP, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    print("  saved %s (%d bytes)" % (name, len(content)))


def main():
    vh = {"Authorization": "Bearer " + E["VAPI_API_KEY"], "User-Agent": "EllieFix/1.0"}
    mh = {"Authorization": "Token " + E["MAKE_API_TOKEN"], "User-Agent": "Mozilla/5.0 EllieFix/1.0"}
    stamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    manifest = {"captured_at": stamp, "items": {}}

    targets = [
        ("squad_keyrenter.before.json", "https://api.vapi.ai/squad/%s" % SQUAD_ID, vh),
        ("tool_route_caller.before.json", "https://api.vapi.ai/tool/%s" % ROUTE_CALLER_TOOL_ID, vh),
        ("assistant_router.before.json", "https://api.vapi.ai/assistant/%s" % ROUTER_ASSISTANT_ID, vh),
        ("blueprint_3442510.before.json",
         "https://us2.make.com/api/v2/scenarios/%d/blueprint" % MAKE_SCENARIO_ID, mh),
        ("scenario_3442510_meta.before.json",
         "https://us2.make.com/api/v2/scenarios/%d" % MAKE_SCENARIO_ID, mh),
    ]
    ok = True
    print("Capturing live pre-change snapshot @ %s" % stamp)
    for name, url, h in targets:
        st, body = get(url, h)
        if st != 200:
            print("  !! %s -> HTTP %s (NOT saved)" % (name, st))
            ok = False
            continue
        # validate JSON parses before trusting it as a rollback point
        try:
            json.loads(body)
        except Exception as ex:
            print("  !! %s -> invalid JSON (%s) NOT saved" % (name, ex))
            ok = False
            continue
        save(name, body)
        manifest["items"][name] = {"url": url, "http": st, "bytes": len(body)}

    save("MANIFEST.json", json.dumps(manifest, indent=2))
    if not ok:
        print("\nSNAPSHOT INCOMPLETE — do not proceed to apply. Investigate the !! lines.")
        sys.exit(1)
    print("\nSnapshot complete. Rollback baseline is fixes/_snapshots/ (%s)." % stamp)


if __name__ == "__main__":
    main()
