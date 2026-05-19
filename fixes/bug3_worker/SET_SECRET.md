# BUG-3 — Cloudflare Worker auth

## STATUS: ALREADY RESOLVED — verified live, NO action required

Re-verified against live state during fix work (non-destructive probe +
byte-for-byte secret comparison):

| Check | Result |
|---|---|
| `.env VAPI_WEBHOOK_SECRET` | (19-byte value in `.env`, ends `…2027%`) |
| Vapi Vendor tool `f390f48a` `x-vapi-secret` | byte-identical to `.env` |
| Vapi Uncertain tool `a69c0e85` `x-vapi-secret` | byte-identical to `.env` |
| Worker probe: header secret + `{}` body | **HTTP 400 `invalid_payload`** → auth PASSES |
| Worker probe: no secret + `{}` body | HTTP 401 `unauthorized` (correct) |

The `{"error":"unauthorized"}` in audit call `019e1fb3` was **historical**.
The Worker secret has since been corrected (interim work) so the deployed
`VAPI_WEBHOOK_SECRET` now matches what the Vapi tools send. Vendor/Uncertain
auth is currently healthy.

## DO NOT re-run `wrangler secret put` for this

Re-setting the secret now is **a no-op at best and harmful at worst**: the
value contains a `%`, and a shell-mangled re-set would *break* the
currently-working auth. `fixes/apply.py` therefore treats BUG-3 as
**probe-only** — it re-runs the non-destructive check and does **not**
mutate the secret.

## Regression runbook (ONLY if the probe ever shows 401 with the correct secret)

The Worker code (`vapi-backend/src/routes/persona-intent.ts:23`) is correct;
a future 401 means the deployed secret drifted. To restore — pipe via stdin
so the `%` is not interpolated:

Read the value from `.env` (never paste it into a shell — the `%` mangles):

```bash
cd vapi-backend
grep '^VAPI_WEBHOOK_SECRET=' ../.env | cut -d= -f2- | tr -d '\n' \
  | npx wrangler secret put VAPI_WEBHOOK_SECRET
```
```powershell
((Get-Content ..\.env | Select-String '^VAPI_WEBHOOK_SECRET=') -split '=',2)[1] `
  | npx wrangler secret put VAPI_WEBHOOK_SECRET
```

Then re-run `python fixes/verify.py` and confirm `with-secret -> 400
invalid_payload (AUTH FIXED)`.

## Verification (non-destructive, in `fixes/verify.py`)

Auth is checked *before* payload parsing, so an empty body passes auth then
fails Zod → `400 invalid_payload` with **zero** GHL/SMS side effects. A real
vendor call (with SMS) is the operator's post-merge acceptance test.
