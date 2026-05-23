import { z } from 'zod'

export const PERSONAS = ['prospect', 'tenant', 'owner', 'vendor', 'uncertain'] as const
export type Persona = (typeof PERSONAS)[number]

/**
 * Tool-call payload as Vapi sends it to the Make webhook today.
 * Mirrors the field names used by Make scenario v4 module 1 (e.g. {{1.caller_phone}}).
 *
 * Unknown fields are passed through (`.passthrough()`) so we don't reject
 * future Vapi-side additions silently.
 */
export const ToolPayload = z
  .object({
    caller_name: z.string().optional(),
    caller_phone: z.string().min(1, 'caller_phone required'),
    persona: z.enum(PERSONAS),
    intent: z.string().optional(),
    property_interest: z.string().optional(),
    notes: z.string().optional(),
  })
  .passthrough()

export type ToolPayload = z.infer<typeof ToolPayload>

/**
 * Cloudflare Worker bindings — secrets + non-secret vars.
 * The shape mirrors `wrangler.toml [vars]` + `wrangler secret put` names.
 */
export interface Bindings {
  // Secrets
  GHL_PRIVATE_TOKEN: string
  APPFOLIO_CLIENT_ID: string
  APPFOLIO_SECRET: string
  VAPI_WEBHOOK_SECRET: string
  // Non-secret vars (in wrangler.toml)
  GHL_LOCATION_ID: string
  APPFOLIO_BASE_URL: string
  // KV — BUG-3 diagnosis log (auth.probe history). Reads via CF API.
  AUTH_PROBES: KVNamespace
}
