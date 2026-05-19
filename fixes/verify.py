"""verify.py — verification suite (the PR gate is `--artifacts`).

  python fixes/verify.py                # artifact checks + non-destructive worker auth probe (PR gate)
  python fixes/verify.py --live         # ALSO GET live configs and assert they match the fix (post-push)

Never has side effects. Does NOT POST to the Make webhook (post-fix that
would still send a real SMS — that is the operator's post-merge call test).
"""
import argparse, json, os, sys, urllib.request, urllib.error, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FX = os.path.join(ROOT, "fixes")
SNAP = os.path.join(FX, "_snapshots")
ctx = ssl.create_default_context()
SQUAD_ID = "80ccd39f-f2a6-4035-aeb9-ddb6eff59875"
TOOL_ID = "e008d9d3-96d1-4ef7-ac11-e551b981d786"
ROUTER_ID = "6236e418-b4d0-480f-aeb6-c245af67d273"
WORKER = "https://vapi-backend.misty-dew-89d2.workers.dev/persona-intent"
PASS, FAIL = "PASS", "FAIL"
results = []


def chk(name, cond, detail=""):
    results.append((PASS if cond else FAIL, name, detail))
    print("  [%s] %s%s" % (PASS if cond else FAIL, name, (" — " + detail) if detail and not cond else ""))


def env():
    d = {}
    for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        ln = ln.strip()
        if ln and "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def J(p):
    return json.load(open(p, encoding="utf-8"))


# ---------------- artifact checks (PR gate) ----------------
def check_artifacts():
    print("\n== BUG-1 artifacts ==")
    t = J(os.path.join(FX, "bug1_squad_handoff", "tool_route_caller.fixed.json"))
    chk("route_caller: custom function removed", t.get("function") in (None,), repr(t.get("function"))[:40])
    chk("route_caller: type=handoff", t.get("type") == "handoff")
    d = t.get("destinations", [])
    chk("route_caller: 5 destinations", len(d) == 5, "got %d" % len(d))
    chk("route_caller: every dest has description + contextEngineeringPlan{all}",
        all(x.get("description") and (x.get("contextEngineeringPlan") or {}).get("type") == "all" for x in d))
    sq = J(os.path.join(FX, "bug1_squad_handoff", "squad_keyrenter.fixed.json"))
    mem = sq.get("members", [])
    chk("squad: 6 members", len(mem) == 6, "got %d" % len(mem))
    chk("squad: NO member has assistantDestinations (matches Canvas)",
        all("assistantDestinations" not in m for m in mem))

    print("\n== BUG-2+4+5 artifacts ==")
    bp = J(os.path.join(FX, "bug2_4_5_make", "blueprint_3442510.fixed.json"))
    flow = bp["flow"]

    def walk(fl):
        for m in fl:
            yield m
            for r in m.get("routes", []) or []:
                yield from walk(r.get("flow", []) or [])
    allm = list(walk(flow))
    wr = [m for m in allm if m.get("module") == "gateway:WebhookRespond"]
    chk("exactly 1 WebhookRespond", len(wr) == 1, "got %d" % len(wr))
    chk("WR is early (flow[1], before router)",
        len(flow) >= 2 and flow[1].get("module") == "gateway:WebhookRespond")
    if wr:
        body = wr[0].get("mapper", {}).get("body")
        try:
            json.loads(body)
            okjson = True
        except Exception:
            okjson = False
        chk("WR body is valid JSON", okjson, repr(body)[:40])
        hdrs = wr[0].get("mapper", {}).get("headers", [])
        chk("WR sets Content-Type: application/json",
            any(h.get("key") == "Content-Type" and "json" in h.get("value", "") for h in hdrs))
    routers = [m for m in allm if m.get("module") == "builtin:BasicRouter"]
    chk("4 BasicRouters", len(routers) == 4, "got %d" % len(routers))
    chk("every BasicRouter version==1 (memory: else routes silently stripped)",
        all(r.get("version") == 1 for r in routers))
    pr = next((m for m in allm if m.get("id") == 2), None)
    chk("Persona Router keeps 6 routes", pr and len(pr.get("routes", [])) == 6,
        "got %s" % (len(pr.get("routes", [])) if pr else "missing"))
    chk("#210 fail-hack removed", not any(m.get("id") == 210 for m in allm))
    chk("AppFolio #3/#217/#31/#189 have onerror",
        all(next((m for m in allm if m.get("id") == i), {}).get("onerror") for i in (3, 217, 31, 189)))
    empties = [(m.get("id"), i) for m in allm for i, r in enumerate(m.get("routes", []) or [])
               if not (r.get("flow") or [])]
    chk("no empty route flows", not empties, str(empties))
    pbody = J(os.path.join(FX, "bug2_4_5_make", "patch_body.json"))
    chk("patch_body has blueprint+scheduling (proven Make PATCH shape)",
        "blueprint" in pbody and "scheduling" in pbody)
    try:
        json.loads(pbody["blueprint"]); json.loads(pbody["scheduling"]); spok = True
    except Exception:
        spok = False
    chk("patch_body strings re-parse", spok)

    print("\n== BUG-6+7 artifacts ==")
    p = open(os.path.join(FX, "bug6_7_router", "router_system_prompt.fixed.txt"), encoding="utf-8").read()
    chk("0x '### [Auto-Update] ###' blocks", p.count("### [Auto-Update] ###") == 0)
    chk("'captured info' directive appears exactly once",
        p.count("Once you have collected a piece of information") == 1)
    chk("no obsolete route_caller/confidence mechanics",
        all(b not in p for b in ("route_caller", "confidence >= 0.7", "transfer_request")))
    chk("DYNAMIC DIRECTIVES block preserved",
        "### DYNAMIC DIRECTIVES ###" in p and "### END DYNAMIC DIRECTIVES ###" in p)
    pb = J(os.path.join(FX, "bug6_7_router", "assistant_router.patch_body.json"))
    chk("router patch_body sends whole model object (siblings preserved)",
        "model" in pb and "messages" in pb["model"] and pb["model"].get("provider"))


