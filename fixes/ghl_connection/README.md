# fixes/ghl_connection — Migrate inline GHL token → Make Connection

## Status: SHELVED (2026-05-21)

Premise was: replace the literal `Authorization: Bearer pit-…` embedded in 19 `http:ActionSendData` modules of scenario 3442510 with `__IMTCONN__: <conn_id>` pointing at a Make Connection holding the token server-side.

**Blocker (proven by API probes):** Make's `POST /connections?teamId=…` rejected every plausible `accountType` enum (`http:bearer`, `http2:bearer`, `http:apiKeyAuth`, `bearer`, `http`, `http2`, …) with `"Failed to load manifest for connection '<name>'"`. The org has 23 existing connections — all are `airtable2`/`openai-gpt-3`/`asana`/`google`; none are HTTP-bearer. No mirror config exists.

Additional uncertainty: it's not yet confirmed that `http:ActionSendData` (HTTP module v3) even accepts `__IMTCONN__` references at all. The matching auth-aware variant `http:ActionSendDataBasicAuth` does (we use it for AppFolio), but Bearer-via-connection may require switching all 19 modules to a different module type — a much bigger refactor than the original plan assumed.

## Why shelved (cost/benefit)

The token-inline-in-blueprint is a pre-existing smell, **not** a leaked secret:
- Not in git history (`git log --all -p | grep "pit-<prefix>"` = 0).
- Only Make team members can read the blueprint (small, trusted blast radius).
- Worker secret rotation (BUG-3) already happened independently; this is a separate item.

Migrating it without a working API path requires UI work + uncertain module-type migration. Effort/risk ratio doesn't justify pushing through now.

## To resume

If/when you (or Make support) confirm:
1. The exact `accountType` enum name for an HTTP Bearer connection (likely needs UI inspection — the URL when creating shows it).
2. Whether `http:ActionSendData` accepts `__IMTCONN__` directly, OR whether the 19 modules need to be switched to `http:ActionSendDataAuth` / `http2:…` first.

Then:
- `create_connection.py --conn-id <N>` accepts a manually-created connection id and writes the artifact.
- Build the transform on top (modeled on `fixes/bug2_4_5_make/build_blueprint.py` patterns).

`create_connection.py` retains the API-first attempt + UI fallback path for when this is revisited.
