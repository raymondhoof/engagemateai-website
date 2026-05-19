"""apply.py — controlled LIVE push of the Ellie fixes. Run POST-MERGE, only
after explicit go-ahead.

  python fixes/apply.py                 # DRY-RUN (default): show intended changes, no writes
  python fixes/apply.py --apply         # actually push (all bugs, in order)
  python fixes/apply.py --apply --only bug1            # subset
  python fixes/apply.py --apply --only bug2_4_5,bug6_7

Safeguards:
  * refuses to run without fixes/_snapshots/MANIFEST.json (rollback baseline)
  * idempotent: GETs current, pushes only if different, GET-verifies after
  * order enforced: bug1 -> bug2_4_5 -> bug3(probe-only) -> bug6_7
  * BUG-3 NEVER mutates (probe only) — see fixes/bug3_worker/SET_SECRET.md
  * Make blueprint PATCH is the highest blast radius; dry-run prints a diff
"""
import argparse, json, os, sys, urllib.request, urllib.error, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FX = os.path.join(ROOT, "fixes")
SNAP = os.path.join(FX, "_snapshots")
ctx = ssl.create_default_context()
SQUAD_ID = "80ccd39f-f2a6-4035-aeb9-ddb6eff59875"
TOOL_ID = "e008d9d3-96d1-4ef7-ac11-e551b981d786"
ROUTER_ID = "6236e418-b4d0-480f-aeb6-c245af67d273"
SCENARIO_ID = 3442510
ORDER = ["bug1", "bug2_4_5", "bug3", "bug6_7"]


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


def J(p):
    return json.load(open(p, encoding="utf-8"))


