import { logger } from './log'
import type { Bindings } from './vapi'

/**
 * Send an alert email via Resend. Non-blocking — caller wraps in ctx.waitUntil.
 * Throttled per (event, ip) to avoid spam during loops/outages: at most 1
 * email per key per 5 min, tracked in the AUTH_PROBES KV namespace under
 * the prefix `alert:lock:<event>:<ip>`.
 */
export async function sendAlert(
  env: Bindings,
  args: { event: string; ip: string; subject: string; bodyHtml: string },
): Promise<void> {
  if (!env.RESEND_API_KEY || !env.ALERT_EMAIL_TO || !env.ALERT_EMAIL_FROM) {
    // Pre-deploy / unset: log only. Worker continues normally.
    logger.warn('alert.skipped_unconfigured', {
      event: args.event,
      hasKey: !!env.RESEND_API_KEY,
      hasTo: !!env.ALERT_EMAIL_TO,
      hasFrom: !!env.ALERT_EMAIL_FROM,
    })
    return
  }
  const lockKey = `alert:lock:${args.event}:${args.ip}`
  const existing = await env.AUTH_PROBES.get(lockKey)
  if (existing) {
    logger.info('alert.throttled', { event: args.event, ip: args.ip })
    return
  }
  // claim the throttle slot first (5 min)
  await env.AUTH_PROBES.put(lockKey, '1', { expirationTtl: 300 })

  const subject = `[ELLIE-PROD] ${args.subject}`
  const body = {
    from: env.ALERT_EMAIL_FROM,
    to: [env.ALERT_EMAIL_TO],
    subject,
    html: args.bodyHtml,
  }
  try {
    const resp = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${env.RESEND_API_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    })
    if (!resp.ok) {
      const text = await resp.text()
      logger.error('alert.send_failed', { status: resp.status, body: text.slice(0, 200) })
    } else {
      logger.info('alert.sent', { event: args.event, subject })
    }
  } catch (err) {
    logger.error('alert.send_exception', { err: String(err) })
  }
}

export function fmtKv(o: Record<string, unknown>): string {
  return Object.entries(o)
    .map(([k, v]) => {
      const display = v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v)
      return `<li><b>${k}</b>: <code>${escapeHtml(display)}</code></li>`
    })
    .join('')
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!),
  )
}
