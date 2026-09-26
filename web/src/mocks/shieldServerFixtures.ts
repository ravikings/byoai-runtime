/**
 * Fixtures for the free, self-hosted Shield server admin API (Phase 1.5).
 * Kept separate from `fixtures.ts` (the Coriqo-style fleet console fixtures):
 * different backend, different contract, same repo.
 *
 * Shapes here are pinned against real responses from `create_app()` — see
 * `web/src/mocks/__contract__/shield_server/*.json` (dumped by
 * `tests/shield_server/test_console_contract_dump.py`) and
 * `web/src/mocks/__contract__/shieldServerContract.test.ts`, which parses
 * those files with the exact same zod schemas these fixtures use.
 */
import type {
  ShieldServerDevice,
  ShieldServerInfo,
  ShieldServerRawEnvelope,
  ShieldServerToken,
} from '@/api/shieldServer'

export const ADMIN_TOKEN = 'test-admin-token'

const SIGNATURE = 'a1b2c3d4e5f6'.repeat(10) + 'a1b2c3d4'

export const SERVER_INFO: ShieldServerInfo = {
  public_url: 'http://127.0.0.1:17840',
  org: 'default',
  key_id: 'shield-server-abcdef12',
}

export const DEFAULT_POLICY: ShieldServerRawEnvelope = {
  document: {
    policy_id: 'pol_0f8a2c9b3d1e4a2b8c6d5e4f3a2b1c0d',
    version: 3,
    tenant_slug: 'default',
    device_id: null,
    issued_at: '2026-09-20T10:00:00Z',
    key_id: SERVER_INFO.key_id,
    managed_by: 'Acme IT',
    policy: {
      mode: 'redact',
      apps: { claude: true, chatgpt: true, gemini: false },
      keep_text: false,
      retention_days: 30,
      notice: true,
    },
    locked: ['mode', 'retention_days'],
  },
  signature: SIGNATURE,
}

export const SHIELD_DEVICES: ShieldServerDevice[] = [
  {
    device_id: 'dev_0f8a2c9b3d1e',
    label: "Alice's MacBook Pro",
    enrolled_at: '2026-09-01T09:00:00Z',
    last_seen: '2026-09-26T09:58:00Z',
    protecting: true,
    reasons: [],
    mode: 'redact',
    policy_version: 3,
    assigned_version: 3,
    drift: false,
    revoked: false,
  },
  {
    device_id: 'dev_1a2b3c4d5e6f',
    label: "Bob's Mac mini",
    enrolled_at: '2026-08-15T09:00:00Z',
    last_seen: '2026-09-24T12:00:00Z',
    protecting: false,
    reasons: ['policy signed with an unknown key', 'has not reported in 2 days'],
    mode: 'redact',
    policy_version: 2,
    assigned_version: 3,
    drift: true,
    revoked: false,
  },
  {
    device_id: 'dev_deadbeef0001',
    label: 'Old contractor laptop',
    enrolled_at: '2026-07-01T09:00:00Z',
    last_seen: '2026-07-10T09:00:00Z',
    protecting: false,
    reasons: ['revoked'],
    mode: null,
    policy_version: 1,
    assigned_version: null,
    drift: false,
    revoked: true,
  },
  {
    device_id: 'dev_freshenrolee0001',
    label: "Chen's MacBook Air",
    enrolled_at: '2026-09-26T09:00:00Z',
    last_seen: null,
    protecting: null,
    reasons: [],
    mode: null,
    policy_version: null,
    assigned_version: 3,
    drift: false,
    revoked: false,
  },
]

export const SHIELD_TOKENS: ShieldServerToken[] = [
  {
    token_id: '111111111111',
    label: 'Front desk Mac',
    created_at: '2026-09-20T10:00:00Z',
    expires_at: '2026-10-20T10:00:00Z',
    revoked: false,
  },
  {
    token_id: '222222222222',
    label: 'Old token',
    created_at: '2026-08-01T10:00:00Z',
    expires_at: '2026-08-08T10:00:00Z',
    revoked: true,
  },
]
