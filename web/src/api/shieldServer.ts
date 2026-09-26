/**
 * Client for the free, self-hosted, single-org Shield server's admin API
 * (Phase 1.5, `internal_doc/shield_msp_plan.md`). Distinct from `shield.ts`
 * (the per-Mac Shield settings UI) and from `client.ts`/`schemas.ts` (the
 * Coriqo-style fleet console, which talks to a different backend on a
 * different port). This module owns exactly the admin-token-gated calls to
 * `/api/v1/shield/*`.
 *
 * Same discipline as `client.ts`: every response is validated with zod before
 * a caller ever sees it, and a 401 is surfaced as its own typed error so the
 * UI can prompt for the admin token rather than rendering a generic failure.
 */
import { z } from 'zod'

/* ------------------------------------------------------------------ *
 * Schemas — the admin API contract, Phase 1.5 section of the plan doc.
 * ------------------------------------------------------------------ */

export const ShieldServerMode = z.enum(['observe', 'redact', 'block'])
export type ShieldServerMode = z.infer<typeof ShieldServerMode>

/** Same set the device's own Settings screen offers (`shield.ts`). */
export const RETENTION_DAYS = [7, 30, 90, 365] as const
export const RetentionDays = z.union([
  z.literal(7),
  z.literal(30),
  z.literal(90),
  z.literal(365),
])

/** Known lockable keys — server validates the same set (`shield.apply_policy_update`). */
export const LOCKABLE_KEYS = ['mode', 'apps', 'keep_text', 'retention_days', 'notice'] as const
export type LockableKey = (typeof LOCKABLE_KEYS)[number]

export const ShieldServerPolicyDoc = z.object({
  mode: ShieldServerMode,
  apps: z.record(z.boolean()),
  keep_text: z.boolean(),
  retention_days: RetentionDays,
  notice: z.boolean(),
})
export type ShieldServerPolicyDoc = z.infer<typeof ShieldServerPolicyDoc>

/** The signed envelope exactly as the server returns and stores it:
 * `{document, signature}`, or `null` when no policy has ever been set for
 * this key (tenant default / device override). `document.policy === null`
 * is the OTHER "unmanaged" case — a signed "no longer managed" tombstone,
 * still carrying a real `version`/`signature` an admin can audit. Both
 * `null` states are folded into the flat view below, which is what every
 * component actually reads. */
export const ShieldServerPolicyDocument = z.object({
  policy_id: z.string(),
  version: z.number().int(),
  tenant_slug: z.string().nullable(),
  device_id: z.string().nullable(),
  issued_at: z.string(),
  key_id: z.string(),
  managed_by: z.string(),
  policy: ShieldServerPolicyDoc.nullable(),
  locked: z.array(z.string()),
})
export type ShieldServerPolicyDocument = z.infer<typeof ShieldServerPolicyDocument>

export const ShieldServerRawEnvelope = z
  .object({ document: ShieldServerPolicyDocument, signature: z.string() })
  .nullable()
export type ShieldServerRawEnvelope = z.infer<typeof ShieldServerRawEnvelope>

/** The flat shape every policy-editing component actually reads, derived
 * from the raw envelope by {@link deriveEnvelopeView}. Never fetched or PUT
 * directly — see the raw schema above for what's actually on the wire. */
export const ShieldServerPolicyEnvelope = z.object({
  version: z.number().int().nonnegative().nullable(),
  key_id: z.string().nullable(),
  policy: ShieldServerPolicyDoc.nullable(),
  locked: z.array(z.string()),
  managed_by: z.string().nullable(),
  issued_at: z.string().nullable(),
  /** Present so an admin can audit what was actually signed; `null` only
   * when this key has never had a policy set at all. */
  signature: z.string().nullable(),
})
export type ShieldServerPolicyEnvelope = z.infer<typeof ShieldServerPolicyEnvelope>

export function deriveEnvelopeView(raw: ShieldServerRawEnvelope): ShieldServerPolicyEnvelope {
  if (raw === null) {
    return {
      version: null, key_id: null, policy: null, locked: [], managed_by: null,
      issued_at: null, signature: null,
    }
  }
  const { document, signature } = raw
  return {
    version: document.version,
    key_id: document.key_id,
    policy: document.policy,
    locked: document.locked,
    managed_by: document.managed_by,
    issued_at: document.issued_at,
    signature,
  }
}

export const ShieldServerDevice = z.object({
  device_id: z.string(),
  label: z.string().nullable(),
  enrolled_at: z.string(),
  last_seen: z.string().nullable(),
  /** `null` when this device has never reported a `shield` block at all —
   * distinct from `false` (it reported, and said it isn't protecting). The
   * UI shows "not heard from yet" for `null`. */
  protecting: z.boolean().nullable(),
  /** Plain-language reasons behind the protecting/quiet chip — never blank
   * when protecting is false, per the plan's "reasons" requirement. */
  reasons: z.array(z.string()),
  mode: ShieldServerMode.nullable(),
  policy_version: z.number().int().nullable(),
  assigned_version: z.number().int().nullable(),
  drift: z.boolean(),
  revoked: z.boolean(),
})
export type ShieldServerDevice = z.infer<typeof ShieldServerDevice>

