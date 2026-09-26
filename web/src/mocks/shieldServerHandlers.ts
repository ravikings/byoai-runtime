/**
 * MSW handlers for the free, self-hosted Shield server admin API.
 * Every route here requires `Authorization: Bearer <ADMIN_TOKEN>` — a missing
 * or wrong token gets 401, exercising the console's admin-token gate in dev
 * and in tests without a real backend. Response shapes are the RAW backend
 * shapes (`{document, signature}` policy envelopes, `token_id`-keyed tokens)
 * — see `shieldServerFixtures.ts`'s header comment for how that's pinned.
 */
import { http, HttpResponse, type HttpHandler } from 'msw'
import { ADMIN_TOKEN, DEFAULT_POLICY, SERVER_INFO, SHIELD_DEVICES, SHIELD_TOKENS } from './shieldServerFixtures'
import type { ShieldServerPolicyDoc, ShieldServerRawEnvelope } from '@/api/shieldServer'

const BASE = import.meta.env.VITE_SHIELD_SERVER_API_BASE ?? '/api/v1/shield'

let devices = SHIELD_DEVICES.map((d) => ({ ...d }))
let tokens = SHIELD_TOKENS.map((t) => ({ ...t }))
let defaultPolicy: ShieldServerRawEnvelope = structuredClone(DEFAULT_POLICY)
const devicePolicies = new Map<string, ShieldServerRawEnvelope>()

/** Tests need a clean slate between cases; MSW's own `resetHandlers` does not
 * reach this module-level mutable state. */
export function resetShieldServerMocks(): void {
  devices = SHIELD_DEVICES.map((d) => ({ ...d }))
  tokens = SHIELD_TOKENS.map((t) => ({ ...t }))
  defaultPolicy = structuredClone(DEFAULT_POLICY)
  devicePolicies.clear()
}

/** Test-only seam: gives a device a real override envelope without going
 * through the UI, for tests that need "Remove override" to already be
 * showable (e.g. the device-switch-resets-the-editor regression test). */
export function seedDeviceOverride(deviceId: string, envelope: ShieldServerRawEnvelope): void {
  devicePolicies.set(deviceId, envelope)
}

/** Test-only seam: simulates the tenant default changing server-side (e.g.
 * another admin's save) without going through this client's own PUT — for
 * tests of the "changed on the server, reload?" banner. */
export function seedDefaultPolicy(envelope: ShieldServerRawEnvelope): void {
  defaultPolicy = envelope
}

function authed(request: Request): boolean {
  return request.headers.get('authorization') === `Bearer ${ADMIN_TOKEN}`
}

let nextVersion = 4

interface PolicyPutBody extends ShieldServerPolicyDoc {
  managed_by: string
  locked: string[]
}

/** Builds a fresh signed-envelope-shaped response from a flat PUT body — the
 * exact shape `_policy_body` on the real backend reads. */
function buildEnvelope(body: PolicyPutBody, deviceId: string | null): ShieldServerRawEnvelope {
  return {
    document: {
      policy_id: `pol_${nextVersion.toString(16).padStart(8, '0')}`,
      version: nextVersion++,
      tenant_slug: 'default',
      device_id: deviceId,
      issued_at: new Date().toISOString(),
      key_id: SERVER_INFO.key_id,
      managed_by: body.managed_by,
      policy: {
        mode: body.mode, apps: body.apps, keep_text: body.keep_text,
        retention_days: body.retention_days, notice: body.notice,
      },
      locked: body.locked ?? [],
    },
    signature: 'f'.repeat(128),
  }
}

function tombstone(deviceId: string | null, managedBy: string): ShieldServerRawEnvelope {
  return {
    document: {
      policy_id: `pol_${nextVersion.toString(16).padStart(8, '0')}`,
      version: nextVersion++,
      tenant_slug: 'default',
      device_id: deviceId,
      issued_at: new Date().toISOString(),
      key_id: SERVER_INFO.key_id,
      managed_by: managedBy,
      policy: null,
      locked: [],
    },
    signature: 'f'.repeat(128),
  }
}

export const shieldServerHandlers: HttpHandler[] = [
  http.get(`${BASE}/devices`, ({ request }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    return HttpResponse.json({ devices })
  }),

  http.delete(`${BASE}/devices/:id`, ({ request, params }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    const d = devices.find((x) => x.device_id === params.id)
    if (d) {
      d.revoked = true
      d.protecting = false
      d.reasons = ['revoked']
    }
    return HttpResponse.json({ revoked: true })
  }),

  http.get(`${BASE}/policy/drift`, ({ request }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    return HttpResponse.json({ devices: devices.filter((d) => d.drift) })
  }),

  http.get(`${BASE}/policy`, ({ request }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    return HttpResponse.json(defaultPolicy)
  }),

  http.put(`${BASE}/policy`, async ({ request }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    const body = (await request.json()) as PolicyPutBody
    defaultPolicy = buildEnvelope(body, null)
    return HttpResponse.json(defaultPolicy)
  }),

  http.delete(`${BASE}/policy`, ({ request }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    defaultPolicy = tombstone(null, '')
    return HttpResponse.json(defaultPolicy)
  }),

  http.get(`${BASE}/devices/:id/policy`, ({ request, params }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    const id = String(params.id)
    return HttpResponse.json(devicePolicies.get(id) ?? null)
  }),

  http.put(`${BASE}/devices/:id/policy`, async ({ request, params }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    const id = String(params.id)
    const body = (await request.json()) as PolicyPutBody
    const envelope = buildEnvelope(body, id)
    devicePolicies.set(id, envelope)
    return HttpResponse.json(envelope)
  }),

  http.delete(`${BASE}/devices/:id/policy`, ({ request, params }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    devicePolicies.delete(String(params.id))
    // Falls back to the tenant default, re-issued above it — see
    // policy.release_device_override on the real backend. The mock keeps it
    // simple: just hand back the current default envelope.
    return HttpResponse.json(defaultPolicy)
  }),

  http.get(`${BASE}/tokens`, ({ request }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    return HttpResponse.json({ tokens })
  }),

  http.post(`${BASE}/tokens`, async ({ request }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    const body = (await request.json()) as { label: string; expires_at: string | null }
    const tokenId = Math.random().toString(16).slice(2, 14).padEnd(12, '0')
    const minted = {
      token_id: tokenId,
      label: body.label,
      created_at: new Date().toISOString(),
      expires_at: body.expires_at,
      revoked: false,
    }
    tokens = [...tokens, minted]
    return HttpResponse.json({ ...minted, token: `shieldtok_${tokenId}${Math.random().toString(16).slice(2)}` })
  }),

  http.delete(`${BASE}/tokens/:id`, ({ request, params }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    const t = tokens.find((x) => x.token_id === params.id)
    if (t) t.revoked = true
    return HttpResponse.json({ revoked: true })
  }),

  http.get(`${BASE}/info`, ({ request }) => {
    if (!authed(request)) return new HttpResponse(null, { status: 401 })
    return HttpResponse.json(SERVER_INFO)
  }),
]
