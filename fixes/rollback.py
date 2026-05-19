"""rollback.py — restore VAPI + Make to the pre-fix state captured in
fixes/_snapshots/. Use if a live push misbehaves.

  python fixes/rollback.py                 # DRY-RUN: show what would be restored
  python fixes/rollback.py --restore --yes  # actually restore (all)
  python fixes/rollback.py --restore --yes --only bug1

BUG-3 has no rollback (apply never mutated it — probe only).
"""
import argparse, json, os, sys, urllib.request, urllib.error, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, "fixes", "_snapshots")
ctx = ssl.create_default_context()
SQUAD_ID = "80ccd39f-f2a6-4035-aeb9-ddb6eff59875"
TOOL_ID = "e008d9d3-96d1-4ef7-ac11-e551b981d786"
ROUTER_ID = "6236e418-b4d0-480f-aeb6-c245af67d273"
SCENARIO_ID = 3442510


def env():
    d = {}
    for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        ln = ln.strip()
        if ln and "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip()
    return d


E = env()
VH = {"Authorization": "Bearer " + E["VAPI_API_KEY"], "Content-Type": "application/json",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EllieFix/1.0"}
MH = {"Authorization": "Token " + E["MAKE_API_TOKEN"], "Content-Type": "application/json",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EllieFix/1.0"}


def J(n):
    return json.load(open(os.path.join(SNAP, n), encoding="utf-8"))


def req(method, url, headers, body):
    r = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "EXC:%s" % e


def r_bug1(dry):
    tool = J("tool_route_caller.before.json")
    squad = J("squad_keyrenter.before.json")
    tbody = {"type": tool.get("type"), "function": tool.get("function"),
             "destinations": tool.get("destinations")}
    sbody = {"members": squad.get("members")}
    if dry:
        print("  DRY restore /tool/%s (function=%s, %d dests) and /squad/%s (%d members, orig assistantDestinations)"
              % (TOOL_ID, "present" if tbody["function"] else "none",
                 len(tbody["destinations"] or []), SQUAD_ID, len(sbody["members"] or [])))
        return
    s1, b1 = req("PATCH", "https://api.vapi.ai/tool/%s" % TOOL_ID, VH, tbody)
    print("  restore tool -> HTTP %s" % s1)
    s2, b2 = req("PATCH", "https://api.vapi.ai/squad/%s" % SQUAD_ID, VH, sbody)
    print("  restore squad -> HTTP %s" % s2)
    if s1 != 200 or s2 != 200:
        print("  !! tool=%s squad=%s — %s %s" % (s1, s2, b1[:150], b2[:150]))


def r_bug2_4_5(dry):
    raw = J("blueprint_3442510.before.json")
    inner = raw["response"]["blueprint"]
    try:
        meta = J("scenario_3442510_meta.before.json")
        sched = (meta.get("scenario") or {}).get("scheduling") or {"type": "immediately"}
    except Exception:
        sched = {"type": "immediately"}
    body = {"blueprint": json.dumps(inner, ensure_ascii=False),
            "scheduling": json.dumps(sched, ensure_ascii=False)}
    if dry:
        print("  DRY restore /scenarios/%d to original blueprint (%d chars), scheduling=%s"
              % (SCENARIO_ID, len(body["blueprint"]), body["scheduling"]))
        return
    s, b = req("PATCH", "https://us2.make.com/api/v2/scenarios/%d" % SCENARIO_ID, MH, body)
    print("  restore scenario -> HTTP %s %s" % (s, "" if s == 200 else b[:300]))


def r_bug6_7(dry):
    a = J("assistant_router.before.json")
    body = {"model": a["model"]}
    if dry:
        print("  DRY restore /assistant/%s model (original system prompt)" % ROUTER_ID)
        return
    s, b = req("PATCH", "https://api.vapi.ai/assistant/%s" % ROUTER_ID, VH, body)
    print("  restore router -> HTTP %s %s" % (s, "" if s == 200 else b[:300]))


HANDLERS = [("bug1", r_bug1), ("bug2_4_5", r_bug2_4_5), ("bug6_7", r_bug6_7)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--yes", action="store_true", help="required with --restore to actually write")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    if not os.path.exists(os.path.join(SNAP, "MANIFEST.json")):
        print("No snapshot to restore from.")
        sys.exit(2)
    dry = not (a.restore and a.yes)
    print("ROLLBACK mode=%s" % ("DRY-RUN" if dry else "RESTORING (live)"))
    if a.restore and not a.yes:
        print("(--restore needs --yes to actually write; showing dry-run)")
    for tag, fn in HANDLERS:
        if a.only and tag not in a.only.split(","):
            continue
        print("\n-- %s --" % tag)
        fn(dry)
    print("\nBUG-3: no rollback (apply never mutated it).")
    if not dry:
        print("Done. Run: python fixes/verify.py --live  (expect artifact checks to now MISMATCH live = restored).")


if __name__ == "__main__":
    main()
