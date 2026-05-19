# Ellie (Keyrenter DC) — Fix RUNBOOK

Controlled live push of 5 fix groups. **VAPI + Make have no staging — a
successful API call IS the deployment.** This PR is the review gate for the
change set + reversible push/rollback scripts; the actual production cutover
is the snapshot-protected procedure below, run **post-merge, only on
explicit go-ahead**.

## Fix groups (this PR)
| Group | What | Live target |
|---|---|---|
| BUG-1 | Squad handoff fixed to Canvas's working pattern (drop custom `function`, add `contextEngineeringPlan:all`, strip shadowing member `assistantDestinations`) | VAPI tool `e008d9d3`, squad `80ccd39f` |
| BUG-2+4+5 | One early `WebhookRespond` (always valid JSON, decoupled from filters); relaxed #5 property match; proven `util:SetVariable` onerror on AppFolio+GHL modules; `#210` fail-hack removed; `dlq:true`; BasicRouter `version:1` preserved; auto-aligned | Make scenario `3442510` |
| BUG-3 | **Already healthy** — verified live (probe → 400, not 401). Probe-only, **no mutation** | (none) |
| BUG-6+7 | Router prompt: 3× duplicate directive removed, obsolete `route_caller(persona,confidence,…)` mechanics removed, triage rewritten to native silent-handoff (Canvas-aligned), DYNAMIC DIRECTIVES preserved | VAPI assistant `6236e418` |

## Prerequisites
- `.env` present at repo root (scripts read VAPI/Make tokens + GHL token at runtime; nothing is hard-coded).
- Python 3.x. Run all commands from repo root with `PYTHONUTF8=1` on Windows.

## Procedure (post-merge, on go-ahead)

1. **Snapshot (rollback baseline — must be fresh, captured immediately before push):**
   `python fixes/snapshot.py`  → writes `fixes/_snapshots/` (gitignored; contains the real GHL token & live config — keep local).

2. **Artifact gate:** `python fixes/verify.py`  → expect `25 passed, 0 failed`.

3. **Dry-run:** `python fixes/apply.py`  → review every intended change. No writes.

4. **Live push (in order, idempotent, verify-back after each):**
   `python fixes/apply.py --apply`
   - GHL token is re-injected into the Make blueprint from `.env` at this step (redacted in the committed artifact).
   - **Make active-scenario caveat:** if `STEP bug2_4_5` PATCH errors about the scenario being active, in Make UI toggle scenario `3442510` **OFF**, run `python fixes/apply.py --apply --only bug2_4_5`, then toggle **ON**. The OFF window briefly drops inbound call handling — deliberate operator decision; do it in a low-traffic window.

5. **Confirm live:** `python fixes/verify.py --live`  → live configs match the fix.

6. **Operator acceptance (the real end-to-end test, cannot be automated):**
   place one test call per persona (prospect / tenant / owner / vendor / "I want Debbie"). Confirm: Router silently hands to the right specialist; specialist's `sendToMake<Persona>` returns valid JSON (no "invalid json"/"Accepted"); no loop/dead-air; SMS received.

## Rollback (if anything misbehaves)
`python fixes/rollback.py` (dry-run) → `python fixes/rollback.py --restore --yes`
Restores tool, squad, scenario blueprint, and router prompt from
`fixes/_snapshots/`. BUG-3 has no rollback (never mutated).

## Out of scope (follow-ups, not in this PR)
- **GHL token is embedded inline in the Make blueprint** (pre-existing). Should move to a Make connection. Tracked separately; this PR only redacts it from the committed artifact + re-injects at push.
- BUG-8 (Tenant Turner integration must be *built* — `TENANT_TURNER_API_KEY` is in `.env`), BUG-9 (`.env` AppFolio creds invalid — live Make path unaffected), BUG-10 (voice/model consistency), BUG-11 (retire the parallel `Keyrenter Flow` workflow).
