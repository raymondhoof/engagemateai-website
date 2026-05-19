"""BUG-2 + BUG-4 + BUG-5 — transform the live snapshot of Make scenario
3442510 into a fixed blueprint + exact PATCH body. Pure transform.

Grounded in REAL shapes (fixes/_snapshots/blueprint_3442510.before.json,
inspected) and the user's OWN proven onerror pattern (fix_elc_onerror.json:
util:SetVariable capturing {{error.message}}, roundtrip scope — non-fatal +
observable).

Changes
-------
BUG-2 (always valid JSON, decouple from filters):
  * Insert ONE gateway:WebhookRespond immediately after the trigger (flow[1],
    before the Persona Router) returning a STATIC, guaranteed-valid body
    {"status":"received"} with Content-Type: application/json, HTTP 200.
  * Remove all 13 downstream WebhookRespond modules (ids 9,11,13,15,24,26,
    28,30,37,39,43,194,237). Make honours only the first response; with the
    early one in place the later ones would error ("response already sent")
    on modules that have no onerror. Removing them is safe — each is a
    terminal leaf in its route.

BUG-4 (property match no longer drops every row):
  * Module #5 filter: replace the 4-AND chain (property_name contains
    interest AND posted_to_website=yes AND rentable=yes AND visibility=
    active) with a 2-branch OR, case-insensitive:
      property_name contains interest  OR  interest contains property_name.
  * Scope: ONLY #5 (the report's cited text filter). #20 (tenant phone
    match) and #192 (numeric bedroom/budget) are correct logic and left
    untouched to minimise blast radius.

BUG-5 (error handlers + remove the fail hack):
  * Add the proven util:SetVariable onerror handler to the 4 AppFolio
    modules (#3,#217,#31,#189) and every GHL http:ActionSendData module
    (leadconnectorhq) so a failure is non-fatal AND captured.
  * Delete the #210 http://thisurldoesnotexist.fail/stop hack and its
    builtin:Commit onerror (#215) entirely.
  * Add metadata.scenario with dlq:true (store incomplete executions) so
    real failures become visible, mirroring fix_elc_onerror.json's
    metadata.scenario block. instant:true is preserved.

Memory-mandated invariants (verified post-transform, will hard-fail build):
  * Every builtin:BasicRouter keeps "version":1 + "mapper":null + routes.
  * Auto-align: x/y recomputed (300px sequential; router routes fan
    vertically 300px; onerror 150 down / 300 right of parent).

Run:  python fixes/bug2_4_5_make/build_blueprint.py
"""
import copy, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(os.path.dirname(HERE), "_snapshots")

APPFOLIO_IDS = {3, 217, 31, 189}
WR = "gateway:WebhookRespond"
FAIL_HACK_ID = 210


def load_inner():
    raw = json.load(open(os.path.join(SNAP, "blueprint_3442510.before.json"), encoding="utf-8"))
    bp = raw["response"]["blueprint"]            # {flow, name, metadata}
    meta = json.load(open(os.path.join(SNAP, "scenario_3442510_meta.before.json"), encoding="utf-8"))
    sched = (meta.get("scenario") or {}).get("scheduling") or {"type": "immediately"}
    return copy.deepcopy(bp), sched


def walk(flow):
    for m in flow:
        yield m
        for r in m.get("routes", []) or []:
            yield from walk(r.get("flow", []) or [])


def max_id(flow):
    return max((m.get("id", 0) for m in walk(flow)), default=0)


def prune_modules(flow, drop_ids=None, drop_modules=None):
    """Recursively remove modules whose id in drop_ids or module in drop_modules."""
    drop_ids = drop_ids or set()
    drop_modules = drop_modules or set()
    out = []
    for m in flow:
        if m.get("id") in drop_ids or m.get("module") in drop_modules:
            continue
        if m.get("routes"):
            for r in m["routes"]:
                r["flow"] = prune_modules(r.get("flow", []) or [], drop_ids, drop_modules)
        out.append(m)
    return out


