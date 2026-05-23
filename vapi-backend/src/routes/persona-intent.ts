import { Hono } from 'hono'
import { Bindings, ToolPayload } from '../lib/vapi'
import { handleUncertain } from '../personas/uncertain'
import { handleProspect } from '../personas/prospect'
import { handleTenant } from '../personas/tenant'
import { handleOwner } from '../personas/owner'
import { handleVendor } from '../personas/vendor'
import { logger } from '../lib/log'

export const personaIntent = new Hono<{ Bindings: Bindings }>()

/**
 * POST /persona-intent
 *
 * Single entry point that replaces the Make webhook
 * https://hook.us2.make.com/wu4hqmulcbt5nxcqvqgcnk6m3l81iger.
 *
 * Vapi tool config sends one of 5 personas in the body. We auth with a shared
 * secret (the Make webhook is unauthenticated; we don't replicate that), then
 * dispatch to the persona handler.
 */
// TEMP DIAGNOSTIC (claude 2026-05-22): fingerprint header vs env to locate the BUG-3 mismatch.
// Never logs raw secret. Remove this `fp` + the auth.probe log line once cause is identified.
const fp = (s: string | undefined | null): string =>
  s ? `${s.slice(0, 4)}…${s.slice(-4)}@${s.length}` : 'MISSING'

personaIntent.post('/persona-intent', async (c) => {
  const sentSecret = c.req.header('x-vapi-secret')
  const expected = c.env.VAPI_WEBHOOK_SECRET
  const probeRecord = {
    ts: new Date().toISOString(),
    ip: c.req.header('cf-connecting-ip') ?? 'unknown',
    ua: c.req.header('user-agent') ?? 'unknown',
    sentFp: fp(sentSecret),
    expectedFp: fp(expected),
    match: sentSecret === expected,
    hasHeader: sentSecret !== undefined,
    contentType: c.req.header('content-type') ?? 'unknown',
  }
  logger.info('auth.probe', probeRecord)
  // Non-blocking KV write so persistent diagnosis survives wrangler-tail window.
  // Key: probe:<ISO ts>:<short rand> ensures uniqueness + chronological-ish sort.
  // 14-day TTL keeps free tier comfortable (1k writes/day; we expect <100/day).
  const key = `probe:${probeRecord.ts}:${Math.random().toString(36).slice(2, 8)}`
  c.executionCtx.waitUntil(
    c.env.AUTH_PROBES.put(key, JSON.stringify(probeRecord), { expirationTtl: 14 * 24 * 3600 })
      .catch((err) => logger.warn('auth.probe.kv_write_failed', { err: String(err) })),
  )
  if (!sentSecret || sentSecret !== expected) {
    logger.warn('auth.unauthorized', { ip: c.req.header('cf-connecting-ip') ?? 'unknown' })
    return c.json({ error: 'unauthorized' }, 401)
  }

  let raw: unknown
  try {
    raw = await c.req.json()
  } catch {
    return c.json({ error: 'invalid_json' }, 400)
  }

  const parsed = ToolPayload.safeParse(raw)
  if (!parsed.success) {
    logger.warn('payload.invalid', { issues: parsed.error.flatten() })
    return c.json({ error: 'invalid_payload', issues: parsed.error.flatten() }, 400)
  }
  const payload = parsed.data

  logger.info('request.received', {
    persona: payload.persona,
    intent: payload.intent ?? null,
    callerPhone: payload.caller_phone,
  })

  try {
    switch (payload.persona) {
      case 'uncertain':
        return await handleUncertain(c, payload)
      case 'prospect':
        return await handleProspect(c, payload)
      case 'tenant':
        return await handleTenant(c, payload)
      case 'owner':
        return await handleOwner(c, payload)
      case 'vendor':
        return await handleVendor(c, payload)
    }
  } catch (err) {
    const error = err instanceof Error ? err.message : String(err)
    logger.error('handler.failed', { persona: payload.persona, error })
    return c.json({ error: 'internal_error' }, 500)
  }
})
