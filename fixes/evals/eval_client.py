"""Thin wrapper around Vapi's evals API.
  - POST /eval                       -> create an eval (mockConversation + judgePlan)
  - POST /eval/run                   -> run an eval against a target (squad or assistant)
  - GET  /eval/{id}                  -> fetch eval definition
  - GET  /eval-run/{runId}           -> fetch run results (polled)
Read-only-ish: create + run are write operations; polling is read."""
import json, os, time, urllib.request, urllib.error, ssl

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ctx = ssl.create_default_context()
_E = None


def env():
    global _E
    if _E is None:
        _E = {}
        for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
            ln = ln.strip()
            if ln and "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                _E[k.strip()] = v.strip()
    return _E


def _h():
    return {"Authorization": "Bearer " + env()["VAPI_API_KEY"],
            "Content-Type": "application/json", "User-Agent": "EllieFix/1.0"}


def _req(method, url, body=None, timeout=40):
    data = json.dumps(body).encode() if body is not None else None
    try:
        r = urllib.request.Request(url, data=data, headers=_h(), method=method)
        with urllib.request.urlopen(r, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as ex:
        return None, "EXC:%s" % ex


def create_eval(body):
    return _req("POST", "https://api.vapi.ai/eval", body)


def run_eval(body):
    return _req("POST", "https://api.vapi.ai/eval/run", body)


def get_eval(eid):
    return _req("GET", "https://api.vapi.ai/eval/" + eid)


def get_run(rid):
    # /eval/run/{id} — confirmed endpoint
    st, b = _req("GET", "https://api.vapi.ai/eval/run/" + rid)
    return st, b, "/eval/run/"


def poll_run(rid, max_seconds=120, poll_every=4):
    deadline = time.time() + max_seconds
    last = None
    last_path = None
    while time.time() < deadline:
        st, b, path = get_run(rid)
        last, last_path = (st, b), path
        if st != 200:
            return st, b, path
        try:
            d = json.loads(b)
        except Exception:
            return st, b, path
        status = d.get("status") or (d.get("run") or {}).get("status")
        if status and str(status).lower() in ("completed", "passed", "failed", "succeeded", "error", "errored", "terminal"):
            return st, b, path
        time.sleep(poll_every)
    return last[0], last[1], last_path


if __name__ == "__main__":
    print("evals API base sanity:", _req("GET", "https://api.vapi.ai/eval?limit=1"))
