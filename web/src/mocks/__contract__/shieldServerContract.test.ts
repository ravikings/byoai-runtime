/**
 * Pins the console/server contract from the console's side: every JSON file
 * here is a REAL response dumped straight from `create_app()` (see
 * `tests/shield_server/test_console_contract_dump.py`), parsed with the
 * exact zod schemas the console's api client uses. A schema change on either
 * side that stops agreeing with the other fails here.
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import {
  ShieldServerDeviceList,
  ShieldServerDrift,
  ShieldServerInfo,
  ShieldServerRawEnvelope,
  ShieldServerTokenList,
  ShieldServerTokenMinted,
  deriveEnvelopeView,
} from '@/api/shieldServer'

const DIR = dirname(fileURLToPath(import.meta.url))

function loadFixture(name: string): unknown {
  return JSON.parse(readFileSync(join(DIR, 'shield_server', `${name}.json`), 'utf-8'))
}

describe('shield server contract fixtures parse with the console schemas', () => {
  it('info', () => {
    expect(ShieldServerInfo.parse(loadFixture('info'))).toMatchObject({ org: 'default' })
  })

  it('policy_null — nothing ever set', () => {
    const raw = ShieldServerRawEnvelope.parse(loadFixture('policy_null'))
    expect(raw).toBeNull()
    expect(deriveEnvelopeView(raw)).toEqual({
      version: null, key_id: null, policy: null, locked: [], managed_by: null,
      issued_at: null, signature: null,
    })
  })

  it('policy — a real signed default', () => {
    const raw = ShieldServerRawEnvelope.parse(loadFixture('policy'))
    expect(raw).not.toBeNull()
    const view = deriveEnvelopeView(raw)
    expect(view.policy?.mode).toBe('redact')
    expect(view.locked).toContain('mode')
    expect(view.signature).toEqual(expect.any(String))
  })

  it('device_policy_null — device never had an override', () => {
    expect(ShieldServerRawEnvelope.parse(loadFixture('device_policy_null'))).toBeNull()
  })

  it('device_policy — a real per-device override', () => {
    const raw = ShieldServerRawEnvelope.parse(loadFixture('device_policy'))
    const view = deriveEnvelopeView(raw)
    expect(view.policy?.mode).toBe('block')
    expect(view.managed_by).toContain('override')
  })

  it('devices', () => {
    const list = ShieldServerDeviceList.parse(loadFixture('devices'))
    expect(list.devices.length).toBeGreaterThan(0)
    expect(list.devices.at(0)?.label).toBe('Contract Mac')
  })

  it('drift — wrapped in {devices}', () => {
    const drift = ShieldServerDrift.parse(loadFixture('drift'))
    expect(Array.isArray(drift.devices)).toBe(true)
  })

  it('tokens', () => {
    const list = ShieldServerTokenList.parse(loadFixture('tokens'))
    expect(list.tokens.at(0)?.token_id).toHaveLength(12)
    expect(list.tokens.at(0)).not.toHaveProperty('token_hash')
  })

  it('tokens_mint — the token is only ever present here', () => {
    const minted = ShieldServerTokenMinted.parse(loadFixture('tokens_mint'))
    expect(minted.token.startsWith('shieldtok_')).toBe(true)
    expect(minted.token_id).toHaveLength(12)
  })
})
