# fixes/ — Ellie (Keyrenter DC) P0/P1 remediation

Why these fixes (evidence from live config + Make exec logs + 51 call transcripts):

- **Real failure was not "AppFolio/Tenant Turner lookups."** AppFolio returns
  data fine at runtime. Tenant Turner has no integration. The actual chain:
  the squad handoff never resolved (Router handled every call itself), and
  the Make webhook never returned JSON (caller got "Accepted" → Vapi tool
  error → Ellie falsely said "I'll pass this along" while data was lost).
- BUG-1 (handoff) and BUG-2 (always-valid-JSON) are the P0 root causes;
  BUG-4/5/6/7 remove the aggravating factors. BUG-3 was already resolved by
  interim work (verified live).

## Layout
```
fixes/
  RUNBOOK.md                  operator procedure (push order, caveats, rollback)
  snapshot.py                 capture live pre-push state -> _snapshots/ (rollback baseline)
  verify.py                   artifact gate (25 checks) + non-destructive worker auth probe; --live
  apply.py                    DRY-RUN by default; --apply pushes (idempotent, ordered, verify-back)
  rollback.py                 restore everything from _snapshots/
  _snapshots/                 GITIGNORED — live state incl. real GHL token; regenerate before push
  bug1_squad_handoff/         tool_route_caller.fixed.json, squad_keyrenter.fixed.json, build.py
  bug2_4_5_make/              blueprint_3442510.fixed.json, patch_body.json, CHANGES.md, build_blueprint.py
  bug3_worker/                SET_SECRET.md  (status: already healthy; probe-only; do NOT reset)
  bug6_7_router/              router_system_prompt.fixed.txt, *.patch_body.json, *.diff, build_prompt.py
```

## Design guarantees
- Every fix is derived from the live snapshot by a pure `build*.py` and
  validated by hard assertions + `verify.py` (mirrors the **working Canvas
  Living** config; uses the user's **own proven Make onerror pattern**).
- `GHL_PRIVATE_TOKEN` is redacted from the committed blueprint and
  re-injected from `.env` at push time. No secret is committed.
- Memory-mandated Make invariants enforced: `builtin:BasicRouter` keeps
  `version:1` (else routes silently strip); modules auto-aligned.
- Nothing here pushes to production until the RUNBOOK is run post-merge on
  explicit go-ahead. `apply.py` is dry-run unless `--apply`.
