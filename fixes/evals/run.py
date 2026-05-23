"""A2/A3 runner. Ensures each scenario has a Vapi eval object (creates if
absent, indexed by name), runs each against its target, polls for results,
applies LOCAL pass/fail logic on the raw result (toolCalls + content),
emits a markdown report.

Refuses to run if _artifacts/gate_passed.json is missing/stale (>48h).

  python fixes/evals/run.py                   # run all 9 scenarios + report
  python fixes/evals/run.py --scenario 1-prospect-routing
  python fixes/evals/run.py --skip-create     # assume evals exist; just run them
"""
import argparse, datetime, json, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from eval_client import _req, create_eval, run_eval, poll_run
from scenarios import SCENARIOS, FAIL_PATTERNS

ART = os.path.join(HERE, "_artifacts")
REP = os.path.join(HERE, "_reports")
os.makedirs(ART, exist_ok=True)
os.makedirs(REP, exist_ok=True)

EVAL_INDEX = os.path.join(ART, "eval_index.json")


def gate_ok():
    p = os.path.join(ART, "gate_passed.json")
    if not os.path.exists(p):
        return False, "gate_passed.json missing — run cost_gate.py first"
    d = json.load(open(p))
    age = time.time() - time.mktime(time.strptime(d["passed_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    if age > 48 * 3600:
        return False, f"gate stale ({int(age/3600)}h old > 48h)"
    return True, f"gate passed {d['passed_at']} cost=${d['observed_cost_usd']}"


def list_evals():
    st, b = _req("GET", "https://api.vapi.ai/eval?limit=50")
    if st != 200:
        return []
    return json.loads(b).get("results", [])


def ensure_eval(scenario, existing):
    """Find or create the eval; return eval_id."""
    name = "AutoEval: " + scenario["id"]
    for e in existing:
        if e.get("name") == name:
            return e["id"], "reused"
    # create — minimal placeholder judge since we use local logic
    body = {
        "name": name,
        "type": "chat.mockConversation",
        "description": scenario["name"],
        "messages": [
            {"role": "user", "content": scenario["caller_line"]},
            {"role": "assistant", "judgePlan": {"type": "regex", "content": ".*"}},
        ],
    }
    st, b = create_eval(body)
    if st not in (200, 201):
        raise RuntimeError("create_eval failed for %s: %s %s" % (scenario["id"], st, b[:200]))
    return json.loads(b)["id"], "created"


def fire_run(eval_id, target_type, target_id):
    body = {"type": "eval", "evalId": eval_id,
            "target": {"type": target_type,
                       ("squadId" if target_type == "squad" else "assistantId"): target_id}}
    st, b = run_eval(body)
    if st not in (200, 201, 202):
        raise RuntimeError("run_eval failed: %s %s" % (st, b[:200]))
    d = json.loads(b)
    return d.get("evalRunId") or d.get("id")


def fetch_final(run_id, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, b = _req("GET", "https://api.vapi.ai/eval/run/" + run_id)
        if st != 200:
            return None
        d = json.loads(b)
        if str(d.get("status", "")).lower() in ("ended", "completed", "failed", "passed", "succeeded", "error", "errored"):
            return d
        time.sleep(4)
    return None


def evaluate(scenario, run_result):
    """Apply local pass/fail logic to the run result. Returns
    {pass: bool, reasons: [str], observed: {...}}"""
    reasons = []
    observed = {}
    if not run_result:
        return {"pass": False, "reasons": ["no run result"], "observed": {}}
    if (run_result.get("results") or []):
        r0 = run_result["results"][0]
    else:
        return {"pass": False, "reasons": ["no results array"], "observed": {}}
    # the assistant message (second message — first is user echo)
    assistant_msgs = [m for m in (r0.get("messages") or []) if m.get("role") == "assistant"]
    if not assistant_msgs:
        return {"pass": False, "reasons": ["no assistant message"], "observed": {}}
    asst = assistant_msgs[0]
    content = asst.get("content", "") or ""
    tool_calls = asst.get("toolCalls") or []
    observed["content"] = content[:300]
    observed["tool_calls"] = [{"name": (tc.get("function") or {}).get("name"),
                                "args": (tc.get("function") or {}).get("arguments")}
                                for tc in tool_calls]

    chk = scenario["check"]
    if chk["kind"] == "handoff_dest":
        expected = chk["expected_assistant_id"]
        dests = []
        # (a) structured toolCalls (OpenAI-style)
        for tc in tool_calls:
            args_raw = (tc.get("function") or {}).get("arguments") or ""
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                dest = args.get("destination") or args.get("assistantId") or args.get("targetAssistantId")
                if dest:
                    dests.append(dest)
            except Exception:
                pass
        # (b) text-embedded <function=NAME>{json}</function> (Groq llama-3.3 style)
        for m in re.finditer(r"<function=([\w_]+)>(\{.*?\})</function>", content):
            try:
                args = json.loads(m.group(2))
                dest = args.get("destination") or args.get("assistantId") or args.get("targetAssistantId")
                if dest:
                    dests.append(dest)
            except Exception:
                pass
        observed["destinations_seen"] = dests
        if expected in dests:
            pass  # ok
        else:
            reasons.append(f"expected handoff to {expected}, saw {dests or '(no handoff)'}")
    elif chk["kind"] == "content_includes_all":
        for pat in chk["patterns"]:
            if not re.search(pat, content):
                reasons.append(f"content missing required pattern: {pat!r}")

    # global fail patterns scan
    full = content + " " + json.dumps(tool_calls)
    for fp in FAIL_PATTERNS:
        if re.search(fp, full):
            reasons.append(f"fail pattern hit: {fp!r}")

    return {"pass": not reasons, "reasons": reasons, "observed": observed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", help="run one scenario by id")
    ap.add_argument("--skip-create", action="store_true")
    a = ap.parse_args()

    ok, msg = gate_ok()
    print("GATE:", msg)
    if not ok:
        sys.exit(2)

    targets = [s for s in SCENARIOS if (not a.scenario or s["id"] == a.scenario)]
    print(f"Running {len(targets)} scenario(s).\n")

    print("Listing existing evals to deduplicate...")
    existing = list_evals()
    print(f"  {len(existing)} existing evals in org")

    results = []
    for s in targets:
        print(f"\n--- {s['id']} : {s['name']} ---")
        eid, status = ensure_eval(s, existing)
        print(f"  eval id={eid} ({status})")
        rid = fire_run(eid, s["target_type"], s["target_id"])
        print(f"  run id={rid}, polling...")
        rr = fetch_final(rid, timeout=180)
        if rr is None:
            print("  TIMED OUT")
            results.append({"scenario": s, "run_id": rid, "result": None,
                            "verdict": {"pass": False, "reasons": ["poll timeout"], "observed": {}}})
            continue
        # save raw
        open(os.path.join(ART, f"run_{s['id']}.json"), "w", encoding="utf-8").write(json.dumps(rr, indent=2))
        verdict = evaluate(s, rr)
        results.append({"scenario": s, "run_id": rid, "result": rr, "verdict": verdict})
        print(f"  verdict: {'PASS' if verdict['pass'] else 'FAIL'}  reasons={verdict['reasons']}")

    # report
    stamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
    out = os.path.join(REP, f"evals_{stamp}.md")
    total_cost = sum((r["result"] or {}).get("cost", 0) or 0 for r in results)
    passes = sum(1 for r in results if r["verdict"]["pass"])
    lines = [
        f"# Vapi evals report — {stamp}",
        f"squad: 80ccd39f-…  scenarios run: {len(results)}  "
        f"**{passes}/{len(results)} PASS**  total cost: ${total_cost:.4f}",
        "",
    ]
    for r in results:
        s, v = r["scenario"], r["verdict"]
        emoji = "OK" if v["pass"] else "FAIL"
        lines.append(f"## {s['id']}  [{emoji}]  — {s['name']}")
        lines.append(f"- caller: {s['caller_line']!r}")
        lines.append(f"- target: {s['target_type']} `{s['target_id']}`")
        lines.append(f"- check: `{s['check']}`")
        if v["reasons"]:
            lines.append(f"- fail reasons:")
            for x in v["reasons"]:
                lines.append(f"  - {x}")
        lines.append(f"- observed content: `{v['observed'].get('content','')[:200]}`")
        if v["observed"].get("tool_calls"):
            lines.append(f"- tool calls: `{json.dumps(v['observed']['tool_calls'])[:400]}`")
        if v["observed"].get("destinations_seen") is not None:
            lines.append(f"- destinations seen: `{v['observed']['destinations_seen']}`")
        lines.append("")
    open(out, "w", encoding="utf-8").write("\n".join(lines))
    print(f"\nReport -> {out}")
    print(f"Summary: {passes}/{len(results)} pass  cost ${total_cost:.4f}")
    sys.exit(0 if passes == len(results) else 1)


if __name__ == "__main__":
    main()
