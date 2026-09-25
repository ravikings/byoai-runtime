/**
 * Coriqo shield (capture ledger) client — the broker between the browser-side
 * trust model and the capture backend. It sticks to the console's existing
 * API conventions: typed, schema-checked responses, explicit states per error
 * kind, no silent fallbacks. The shield itself lives on the Python side
 * (`byoai.integrations.shield`), proxied at `/shield-api` from Vite and
 * `/console/{tenant}/shield` in production.
 */
import { z } from 'zod'

/* ----------------------------------------------------------------- schema */

export const ShieldFlag = z.object({
  tier: z.enum(['agent', 'high', 'pii', 'conduct']),
  rule: z.string().min(1),
})

export const ShieldItem = z.object({
  id: z.string().min(1),
  source: z.enum(['mcp', 'desktop']),
  surface: z.string().min(1),
  ts: z.string(),
  date: z.string(),
  verb: z.string(),
  reply: z.string().nullish(),
  status: z.enum(['answered', 'running', 'failed', 'blocked']),
  verdict: z.string().nullish(),
  chars: z.number().nullish(),
  redactions: z.array(z.string()).nullish(),
  tier: z.enum(['ok', 'warn', 'bad']),
  flags: z.array(ShieldFlag),
  response_stream: z.boolean().optional(),
  tool: z.string().nullish(),
  identity: z.record(z.unknown()).nullish(),
  usage: z.record(z.unknown()).nullish(),
  latency_ms: z.number().nullish(),
  seal: z.string().min(1),
})

export const ShieldFeed = z.object({
  counts: z.object({
    checked: z.number().int().nonnegative(),
    caught: z.number().int().nonnegative(),
    high_risk: z.number().int().nonnegative(),
  }),
  total: z.number().int(),
  offset: z.number().int(),
  limit: z.number().int(),
  has_more: z.boolean(),
  items: z.array(ShieldItem),
})

export const ShieldCheckpoint = z.object({
  root: z.string().min(1).nullish(),
  height: z.number().int(),
  device_id: z.string().min(1).nullish(),
  sig_valid: z.boolean().nullish(),
})

export const ShieldVerify = z.object({
  tamper_evident: z.boolean(),
  entries: z.number().int().nullish(),
  broken_at: z.number().int().nullish(),
  reason: z.string().nullish(),
  merkle_root: z.string().nullish(),
  checkpoint: ShieldCheckpoint.nullish(),
})

export const ShieldReceipt = z.object({
  kind: z.literal('byoai.receipt.v2'),
  seal: z.string(),
  height: z.number(),
  payload: z.record(z.unknown()),
  merkle: z.object({
    proof: z.object({
      leaf_index: z.number(),
      leaf_hash: z.string(),
      steps: z.array(z.object({ sibling: z.string(), side: z.string() })).nullish(),
      root_hex: z.string(),
    }),
    checkpoint: z.record(z.unknown()).nullish(),
  }),
  self_check: z.object({
    entry_hash_ok: z.boolean(),
    chain_intact: z.boolean(),
    chain_height: z.number(),
    verified_at: z.string(),
  }),
})

export async function fetchReceipt(seal: string) {
  const res = await fetch(`/shield-api/receipt/${seal}`, { cache: 'no-store' })
  if (!res.ok) throw new Error(`${res.status} receipt ${seal}`)
  return ShieldReceipt.parse(await res.json())
}

export const RETENTION_DAYS = [7, 30, 90, 365] as const

export const ShieldPolicy = z.object({
  mode: z.enum(['observe', 'redact', 'block']),
  apps: z.record(z.boolean()),
  keep_text: z.boolean(),
  retention_days: z.number().int().positive(),
  notice: z.boolean(),
  /** Apps the capture proxy can read. Served, not saved: the others can't
   * be turned on yet. */
  covered_apps: z.array(z.string()).optional(),
})
export type ShieldPolicy = z.infer<typeof ShieldPolicy>