export const ShieldServerDeviceList = z.object({
  devices: z.array(ShieldServerDevice),
})
export type ShieldServerDeviceList = z.infer<typeof ShieldServerDeviceList>

export const ShieldServerDrift = z.object({
  devices: z.array(ShieldServerDevice),
})

export const ShieldServerToken = z.object({
  token_id: z.string(),
  label: z.string(),
  created_at: z.string(),
  expires_at: z.string().nullable(),
  revoked: z.boolean(),
})
export type ShieldServerToken = z.infer<typeof ShieldServerToken>

/** Only present the one time the token is minted — never re-served. */
export const ShieldServerTokenMinted = ShieldServerToken.extend({
  token: z.string(),
})
export type ShieldServerTokenMinted = z.infer<typeof ShieldServerTokenMinted>

export const ShieldServerTokenList = z.object({
  tokens: z.array(ShieldServerToken),
})

export const ShieldServerInfo = z.object({
  public_url: z.string(),
  org: z.string(),
  key_id: z.string(),
})
export type ShieldServerInfo = z.infer<typeof ShieldServerInfo>

/* ------------------------------------------------------------------ *
 * Errors
 * ------------------------------------------------------------------ */

export class ShieldServerAuthError extends Error {
  readonly kind = 'auth'
  constructor(readonly url: string) {
    super(`Admin token missing or rejected for ${url}`)
    this.name = 'ShieldServerAuthError'
  }
}

export class ShieldServerHttpError extends Error {
  readonly kind = 'http'
  constructor(readonly url: string, readonly status: number, readonly body: string) {
    super(`${status} from ${url}`)
    this.name = 'ShieldServerHttpError'
  }
}

export class ShieldServerSchemaError extends Error {
  readonly kind = 'schema'
  constructor(readonly url: string, readonly issues: readonly z.ZodIssue[]) {
    super(`Response from ${url} did not match the shield server admin API contract`)
    this.name = 'ShieldServerSchemaError'
  }
}

/* ------------------------------------------------------------------ *
 * Admin token storage — sessionStorage only, never persisted longer.
 * ------------------------------------------------------------------ */

const TOKEN_KEY = 'byoai.shieldServer.adminToken'

/** The in-memory fallback the comments above always claimed to have, now
 * actually written to. `sessionStorage` can throw on every call in a private
 * window or with site data blocked — when it does, the token must still work
 * for the rest of THIS session (surviving a re-render, a route change, a
 * remount), or the admin loops on 401 forever after typing it in once. This
 * is written unconditionally, `sessionStorage` is tried in addition (best
 * effort, for surviving a reload), and reads check memory first. */
let inMemoryAdminToken: string | null = null

/** Every access is wrapped: a private window, blocked storage, or a disabled
 * API must degrade to "no token remembered", never throw into render. */