def auto_align(flow, x=0, y=1800):
    """300px sequential horizontally; router fans routes vertically 300px
    centred on the router's y; onerror 150 down / 300 right of its parent."""
    cx = x
    for m in flow:
        m.setdefault("metadata", {})
        des = m["metadata"].get("designer") or {}
        des["x"], des["y"] = cx, y
        m["metadata"]["designer"] = des
        if m.get("onerror"):
            for j, h in enumerate(m["onerror"]):
                h.setdefault("metadata", {})
                h["metadata"]["designer"] = {"x": cx + 300, "y": y + 150 + j * 150}
        routes = m.get("routes") or []
        if routes:
            n = len(routes)
            top = y - ((n - 1) * 300) // 2
            for i, r in enumerate(routes):
                auto_align(r.get("flow", []) or [], cx + 300, top + i * 300)
        cx += 300


def onerror_handler(parent_id, hid):
    return [{
        "id": hid,
        "module": "util:SetVariable",
        "version": 1,
        "mapper": {"name": "errModule%d" % parent_id, "scope": "roundtrip",
                   "value": "{{error.message}}"},
        "metadata": {"designer": {"x": 0, "y": 0}},
        "parameters": {},
    }]


def main():
    bp, sched = load_inner()
    flow = bp["flow"]
    changes = []

    # ---- guard: structure is what we inspected ----
    assert len(flow) == 2 and flow[0]["id"] == 1, "unexpected top flow"
    assert flow[1]["module"] == "builtin:BasicRouter", "expected Persona Router at flow[1]"
    trigger, persona_router = flow[0], flow[1]

    nid = max(max_id(flow), 250)

    # ---- BUG-5a: drop the fail hack (#210 + its onerror) ----
    before = len(list(walk(flow)))
    flow[:] = prune_modules(flow, drop_ids={FAIL_HACK_ID})
    changes.append("BUG-5: removed #%d http://thisurldoesnotexist.fail/stop hack (+ onerror Commit #215)" % FAIL_HACK_ID)

    # ---- BUG-2: remove all WebhookRespond, insert ONE early ----
    wr_ids = [m["id"] for m in walk(flow) if m.get("module") == WR]
    flow[:] = prune_modules(flow, drop_modules={WR})
    changes.append("BUG-2: removed %d downstream WebhookRespond modules %s" % (len(wr_ids), wr_ids))

    nid += 1
    early = {
        "id": nid,
        "module": WR,
        "version": 1,
        "mapper": {
            "status": "200",
            "body": '{"status":"received"}',
            "headers": [{"key": "Content-Type", "value": "application/json"}],
        },
        "metadata": {"designer": {"x": 300, "y": 1800, "name": "Early Respond (always valid JSON)"}},
    }
    # new top flow: trigger -> early respond -> persona router
    bp["flow"] = [trigger, early, persona_router]
    flow = bp["flow"]
    changes.append("BUG-2: inserted #%d early WebhookRespond {\"status\":\"received\"} (Content-Type JSON, 200) as flow[1]" % nid)

    # ---- BUG-4: relax #5 property-match filter ----
    fixed5 = False
    for m in walk(flow):
        if m.get("id") == 5:
            m["filter"] = {
                "name": "Pull Property (relaxed)",
                "conditions": [
                    [{"a": "{{174.property_name}}", "b": "{{1.property_interest}}", "o": "text:contain:ci"}],
                    [{"a": "{{1.property_interest}}", "b": "{{174.property_name}}", "o": "text:contain:ci"}],
                ],
            }
            fixed5 = True
    assert fixed5, "module #5 not found — cannot apply BUG-4"
    changes.append("BUG-4: #5 filter -> 2-branch OR (name contains interest OR interest contains name), ci; dropped posted_to_website/rentable/visibility ANDs")

    # ---- BUG-5b: onerror handlers on AppFolio + GHL HTTP modules ----
    eh = []
    for m in walk(flow):
        mod = m.get("module", "")
        url = (m.get("mapper") or {}).get("url", "") if isinstance(m.get("mapper"), dict) else ""
        is_appfolio = m.get("id") in APPFOLIO_IDS
        is_ghl = mod.startswith("http:") and "leadconnectorhq.com" in str(url)
        if (is_appfolio or is_ghl) and not m.get("onerror"):
            nid += 1
            m["onerror"] = onerror_handler(m["id"], nid)
            eh.append(m["id"])
    changes.append("BUG-5: added util:SetVariable onerror (proven pattern) to modules %s" % sorted(eh))

    # ---- BUG-5c: scenario.dlq=true for failure visibility ----
    bp.setdefault("metadata", {})
    scn = bp["metadata"].get("scenario") or {}
    scn.update({"dlq": True, "maxErrors": 3, "autoCommit": True,
                "sequential": False, "confidential": False})
    bp["metadata"]["scenario"] = scn
    bp["metadata"]["instant"] = True            # preserve sync webhook (memory)
    bp["metadata"].setdefault("version", 1)
    changes.append("BUG-5: metadata.scenario.dlq=true (store incomplete executions); instant:true preserved")

    # ---- memory invariant: BasicRouter version:1 + mapper:null ----
    for m in walk(flow):
        if m.get("module") == "builtin:BasicRouter":
            m["version"] = 1
            m["mapper"] = None
            assert "routes" in m and m["routes"], "router #%s lost routes!" % m.get("id")

    # ---- auto-align (memory) ----
    auto_align(bp["flow"])

    # ---- POST-TRANSFORM ASSERTIONS (hard-fail the build) ----
    allm = list(walk(bp["flow"]))
    wr_now = [m for m in allm if m.get("module") == WR]
    routers = [m for m in allm if m.get("module") == "builtin:BasicRouter"]
    assert len(wr_now) == 1, "expected exactly 1 WebhookRespond, got %d" % len(wr_now)
    assert wr_now[0]["id"] == early["id"], "the single WR is not the early one"
    assert all(r.get("version") == 1 for r in routers), "a BasicRouter lost version:1"
    assert len(routers) == 4, "expected 4 routers, got %d" % len(routers)
    assert not any(m.get("id") == FAIL_HACK_ID for m in allm), "#210 still present"
    for aid in APPFOLIO_IDS:
        am = next((m for m in allm if m.get("id") == aid), None)
        assert am and am.get("onerror"), "AppFolio #%s missing onerror" % aid
    # persona routes intact (6 routes on Persona Router #2)
    pr = next(m for m in allm if m.get("id") == 2)
    assert len(pr.get("routes", [])) == 6, "Persona Router must keep 6 routes, has %d" % len(pr.get("routes", []))

    # ---- SECRET REDACTION (pre-existing: GHL token is embedded inline in
    # every GHL http module's Authorization header). NEVER commit the raw
    # token. Replace with a sentinel; apply.py re-injects from .env at push
    # time so live behaviour is unchanged. (Token-in-blueprint vs a Make
    # connection is a pre-existing smell — noted in RUNBOOK as follow-up.)
    GHL_SENTINEL = "__GHL_PRIVATE_TOKEN__"
    root = os.path.dirname(os.path.dirname(HERE))
    real_tok = None
    for ln in open(os.path.join(root, ".env"), encoding="utf-8"):
        ln = ln.strip()
        if ln.startswith("GHL_PRIVATE_TOKEN="):
            real_tok = ln.split("=", 1)[1].strip()
    bp_str = json.dumps(bp, ensure_ascii=False)
    n_red = 0
    if real_tok and real_tok in bp_str:
        n_red = bp_str.count(real_tok)
        bp_str = bp_str.replace(real_tok, GHL_SENTINEL)
    bp = json.loads(bp_str)
    assert real_tok is None or real_tok not in json.dumps(bp), "GHL token not fully redacted"
    changes.append("SECURITY: redacted GHL_PRIVATE_TOKEN in %d place(s) -> %s (re-injected at push)"
                    % (n_red, GHL_SENTINEL))

    # ---- emit artifacts ----
    with open(os.path.join(HERE, "blueprint_3442510.fixed.json"), "w", encoding="utf-8") as f:
        json.dump(bp, f, indent=2)
    patch_body = {"blueprint": json.dumps(bp, ensure_ascii=False),
                  "scheduling": json.dumps(sched, ensure_ascii=False)}
    with open(os.path.join(HERE, "patch_body.json"), "w", encoding="utf-8") as f:
        json.dump(patch_body, f, indent=2)
    with open(os.path.join(HERE, "CHANGES.md"), "w", encoding="utf-8") as f:
        f.write("# Scenario 3442510 — transform changelog\n\n")
        f.write("modules: %d -> %d\n\n" % (before, len(allm)))
        for c in changes:
            f.write("- %s\n" % c)

    print("Blueprint transform OK. modules %d -> %d" % (before, len(allm)))
    for c in changes:
        print("  -", c)
    print("Assertions passed: 1 WR(early), 4 routers@v1, 6 persona routes, "
          "#210 gone, AppFolio onerror present.")
    print("Artifacts: blueprint_3442510.fixed.json, patch_body.json, CHANGES.md")


if __name__ == "__main__":
    main()
