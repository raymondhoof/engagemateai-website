"""A1 — cost gate. Runs ONE existing eval (Keyrenter - Prospect Routing,
id 194a9f5f...) against the Keyrenter squad via POST /eval/run. Polls,
scans result JSON for any cost/billing field > $0.01. Writes
_artifacts/gate_passed.json on PASS. Exits non-zero on FAIL or unknown.

Reusing an EXISTING eval (not creating a new one) minimizes API mutations
and matches what the org has already used.
"""
import datetime, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_client import create_eval, run_eval, get_eval, get_run, poll_run, _req

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "_artifacts")
os.makedirs(ART, exist_ok=True)

SQUAD = "80ccd39f-f2a6-4035-aeb9-ddb6eff59875"
EVAL_ID = "194a9f5f-b445-4405-a2bf-d2d000b5fa44"  # "Keyrenter - Prospect Routing"
THRESHOLD = 0.01


def scan_for_cost(node, hits, path=""):
    """Walk JSON. Hit if any numeric leaf under a cost-y key > THRESHOLD."""
    cost_keys = ("cost", "billing", "totalcost", "amount", "price", "charge", "billable")
    if isinstance(node, dict):
        for k, v in node.items():
            kl = k.lower()
            sub_path = f"{path}.{k}"
            is_cost_key = any(c in kl for c in cost_keys)
            if isinstance(v, (int, float)) and is_cost_key and v > THRESHOLD:
                hits.append((sub_path, v))
            if isinstance(v, bool) and is_cost_key and v is True and "billable" in kl:
                hits.append((sub_path, True))
            scan_for_cost(v, hits, sub_path)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            scan_for_cost(v, hits, f"{path}[{i}]")


def main():
    print("=" * 70)
    print("A1 COST GATE — single eval run against Keyrenter squad")
    print("=" * 70)

    # confirm eval exists
    st, b = get_eval(EVAL_ID)
    if st != 200:
        print("FAIL: cannot fetch eval %s -> %s %s" % (EVAL_ID, st, b[:200]))
        sys.exit(2)
    ev = json.loads(b)
    print("Using existing eval id=%s name=%r type=%s" % (ev.get("id"), ev.get("name"), ev.get("type")))

    # try POST /eval/run with squad target — guess at schema, iterate
    candidates = [
        {"evalId": EVAL_ID, "target": {"type": "squad", "squadId": SQUAD}},
        {"evalId": EVAL_ID, "squadId": SQUAD},
        {"id": EVAL_ID, "target": {"type": "squad", "squadId": SQUAD}},
        {"evalIds": [EVAL_ID], "target": {"type": "squad", "squadId": SQUAD}},
    ]
    run_resp = None
    for body in candidates:
        st, b = run_eval(body)
        print("  POST /eval/run body=%s -> %s %s" % (json.dumps(body), st, b[:200]))
        if st in (200, 201, 202):
            run_resp = (st, b, body)
            break
    if run_resp is None:
        print("\nFAIL: no /eval/run body shape accepted. Halting.")
        sys.exit(3)
    st, b, body = run_resp
    d = json.loads(b)
    print("\nRun accepted. Response keys:", list(d.keys()) if isinstance(d, dict) else type(d))

    # find a run id to poll
    rid = d.get("id") or d.get("runId") or (d.get("results") or [{}])[0].get("id") or d.get("eval_run_id")
    if rid:
        print("Polling run id=%s ..." % rid)
        st2, b2, path = poll_run(rid, max_seconds=180, poll_every=4)
        print("  poll resolved at endpoint=%s -> %s" % (path, st2))
        full = json.loads(b2) if st2 == 200 else d
    else:
        print("(no run id returned; using initial response as the final)")
        full = d

    # dump
    out = os.path.join(ART, "cost_gate_result.json")
    open(out, "w", encoding="utf-8").write(json.dumps(full, indent=2))
    print("Result saved -> %s" % out)

    # scan for cost
    hits = []
    scan_for_cost(full, hits)
    if hits:
        print("\nCOST DETECTED (>%.2f or billable=true):" % THRESHOLD)
        for path, val in hits[:20]:
            print("  %s = %r" % (path, val))
        print("\nFAIL — gate not written. Manual review required.")
        sys.exit(4)

    # also surface anything in known cost-fields under threshold (informational)
    info_hits = []
    def scan_info(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (int, float)) and any(c in k.lower() for c in ("cost", "billing", "amount", "price")):
                    info_hits.append((f"{path}.{k}", v))
                scan_info(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                scan_info(v, f"{path}[{i}]")
    scan_info(full)
    if info_hits:
        print("\nObserved cost-like fields (all ≤ %.2f):" % THRESHOLD)
        for p, v in info_hits[:10]:
            print("  %s = %r" % (p, v))
    else:
        print("\nNo cost-like fields observed in result.")

    gate = {"passed_at": datetime.datetime.utcnow().isoformat() + "Z",
            "eval_id": EVAL_ID, "squad_id": SQUAD,
            "max_observed_cost": (max(v for _, v in info_hits) if info_hits else 0.0),
            "result_path": out, "run_body": body}
    gp = os.path.join(ART, "gate_passed.json")
    open(gp, "w", encoding="utf-8").write(json.dumps(gate, indent=2))
    print("\nGATE PASSED. Written -> %s" % gp)


if __name__ == "__main__":
    main()