export function getStoredAdminToken(): string | null {
  if (inMemoryAdminToken !== null) return inMemoryAdminToken
  try {
    return sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setStoredAdminToken(token: string): void {
  inMemoryAdminToken = token
  try {
    sessionStorage.setItem(TOKEN_KEY, token)
  } catch {
    // Best effort: the token still works for this session via the in-memory
    // cache above; only persistence across a reload is lost.
  }
}

export function clearStoredAdminToken(): void {
  inMemoryAdminToken = null
  try {
    sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    // Nothing to clean up if storage was never reachable.
  }
}

/* ------------------------------------------------------------------ *
 * Fetch wrapper
 * ------------------------------------------------------------------ */

const ADMIN_BASE = import.meta.env.VITE_SHIELD_SERVER_API_BASE ?? '/api/v1/shield'

export interface AdminRequestOptions {
  readonly method?: 'GET' | 'PUT' | 'POST' | 'DELETE'
  readonly body?: unknown
  readonly signal?: AbortSignal
}

async function adminFetch<S extends z.ZodTypeAny>(
  path: string,
  schema: S,
  options: AdminRequestOptions = {},
): Promise<z.infer<S>> {
  const url = `${ADMIN_BASE}${path.startsWith('/') ? path : `/${path}`}`
  const token = getStoredAdminToken()
  const response = await fetch(url, {
    method: options.method ?? 'GET',
    headers: {
      Accept: 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  })

  if (response.status === 401) {
    throw new ShieldServerAuthError(url)
  }

  const text = await response.text()

  if (!response.ok) {
    throw new ShieldServerHttpError(url, response.status, text)
  }

  // DELETE endpoints may answer 200 with no body or with the envelope; guard
  // the empty-body case rather than let JSON.parse('') throw.
  const json: unknown = text.length === 0 ? null : JSON.parse(text)
  const result = schema.safeParse(json)
  if (!result.success) {
    throw new ShieldServerSchemaError(url, result.error.issues)
  }
  return result.data as z.infer<S>
}

export function isAuthError(e: unknown): e is ShieldServerAuthError {
  return e instanceof ShieldServerAuthError
}

/* ------------------------------------------------------------------ *
 * Calls
 * ------------------------------------------------------------------ */

export function fetchDevices(signal?: AbortSignal) {
  return adminFetch('/devices', ShieldServerDeviceList, { signal })
}

export function revokeDevice(deviceId: string) {
  return adminFetch(`/devices/${encodeURIComponent(deviceId)}`, z.unknown(), { method: 'DELETE' })
}

export function fetchDrift(signal?: AbortSignal) {
  return adminFetch('/policy/drift', ShieldServerDrift, { signal })
}

/** Flattens the PolicyDoc + locked keys the form edits, plus the `managed_by`
 * label the server's PUT requires, into exactly the flat body
 * `_policy_body` on the backend reads (`{mode, apps, keep_text,
 * retention_days, notice, managed_by, locked}`) — NOT `{policy, locked}`. */
function policyPutBody(next: { policy: ShieldServerPolicyDoc; locked: string[] }, managedBy: string) {
  return { ...next.policy, managed_by: managedBy, locked: next.locked }
}

async function fetchPolicyEnvelope(path: string, signal?: AbortSignal): Promise<ShieldServerPolicyEnvelope> {
  const raw = await adminFetch(path, ShieldServerRawEnvelope, { signal })
  return deriveEnvelopeView(raw)
}

async function putPolicyEnvelope(path: string, body: unknown): Promise<ShieldServerPolicyEnvelope> {
  const raw = await adminFetch(path, ShieldServerRawEnvelope, { method: 'PUT', body })
  return deriveEnvelopeView(raw)
}

async function deletePolicyEnvelope(path: string): Promise<ShieldServerPolicyEnvelope> {
  const raw = await adminFetch(path, ShieldServerRawEnvelope, { method: 'DELETE' })
  return deriveEnvelopeView(raw)
}

export function fetchDefaultPolicy(signal?: AbortSignal) {
  return fetchPolicyEnvelope('/policy', signal)
}

export function saveDefaultPolicy(
  next: { policy: ShieldServerPolicyDoc; locked: string[] },
  managedBy: string,
) {
  return putPolicyEnvelope('/policy', policyPutBody(next, managedBy))
}

/** Stops managing the tenant default: devices leave managed mode. */
export function deleteDefaultPolicy() {
  return deletePolicyEnvelope('/policy')
}

export function fetchDevicePolicy(deviceId: string, signal?: AbortSignal) {
  return fetchPolicyEnvelope(`/devices/${encodeURIComponent(deviceId)}/policy`, signal)
}

export function saveDevicePolicy(
  deviceId: string,
  next: { policy: ShieldServerPolicyDoc; locked: string[] },
  managedBy: string,
) {
  return putPolicyEnvelope(
    `/devices/${encodeURIComponent(deviceId)}/policy`,
    policyPutBody(next, managedBy),
  )
}

/** Falls the device back to the tenant default. */
export function deleteDevicePolicy(deviceId: string) {
  return deletePolicyEnvelope(`/devices/${encodeURIComponent(deviceId)}/policy`)
}

export function fetchTokens(signal?: AbortSignal) {
  return adminFetch('/tokens', ShieldServerTokenList, { signal })
}

export function mintToken(next: { label: string; expires_at: string | null }) {
  return adminFetch('/tokens', ShieldServerTokenMinted, { method: 'POST', body: next })
}

export function revokeToken(tokenId: string) {
  return adminFetch(`/tokens/${encodeURIComponent(tokenId)}`, z.unknown(), { method: 'DELETE' })
}

export function fetchServerInfo(signal?: AbortSignal) {
  return adminFetch('/info', ShieldServerInfo, { signal })
}

/* ------------------------------------------------------------------ *
 * Client-side validation mirroring the server rules named in the plan
 * (`shield.apply_policy_update`): mode is one of the three, retention_days is
 * one of the four allowed buckets, apps only contains booleans (zod already
 * enforces the value type; this catches the case a caller assembles a bad
 * document before it ever reaches the network).
 * ------------------------------------------------------------------ */

export interface PolicyValidationError {
  readonly field: string
  readonly message: string
}

export function validatePolicyDoc(doc: ShieldServerPolicyDoc): PolicyValidationError[] {
  const errors: PolicyValidationError[] = []
  if (!ShieldServerMode.safeParse(doc.mode).success) {
    errors.push({ field: 'mode', message: 'Mode must be observe, redact, or block.' })
  }
  if (!RETENTION_DAYS.includes(doc.retention_days as (typeof RETENTION_DAYS)[number])) {
    errors.push({
      field: 'retention_days',
      message: 'Retention must be 7, 30, 90, or 365 days.',
    })
  }
  if (Object.keys(doc.apps).length === 0) {
    errors.push({ field: 'apps', message: 'At least one app must be listed.' })
  }
  return errors
}

export function validateLockedKeys(locked: readonly string[]): PolicyValidationError[] {
  const unknown = locked.filter((k) => !LOCKABLE_KEYS.includes(k as LockableKey))
  return unknown.length > 0
    ? [{ field: 'locked', message: `Unknown lockable key(s): ${unknown.join(', ')}` }]
    : []
}
