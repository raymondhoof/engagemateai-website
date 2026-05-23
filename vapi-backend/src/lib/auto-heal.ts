import { logger } from './log'
import { sendAlert, fmtKv } from './notify'
import type { Bindings } from './vapi'

const MISMATCH_THRESHOLD = 3
const COUNT_TTL_SECS = 7200 // 2h window, reset after heal

/**
 * Called on every auth mismatch (hasHeader=true, match=false).
 * Increments an hourly KV counter. Once threshold is hit, patches
 * the x-vapi-secret value on all PERSONA_INTENT_TOOL_IDS to match
 * the Worker's current VAPI_WEBHOOK_SECRET.
 */
export async function autoHealIfNeeded(env: Bindings, ip: string): Promise<void> {
  if (!env.VAPI_API_KEY || !env.PERSONA_INTENT_TOOL_IDS) {
    logger.warn('auto_heal.skipped_unconfigured', {
      hasApiKey: !!env.VAPI_API_KEY,
      hasToolIds: !!env.PERSONA_INTENT_TOOL_IDS,
    })
    return
  }

  const hourKey = `mismatch:count:${new Date().toISOString().slice(0, 13)}`
  const current = await env.AUTH_PROBES.get(hourKey)
  const count = (parseInt(current ?? '0') || 0) + 1
  await env.AUTH_PROBES.put(hourKey, String(count), { expirationTtl: COUNT_TTL_SECS })

  logger.info('auth.mismatch_count', { count, threshold: MISMATCH_THRESHOLD })

  if (count < MISMATCH_THRESHOLD) return

  const toolIds = env.PERSONA_INTENT_TOOL_IDS.split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  const newSecret = env.VAPI_WEBHOOK_SECRET
  const fp = (s: string) => `${s.slice(0, 4)}…${s.slice(-4)}@${s.length}`
  const results: Record<string, string> = {}

  for (const toolId of toolIds) {
    try {
      const resp = await fetch(`https://api.vapi.ai/tool/${toolId}`, {
        method: 'PATCH',
        headers: {
          Authorization: `Bearer ${env.VAPI_API_KEY}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          headers: {
            type: 'object',
            properties: {
              'x-vapi-secret': { type: 'string', value: newSecret },
            },
          },
        }),
      })
      if (resp.ok) {
        results[toolId] = `ok(${resp.status})`
        logger.info('auto_heal.patch_ok', { toolId })
      } else {
        const text = await resp.text()
        results[toolId] = `fail(${resp.status})`
        logger.error('auto_heal.patch_failed', { toolId, status: resp.status, body: text.slice(0, 200) })
      }
    } catch (err) {
      results[toolId] = `exception`
      logger.error('auto_heal.patch_exception', { toolId, err: String(err) })
    }
  }

  const allOk = Object.values(results).every((r) => r.startsWith('ok'))

  // Reset counter so a single bad call after healing doesn't re-trigger immediately
  if (allOk) {
    await env.AUTH_PROBES.delete(hourKey)
  }

  await sendAlert(env, {
    event: allOk ? 'auth.auto_healed' : 'auth.auto_heal_failed',
    ip,
    subject: allOk
      ? `auto_heal: secret re-aligned (${toolIds.length} tool(s))`
      : `auto_heal FAILED — manual fix needed`,
    bodyHtml: `<p>Auto-heal ${allOk ? 'succeeded' : '<b>FAILED</b>'} after ${count} mismatches in the current hour.</p>
<ul>${fmtKv({ ...results, newSecretFp: fp(newSecret) })}</ul>
<p>${allOk ? 'Vapi tools now send the correct secret. Next call should pass.' : 'Check Worker logs. Run <code>python fixes/rotate_secret.py</code> to manually re-align.'}</p>`,
  })
}
