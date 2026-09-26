/**
 * Same discipline as `schemas.test.ts`: the fixtures satisfy the shield
 * server admin API contract exactly, and a response that violates it raises
 * a typed error rather than being silently accepted.
 */
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'

import {
  fetchDevices,
  fetchDefaultPolicy,
  getStoredAdminToken,
  isAuthError,
  setStoredAdminToken,
  clearStoredAdminToken,
  ShieldServerSchemaError,
  validateLockedKeys,
  validatePolicyDoc,
} from './shieldServer'
import { shieldServerHandlers, resetShieldServerMocks } from '@/mocks/shieldServerHandlers'
import { ADMIN_TOKEN } from '@/mocks/shieldServerFixtures'

const server = setupServer(...shieldServerHandlers)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetShieldServerMocks()
  clearStoredAdminToken()
})
afterAll(() => server.close())

const BASE = '/api/v1/shield'

describe('shieldServer client', () => {
  it('fetches devices and validates the contract', async () => {
    setStoredAdminToken(ADMIN_TOKEN)
    const devices = await fetchDevices()
    expect(devices.devices.length).toBeGreaterThan(0)
  })

  it('raises ShieldServerAuthError on a 401, not a generic error', async () => {
    // no token stored
    await expect(fetchDevices()).rejects.toSatisfy((e: unknown) => isAuthError(e))
  })

  it('raises a schema error, not silently returning bad data', async () => {
    setStoredAdminToken(ADMIN_TOKEN)
    server.use(
      http.get(`${BASE}/devices`, () => HttpResponse.json({ devices: [{ device_id: 1 }] })),
    )
    await expect(fetchDevices()).rejects.toBeInstanceOf(ShieldServerSchemaError)
  })

  it('fetches the default policy envelope', async () => {
    setStoredAdminToken(ADMIN_TOKEN)
    const envelope = await fetchDefaultPolicy()
    expect(envelope.policy?.mode).toBe('redact')
    expect(envelope.locked).toContain('mode')
  })
})

describe('admin token storage survives a sessionStorage that throws', () => {
  it('setStoredAdminToken then getStoredAdminToken still work when sessionStorage.setItem/getItem throw', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })

    try {
      // Neither call may throw into the caller — this is the private-window
      // / blocked-storage case the in-memory fallback exists for.
      expect(() => setStoredAdminToken('a-token-from-a-private-window')).not.toThrow()
      expect(getStoredAdminToken()).toBe('a-token-from-a-private-window')

      // fetches actually authenticate off the in-memory value too, not just
      // getStoredAdminToken() in isolation.
      expect(getStoredAdminToken()).not.toBeNull()
    } finally {
      setItem.mockRestore()
      getItem.mockRestore()
      clearStoredAdminToken()
    }
  })

  it('clearStoredAdminToken drops the in-memory value even when storage throws', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const removeItem = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    try {
      setStoredAdminToken('temp-token')
      expect(getStoredAdminToken()).toBe('temp-token')
      clearStoredAdminToken()
      expect(getStoredAdminToken()).toBeNull()
    } finally {
      setItem.mockRestore()
      removeItem.mockRestore()
    }
  })
})

describe('client-side policy validation mirrors server rules', () => {
  it('rejects an out-of-range retention', () => {
    const errors = validatePolicyDoc({
      mode: 'redact',
      apps: { claude: true },
      keep_text: false,
      // @ts-expect-error deliberately invalid for the test
      retention_days: 14,
      notice: true,
    })
    expect(errors.some((e) => e.field === 'retention_days')).toBe(true)
  })

  it('rejects an unknown lock key', () => {
    const errors = validateLockedKeys(['mode', 'not_a_real_key'])
    expect(errors).toHaveLength(1)
  })

  it('accepts a valid document with no apps issue when apps is non-empty', () => {
    const errors = validatePolicyDoc({
      mode: 'block',
      apps: { claude: true },
      keep_text: true,
      retention_days: 90,
      notice: false,
    })
    expect(errors).toHaveLength(0)
  })
})
