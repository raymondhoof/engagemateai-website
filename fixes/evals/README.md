# fixes/evals — Vapi evals harness for the Keyrenter squad

Replaces the need for a human to dial in per persona. 9 scripted scenarios cover the 5 specialist handoffs (Prospect/Tenant/Owner/Vendor/Uncertain), the new Notice-to-Vacate routing on both Tenant and Uncertain, and the Notice-to-Vacate directive content surfacing in Tenant + Uncertain prompts.

Pass/fail logic is local (in `run.py`), not Vapi's judges — Vapi's regex judges don't account for tool calls in Groq's text-embedded `<function=…>{...}</function>` format. The runner parses both that format AND structured `toolCalls`.

## Commands
```
python fixes/evals/cost_gate.py            # ONE trivial run; writes _artifacts/gate_passed.json on PASS
python fixes/evals/run.py                  # runs all 9 scenarios; refuses without fresh gate (<48h)
python fixes/evals/run.py --scenario <id>  # one scenario by id
```
Latest report: `_reports/evals_<UTC>.md` (gitignored).

## Cost
~$0.0005–$0.0025 per scenario (Groq llama-3.3-70b prompt tokens; no telephony). Full 9-suite ≈ $0.02. Cost gate halts the runner if any single eval exceeds $0.01.

## Scenarios

| id | target | check |
|---|---|---|
| 1-prospect-routing | squad | handoff → Ellie–Prospect |
| 2-tenant-maintenance-routing | squad | handoff → Ellie–Tenant |
| 3-tenant-vacate-routing | squad | handoff → Ellie–Tenant (NEW BUG-fix path) |
| 4-owner-routing | squad | handoff → Ellie–Owner |
| 5-vendor-routing | squad | handoff → Ellie–Vendor |
| 6-uncertain-debbie-routing | squad | handoff → Ellie–Uncertain |
| 7-uncertain-debbie-vacate-routing | squad | handoff → Ellie–Uncertain |
| 8-tenant-vacate-directive | assistant=Tenant | content contains "resident portal" + "notice to vacate" |
| 9-uncertain-vacate-directive | assistant=Uncertain | content contains "resident portal" + "notice to vacate" |

Global fail patterns scanned in every result: `No handoff destination returned`, `{"error":"unauthorized"}`, `invalid json response body`, banned transfer language.

## Evals re-use
The runner deduplicates by name (`AutoEval: <scenario_id>`). Re-runs reuse the existing eval; only the run is fired. New scenarios → new eval objects created automatically.
