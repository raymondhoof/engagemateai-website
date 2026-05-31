import { Hono } from 'hono'
import { Bindings, ToolPayload } from '../lib/vapi'
import { handleUncertain } from '../personas/uncertain'
import { handleProspect } from '../personas/prospect'
import { handleTenant } from '../personas/tenant'
import { handleOwner } from '../personas/owner'
import { handleVendor } from '../personas/vendor'
import { logger } from '../lib/log'
import { sendAlert, fmtKv } from '../lib/notify'
import { autoHealIfNeeded } from '../lib/auto-heal'
import { diagnoseError } from '../lib/diagnose'

const escHtml = (s: string) =>
  s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!))

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
const fp = (s: string | undefined | null): string =>
  s ? `${s.slice(0, 4)}…${s.slice(-4)}@${s.length}` : 'MISSING'

personaIntent.post('/persona-intent', async (c) => {
  // Vapi apiRequest bug: emits x-vapi-secret twice (empty + value); Cloudflare
  // Fetch API joins multi-value headers as ", value". Strip the leading ", " if present.
  const rawSecret = c.req.header('x-vapi-secret')
  const sentSecret = rawSecret?.startsWith(', ') ? rawSecret.slice(2) : rawSecret
  const expected = c.env.VAPI_WEBHOOK_SECRET
  // Capture all x-* + cf-* headers on auth failure to identify unknown callers.
  // Redacted: only logs header names + lengths, never raw values.
  const allHeadersRedacted: Record<string, string> = {}
  c.req.raw.headers.forEach((v, k) => {
    if (/^(x-|cf-)/.test(k)) allHeadersRedacted[k] = `len=${v.length}`
  })
  const probeRecord = {
    ts: new Date().toISOString(),
    ip: c.req.header('cf-connecting-ip') ?? 'unknown',
    ua: c.req.header('user-agent') ?? 'unknown',
    sentFp: fp(sentSecret),
    expectedFp: fp(expected),
    match: sentSecret === expected,
    hasHeader: sentSecret !== undefined,
    contentType: c.req.header('content-type') ?? 'unknown',
    allHeadersRedacted,
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
    c.executionCtx.waitUntil(
      sendAlert(c.env, {
        event: 'auth.unauthorized',
        ip: probeRecord.ip,
        subject: `auth.unauthorized @ ${probeRecord.ts}`,
        bodyHtml: `<p>Worker rejected a /persona-intent request with 401.</p><ul>${fmtKv(probeRecord)}</ul><p>Inspect history: <code>python fixes/auth_probes_history.py --fails-only</code></p>`,
      }),
    )
    // Secret present but wrong (drift) — attempt auto-heal after threshold. Missing header = external probe, skip.
    if (sentSecret !== undefined) {
      c.executionCtx.waitUntil(autoHealIfNeeded(c.env, probeRecord.ip))
    }
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
    const issues = parsed.error.flatten()
    logger.warn('payload.invalid', { issues })
    c.executionCtx.waitUntil(
      (async () => {
        const diagnosis = await diagnoseError(c.env, { event: 'payload.invalid', issues })
        const diagHtml = diagnosis ? `<hr><p><strong>AI Diagnosis:</strong> ${escHtml(diagnosis)}</p>` : ''
        await sendAlert(c.env, {
          event: 'payload.invalid',
          ip: probeRecord.ip,
          subject: `payload.invalid @ ${probeRecord.ts}`,
          bodyHtml: `<p>Worker rejected a /persona-intent request with 400 (malformed body).</p><ul>${fmtKv({ ip: probeRecord.ip, ua: probeRecord.ua, issues: JSON.stringify(issues) })}</ul>${diagHtml}`,
        })
      })(),
    )
    return c.json({ error: 'invalid_payload', issues }, 400)
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
    c.executionCtx.waitUntil(
      (async () => {
        const diagnosis = await diagnoseError(c.env, {
          event: 'handler.failed',
          persona: payload.persona,
          error,
          callerPhone: payload.caller_phone,
        })
        const diagHtml = diagnosis ? `<hr><p><strong>AI Diagnosis:</strong> ${escHtml(diagnosis)}</p>` : ''
        await sendAlert(c.env, {
          event: 'handler.failed',
          ip: probeRecord.ip,
          subject: `handler.failed (${payload.persona}) @ ${probeRecord.ts}`,
          bodyHtml: `<p>A persona handler threw — Worker returned 500.</p><ul>${fmtKv({ persona: payload.persona, intent: payload.intent ?? '(none)', callerPhone: payload.caller_phone, error })}</ul>${diagHtml}`,
        })
      })(),
    )
    return c.json({ error: 'internal_error' }, 500)
  }
})