/** What Shield holds on disk right now — read from the ledger, not assumed. */
export const ShieldPrivacy = z.object({
  ledger_rows: z.number().int().nonnegative(),
  rows_with_text: z.number().int().nonnegative(),
  oldest_row: z.string().nullable(),
  rows_past_retention: z.number().int().nonnegative(),
  sealed_with_text: z.number().int().nonnegative(),
  keep_text: z.boolean(),
  retention_days: z.number().int(),
  mode: z.enum(['observe', 'redact', 'block']),
  less_private: z.boolean().optional(),
  ledger_path: z.string().nullish(),
  seal_path: z.string().nullish(),
  device_id: z.string().nullish(),
})
export type ShieldPrivacy = z.infer<typeof ShieldPrivacy>

export const ShieldScrub = ShieldPrivacy.extend({
  scrubbed: z.number().int().nonnegative(),
  deleted: z.number().int().nonnegative(),
})

/* ------------------------------------------------------------------ calls */

export interface ShieldPage {
  /** Treated as an opaque page number (`0`, `1`, …) — the shield server
   * clamps at 200 rows/page and reports back what it actually served. */
  page: number
  perPage: number
}

async function get<T>(path: string, schema: z.ZodType<T>): Promise<T> {
  const res = await fetch(`/shield-api${path}`, { cache: 'no-store' })
  if (!res.ok) throw new Error(`${res.status} from shield (${path})`)
  return schema.parse(await res.json())
}

export function fetchFeed(page: ShieldPage) {
  const qs = `/feed?limit=${Math.min(page.perPage, 200)}&offset=${page.page * page.perPage}`
  return get(qs, ShieldFeed)
}

export function fetchVerify() {
  return get('/verify', ShieldVerify)
}

export function fetchPolicy() {
  return get('/policy', ShieldPolicy)
}

async function post<T>(path: string, body: unknown, schema: z.ZodType<T>): Promise<T> {
  const res = await fetch(`/shield-api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  const json: unknown = await res.json().catch(() => ({}))
  if (!res.ok) {
    const msg = (json as { error?: string }).error
    throw new Error(msg ?? `${res.status} from shield (${path})`)
  }
  return schema.parse(json)
}

/** `acknowledge` is required by the server for any change that makes Shield
 * less private than it is now (record-only mode, stored previews). */
export function savePolicy(next: Partial<ShieldPolicy> & { acknowledge?: 'less_private' }) {
  return post('/policy', next, ShieldPolicy)
}

export function fetchPrivacy() {
  return get('/privacy', ShieldPrivacy)
}

export const ShieldCoriqo = z.object({
  app_url: z.string().nullish(),
  tenant: z.string().nullish(),
  marketing_url: z.string().nullish(),
  configured: z.boolean(),
})

export function fetchCoriqo() {
  return get('/coriqo', ShieldCoriqo)
}

/** Save the Coriqo connection for this Mac. Blank fields keep what's saved;
 * environment variables still win over the file. */
export function saveCoriqo(next: { app_url?: string; api_key?: string; tenant?: string }) {
  return post('/coriqo', next, ShieldCoriqo)
}

/** Which of the known AI apps are installed on this Mac (read from
 * /Applications on each call). */
export function fetchInstalledApps() {
  return get('/apps', z.record(z.boolean()))
}

const PublishResult = z.object({
  shipped: z.boolean(),
  root: z.string().nullish(),
  height: z.number().nullish(),
  error: z.string().nullish(),
  how: z.string().nullish(),
})

/** Ship this Mac's current seal (root + signed checkpoint) to Coriqo. Throws
 * with the server's own explanation when it wasn't shipped. */
export async function publishSeal() {
  // JSON even with nothing to say: the shield refuses other POSTs (see
  // request_problem in shield.py).
  const res = await fetch('/shield-api/publish', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
  })
  const body = PublishResult.safeParse(await res.json().catch(() => ({})))
  if (!res.ok || !body.success || !body.data.shipped) {
    const d = body.success ? body.data : undefined
    throw new Error(d?.how ?? d?.error ?? `${res.status} publishing the seal`)
  }
  return body.data
}

/** Replace stored message text with fingerprints and apply retention. */
export function scrubStoredText() {
  return post('/privacy/scrub', {}, ShieldScrub)
}


export type ShieldVerify = z.infer<typeof ShieldVerify>