# ---------------- non-destructive worker auth probe ----------------
def worker_probe():
    print("\n== BUG-3 worker auth probe (non-destructive: empty body -> 400 before any side effect) ==")
    E = env()
    sec = E.get("VAPI_WEBHOOK_SECRET", "")

    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EllieFix/1.0"  # non-default UA: Cloudflare 1010s urllib default

    def post(headers):
        headers = dict(headers, **{"User-Agent": UA})
        r = urllib.request.Request(WORKER, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(r, timeout=20, context=ctx) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            return None, "EXC:%s" % e

    st_no, b_no = post({"Content-Type": "application/json"})
    chk("no-secret -> 401 unauthorized", st_no == 401 and "unauthorized" in b_no, "got %s %s" % (st_no, b_no[:60]))
    st_ok, b_ok = post({"Content-Type": "application/json", "x-vapi-secret": sec})
    if st_ok == 400 and "invalid_payload" in b_ok:
        chk("with-secret -> 400 invalid_payload (AUTH FIXED)", True)
    elif st_ok == 401:
        chk("with-secret -> auth (currently 401 — secret NOT yet set; expected PRE-fix)", False,
            "run BUG-3 SET_SECRET step; re-verify post-push")
    else:
        chk("with-secret -> 400 invalid_payload", False, "got %s %s" % (st_ok, b_ok[:80]))


# ---------------- live config confirmation (post-push) ----------------
def check_live():
    print("\n== LIVE config confirmation (post-push) ==")
    E = env()
    vh = {"Authorization": "Bearer " + E["VAPI_API_KEY"], "User-Agent": "EllieFix/1.0"}
    mh = {"Authorization": "Token " + E["MAKE_API_TOKEN"], "User-Agent": "Mozilla/5.0 EllieFix/1.0"}

    def get(u, h):
        r = urllib.request.Request(u, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=40, context=ctx) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            return None, str(e)

    st, t = get("https://api.vapi.ai/tool/%s" % TOOL_ID, vh)
    chk("live route_caller: function unset", st == 200 and not t.get("function"))
    chk("live route_caller: 5 dests w/ contextEngineeringPlan",
        st == 200 and len(t.get("destinations", [])) == 5 and
        all((d.get("contextEngineeringPlan") or {}).get("type") == "all" for d in t.get("destinations", [])))
    st, s = get("https://api.vapi.ai/squad/%s" % SQUAD_ID, vh)
    chk("live squad: no member assistantDestinations",
        st == 200 and all("assistantDestinations" not in m for m in s.get("members", [])))
    st, a = get("https://api.vapi.ai/assistant/%s" % ROUTER_ID, vh)
    sysmsg = ""
    if st == 200:
        sysmsg = next((m.get("content", "") for m in (a.get("model", {}).get("messages") or [])
                       if m.get("role") == "system"), "")
    chk("live router prompt deduped (no Auto-Update, 1x captured-info)",
        "### [Auto-Update] ###" not in sysmsg and
        sysmsg.count("Once you have collected a piece of information") == 1)
    st, bp = get("https://us2.make.com/api/v2/scenarios/3442510/blueprint", mh)
    if st == 200:
        inner = bp.get("response", {}).get("blueprint", {})

        def walk(fl):
            for m in fl:
                yield m
                for r in m.get("routes", []) or []:
                    yield from walk(r.get("flow", []) or [])
        allm = list(walk(inner.get("flow", [])))
        wr = [m for m in allm if m.get("module") == "gateway:WebhookRespond"]
        chk("live scenario: exactly 1 WebhookRespond (early)", len(wr) == 1)
        chk("live scenario: every BasicRouter version==1",
            all(m.get("version") == 1 for m in allm if m.get("module") == "builtin:BasicRouter"))
        chk("live scenario: #210 hack gone", not any(m.get("id") == 210 for m in allm))
    else:
        chk("live scenario fetch", False, str(bp)[:80])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also confirm live configs match (post-push)")
    ap.add_argument("--skip-worker", action="store_true", help="skip the network worker probe")
    a = ap.parse_args()
    print("=" * 70)
    print("VERIFY — artifact gate%s" % (" + live" if a.live else ""))
    print("=" * 70)
    check_artifacts()
    if not a.skip_worker:
        worker_probe()
    if a.live:
        check_live()
    n_fail = sum(1 for r in results if r[0] == FAIL)
    print("\n" + "=" * 70)
    print("RESULT: %d passed, %d failed" % (sum(1 for r in results if r[0] == PASS), n_fail))
    if n_fail:
        print("FAILURES:")
        for s, n, d in results:
            if s == FAIL:
                print("  - %s %s" % (n, ("(%s)" % d) if d else ""))
    print("=" * 70)
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
