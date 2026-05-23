"""Seed 2 Airtable records (Tenant + Uncertain) with Debbie's Notice-to-
Vacate fix, then run apply_approved_fix.py against each. Same flow her
email would have triggered if it had reached the right inbox."""
import json, os, sys, urllib.request, urllib.error, ssl, urllib.parse, subprocess, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ctx = ssl.create_default_context()
E = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    ln = ln.strip()
    if ln and "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        E[k.strip()] = v.strip()

CALL_ID = "019e41ce-1b02-7ff2-887c-6a6c472cf52d"
DEBBIE_EMAIL = """Can we teach Ellie to tell the caller to log in to their resident portal and request the Notice to Vacate? After doing that, they should complete the following link: https://krpm.formstack.com/forms/noticetovacate_044

This part may not be suitable for Ellie at this time however this is what we do after we receive the Request to Vacate we usually email the following: [move-out form + showings coordination]

*Debbie Gomes* / Property Manager / Keyrenter Washington DC"""

TARGETS = [
    {
        "assistant_id": "8968c13a-b097-4387-b292-f4ffdd88c5cd",
        "assistant_name": "Ellie - Tenant",
        "value": ("If the caller mentions vacating, moving out, ending their lease, "
                  "or giving Notice to Vacate, tell them: 'You can start that "
                  "yourself - log into your resident portal and submit a Notice "
                  "to Vacate request. You'll receive a move-out form to complete "
                  "next, and our team will reach out within one business day to "
                  "coordinate showings.' Do not capture move-out details on the call."),
        "rationale": ("Debbie's email (Notice to Vacate self-service): direct vacating "
                      "tenants to the resident portal + formstack form rather than just "
                      "taking a message. Tenant assistant covers direct-stated vacate intent."),
    },
    {
        "assistant_id": "afc690ca-cbb3-4cb0-8dad-7cc5aabec5c0",
        "assistant_name": "Ellie - Uncertain / Message Taking",
        "value": ("If the caller asks about vacating, moving out, ending their lease, "
                  "or wants to give Notice to Vacate, before taking the message tell "
                  "them: 'You can start that yourself - log into your resident portal "
                  "and submit a Notice to Vacate request. You'll receive a move-out "
                  "form to complete next.' Then still take a message so the team has "
                  "it on file, and confirm a team member will follow up within one "
                  "business day."),
        "rationale": ("Debbie's email (Notice to Vacate self-service): covers callers "
                      "who route to Uncertain via 'I want to talk to Debbie' (per ref "
                      "call %s - Timothy Chicknakin, 'preparing to vacate'). Still "
                      "takes a message so team has it on file." % CALL_ID),
    },
]

ah = {"Authorization": "Bearer " + E["AIRTABLE_API_KEY"], "Content-Type": "application/json",
      "User-Agent": "EllieFix/1.0"}
base = E["AIRTABLE_BASE_ID"]
table_enc = urllib.parse.quote(E["AIRTABLE_TABLE_NAME"])


def req(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    try:
        r = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(r, timeout=60, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as ex:
        return None, "EXC:%s" % ex


now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
created_ids = []
print("== Create Airtable records (Status=Approved) ==")
for t in TARGETS:
    fix = {
        "target_assistant_id": t["assistant_id"],
        "field": "model.messages[role=system].content",
        "operation": "append_instruction",
        "value": t["value"],
    }
    fields = {
        "Call ID": CALL_ID,
        "Status": "Approved",
        "Timestamp": now,
        "Assistant ID": t["assistant_id"],
        "Assistant Name": t["assistant_name"],
        "Rationale": t["rationale"],
        "Proposed Fix": json.dumps(fix, indent=2),
        "Transcript": DEBBIE_EMAIL,
    }
    body = {"records": [{"fields": fields}], "typecast": True}
    st, b = req("POST", "https://api.airtable.com/v0/%s/%s" % (base, table_enc), ah, body)
    if st in (200, 201):
        recs = json.loads(b).get("records", [])
        rid = recs[0]["id"] if recs else None
        print("  %s [%s] -> rec %s" % (t["assistant_name"], st, rid))
        created_ids.append(rid)
    else:
        print("  %s -> FAIL %s %s" % (t["assistant_name"], st, b[:300]))

print("\n== Run apply_approved_fix.py per record (--record-id, surgical) ==")
for rid in created_ids:
    print("\n--- apply for %s ---" % rid)
    p = subprocess.run([sys.executable, os.path.join(ROOT, "apply_approved_fix.py"),
                        "--record-id", rid],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    print(p.stdout[-2000:])
    if p.returncode != 0:
        print("STDERR:", p.stderr[-500:])
