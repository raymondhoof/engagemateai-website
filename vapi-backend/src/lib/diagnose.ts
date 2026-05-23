import { logger } from './log'
import type { Bindings } from './vapi'

const SYSTEM_PROMPT =
  'You are a backend systems expert. When given a Cloudflare Worker error from a Vapi voice AI system, ' +
  'explain in 2-3 plain English sentences what likely caused it and what to check first. ' +
  'No jargon like "payload", "schema", or "endpoint". No preamble. Output only the diagnosis sentences.'

export interface DiagnoseInput {
  event: 'handler.failed' | 'payload.invalid'
  persona?: string
  error?: string
  issues?: unknown
  callerPhone?: string
}

function buildPrompt(input: DiagnoseInput): string {
  if (input.event === 'handler.failed') {
    return `A voice AI persona handler threw an unhandled exception.
Persona: ${input.persona ?? 'unknown'}
Error: ${input.error ?? 'none'}
Caller phone: ${input.callerPhone ?? 'unknown'}

What likely caused this and what should be checked first?`
  }
  return `The voice AI received a tool call with a malformed request body.
Validation errors: ${JSON.stringify(input.issues ?? {})}

What likely caused this and what should be checked first?`
}

export async function diagnoseError(env: Bindings, input: DiagnoseInput): Promise<string | null> {
  if (!env.GROQ_API_KEY) return null
  try {
    const resp = await fetch('https://api.groq.com/openai/v1/chat/completions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${env.GROQ_API_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: 'llama-3.3-70b-versatile',
        messages: [
          { role: 'system', content: SYSTEM_PROMPT },
          { role: 'user', content: buildPrompt(input) },
        ],
        temperature: 0.2,
        max_tokens: 200,
        stream: false,
      }),
    })
    if (!resp.ok) {
      logger.warn('diagnose.groq_failed', { status: resp.status })
      return null
    }
    const data = (await resp.json()) as {
      choices?: Array<{ message?: { content?: string } }>
    }
    return data.choices?.[0]?.message?.content?.trim() ?? null
  } catch (err) {
    logger.warn('diagnose.exception', { err: String(err) })
    return null
  }
}