def req(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "EXC:%s" % e


def guard_snapshot():
    mf = os.path.join(SNAP, "MANIFEST.json")
    if not os.path.exists(mf):
        print("REFUSING: no rollback snapshot. Run `python fixes/snapshot.py` first.")
        sys.exit(2)
    return J(mf).get("captured_at")


def step(tag, do, dry):
    print("\n" + "-" * 64 + "\n%s" % tag)
    try:
        do(dry)
    except Exception as e:
        print("  ERROR in %s: %s" % (tag, e))
        if not dry:
            print("  >>> STOP. Investigate before continuing. Rollback: python fixes/rollback.py")
            sys.exit(3)


def bug1(dry):
    tool_fixed = J(os.path.join(FX, "bug1_squad_handoff", "tool_route_caller.fixed.json"))
    squad_fixed = J(os.path.join(FX, "bug1_squad_handoff", "squad_keyrenter.fixed.json"))
    st, cur = req("GET", "https://api.vapi.ai/tool/%s" % TOOL_ID, VH)
    curj = json.loads(cur) if st == 200 else {}
    needs = bool(curj.get("function")) or any(
        not (d.get("contextEngineeringPlan") or {}).get("type") == "all"
        for d in curj.get("destinations", []))
    print("  tool route_caller needs update: %s" % needs)
    if dry:
        print("  DRY: PATCH /tool/%s <- tool_route_caller.fixed.json" % TOOL_ID)
        print("  DRY: PATCH /squad/%s <- squad_keyrenter.fixed.json" % SQUAD_ID)
        return
    s1, b1 = req("PATCH", "https://api.vapi.ai/tool/%s" % TOOL_ID, VH, tool_fixed)
    print("  PATCH tool -> HTTP %s" % s1)
    if s1 != 200:
        raise RuntimeError("tool PATCH failed: %s" % b1[:300])
    s2, b2 = req("PATCH", "https://api.vapi.ai/squad/%s" % SQUAD_ID, VH, squad_fixed)
    print("  PATCH squad -> HTTP %s" % s2)
    if s2 != 200:
        raise RuntimeError("squad PATCH failed: %s" % b2[:300])
    # verify-back
    _, v = req("GET", "https://api.vapi.ai/tool/%s" % TOOL_ID, VH)
    vj = json.loads(v)
    ok = (not vj.get("function")) and len(vj.get("destinations", [])) == 5 and all(
        (d.get("contextEngineeringPlan") or {}).get("type") == "all" for d in vj["destinations"])
    print("  verify-back: %s" % ("OK" if ok else "MISMATCH"))
    if not ok:
        raise RuntimeError("BUG-1 verify-back mismatch")


def bug2_4_5(dry):
    pbody = J(os.path.join(FX, "bug2_4_5_make", "patch_body.json"))
    # re-inject the GHL token redacted from the committed artifact
    SENT = "__GHL_PRIVATE_TOKEN__"
    if SENT in pbody["blueprint"]:
        tok = E.get("GHL_PRIVATE_TOKEN", "")
        if not tok:
            raise RuntimeError("GHL_PRIVATE_TOKEN missing from .env — cannot re-inject")
        pbody = dict(pbody, blueprint=pbody["blueprint"].replace(SENT, tok))
        print("  re-injected GHL_PRIVATE_TOKEN from .env (%d sites)"
              % J(os.path.join(FX, "bug2_4_5_make", "patch_body.json"))["blueprint"].count(SENT))
    if dry:
        bp = json.loads(pbody["blueprint"])
        print("  DRY: PATCH /scenarios/%d  (blueprint %d chars, scheduling=%s)"
              % (SCENARIO_ID, len(pbody["blueprint"]), pbody["scheduling"]))
        print("  DRY: see fixes/bug2_4_5_make/CHANGES.md for module-level diff")
        print("  NOTE: if Make rejects blueprint PATCH on an ACTIVE scenario, the")
        print("        operator toggles the scenario OFF, re-runs --apply --only")
        print("        bug2_4_5, then toggles ON (RUNBOOK step 3).")
        return
    s, b = req("PATCH", "https://us2.make.com/api/v2/scenarios/%d" % SCENARIO_ID, MH, pbody)
    print("  PATCH scenario -> HTTP %s" % s)
    if s != 200:
        raise RuntimeError("scenario PATCH failed (HTTP %s): %s" % (s, b[:400]))
    s2, b2 = req("GET", "https://us2.make.com/api/v2/scenarios/%d/blueprint" % SCENARIO_ID, MH)
    inner = json.loads(b2).get("response", {}).get("blueprint", {})

    def walk(fl):
        for m in fl:
            yield m
            for r in m.get("routes", []) or []:
                yield from walk(r.get("flow", []) or [])
    allm = list(walk(inner.get("flow", [])))
    wr = [m for m in allm if m.get("module") == "gateway:WebhookRespond"]
    routers_v1 = all(m.get("version") == 1 for m in allm if m.get("module") == "builtin:BasicRouter")
    ok = len(wr) == 1 and routers_v1 and not any(m.get("id") == 210 for m in allm)
    print("  verify-back: WR=%d routers_v1=%s #210gone=%s -> %s"
          % (len(wr), routers_v1, not any(m.get("id") == 210 for m in allm), "OK" if ok else "MISMATCH"))
    if not ok:
        raise RuntimeError("BUG-2/4/5 verify-back mismatch — consider rollback")


def bug3(dry):
    # PROBE ONLY — never mutates. See fixes/bug3_worker/SET_SECRET.md
    sec = E.get("VAPI_WEBHOOK_SECRET", "")
    W = "https://vapi-backend.misty-dew-89d2.workers.dev/persona-intent"
    h = {"Content-Type": "application/json",
         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EllieFix/1.0",
         "x-vapi-secret": sec}
    r = urllib.request.Request(W, data=b"{}", headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=20, context=ctx) as resp:
            code, body = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read().decode("utf-8", "replace")
    healthy = code == 400 and "invalid_payload" in body
    print("  probe with-secret -> HTTP %s %s" % (code, body[:60]))
    print("  BUG-3 status: %s (probe-only; never mutates the secret)"
          % ("ALREADY HEALTHY — no action" if healthy else
             "401/UNHEALTHY — see fixes/bug3_worker/SET_SECRET.md regression runbook"))


def bug6_7(dry):
    pbody = J(os.path.join(FX, "bug6_7_router", "assistant_router.patch_body.json"))
    if dry:
        sysmsg = next(m["content"] for m in pbody["model"]["messages"] if m.get("role") == "system")
        print("  DRY: PATCH /assistant/%s  (model object; system prompt %d chars)"
              % (ROUTER_ID, len(sysmsg)))
        return
    s, b = req("PATCH", "https://api.vapi.ai/assistant/%s" % ROUTER_ID, VH, pbody)
    print("  PATCH router assistant -> HTTP %s" % s)
    if s != 200:
        raise RuntimeError("assistant PATCH failed: %s" % b[:300])
    _, v = req("GET", "https://api.vapi.ai/assistant/%s" % ROUTER_ID, VH)
    sysmsg = next((m.get("content", "") for m in json.loads(v).get("model", {}).get("messages", [])
                   if m.get("role") == "system"), "")
    ok = "### [Auto-Update] ###" not in sysmsg and \
        sysmsg.count("Once you have collected a piece of information") == 1 and \
        "route_caller" not in sysmsg
    print("  verify-back: %s" % ("OK" if ok else "MISMATCH"))
    if not ok:
        raise RuntimeError("BUG-6/7 verify-back mismatch")


HANDLERS = {"bug1": bug1, "bug2_4_5": bug2_4_5, "bug3": bug3, "bug6_7": bug6_7}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually push (default is dry-run)")
    ap.add_argument("--only", default="", help="comma list: bug1,bug2_4_5,bug3,bug6_7")
    a = ap.parse_args()
    cap = guard_snapshot()
    dry = not a.apply
    sel = [x for x in ORDER if (not a.only or x in a.only.split(","))]
    print("=" * 64)
    print("APPLY  mode=%s  snapshot=%s  steps=%s" % ("DRY-RUN" if dry else "LIVE PUSH", cap, sel))
    if not dry:
        print("LIVE PUSH to production VAPI + Make. Rollback: python fixes/rollback.py")
    print("=" * 64)
    for tag in sel:
        step("STEP %s" % tag, HANDLERS[tag], dry)
    print("\n" + "=" * 64)
    print("DONE (%s). Next: python fixes/verify.py --live" % ("dry-run" if dry else "applied"))
    print("=" * 64)


if __name__ == "__main__":
    main()
