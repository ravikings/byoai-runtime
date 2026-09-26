/**
 * The register's ordering and per-state rendering. The two properties worth
 * pinning: silence sorts above shipping (quietest first, unknown silence
 * above measured), and a device that never reported renders `never` — no
 * duration is invented for it, and no seq is rendered bare.
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ScopeQSProvider } from '@/app/hrefContext'
import { DeviceRegister, orderRegister } from './DeviceRegister'
import type { Device } from '@/api/schemas'

function device(over: Partial<Device>): Device {
  return {
    device_id: 'dev_AAAA',
    host: 'host',
    agent_ids: [],
    liveness: 'reporting',
    enrolled_at: '2026-09-01T00:00:00Z',
    last_batch_at: '2026-09-26T12:00:00Z',
    last_seq_received: 10,
    expected_interval_s: null,
    quiet_for_s: 5,
    overdue_multiple: null,
    ship_lag_s: null,
    key_state: 'verified',
    integrity: 'intact',
    batches_received: 3,
    ...over,
  }
}

describe('orderRegister', () => {
  it('sorts never_seen, then longest silence, then reporting', () => {
    const rows = orderRegister([
      device({ device_id: 'dev_R', liveness: 'reporting' }),
      device({ device_id: 'dev_N', liveness: 'never_seen', quiet_for_s: null, last_batch_at: null, last_seq_received: null, integrity: 'unverified' }),
      device({ device_id: 'dev_S2', liveness: 'silent', quiet_for_s: 900 }),
      device({ device_id: 'dev_S1', liveness: 'silent', quiet_for_s: 3600 }),
      device({ device_id: 'dev_U', liveness: 'silent', quiet_for_s: null }),
    ])
    expect(rows.map((r) => r.device_id)).toEqual([
      'dev_N',
      'dev_U', // unknown silence is unbounded, not zero — it ranks first in the group
      'dev_S1',
      'dev_S2',
      'dev_R',
    ])
  })
})

describe('DeviceRegister', () => {
  it('renders the three liveness states honestly', () => {
    render(
      <ScopeQSProvider value="">
        <DeviceRegister
          devices={[
            device({ device_id: 'dev_LIVE' }),
            device({ device_id: 'dev_Silent', liveness: 'silent', quiet_for_s: 3600 }),
            device({
              device_id: 'dev_Never',
              liveness: 'never_seen',
              quiet_for_s: null,
              last_batch_at: null,
              last_seq_received: null,
              integrity: 'unverified',
              batches_received: null,
            }),
          ]}
          tenant="t"
          total={3}
        />
      </ScopeQSProvider>,
    )
    // never_seen shows the badge, never a fabricated duration.
    expect(screen.getByText('never')).toBeTruthy()
    // "unverified — not a pass" is the third state's own words.
    expect(screen.getByText(/unverified — not a pass/)).toBeTruthy()
    // A seq always carries its device tail; no bare number cells.
    expect(screen.getByText('…ev_LIVE')).toBeTruthy()
    expect(screen.getAllByText('10').length).toBeGreaterThanOrEqual(2) // reporting + silent last_seq
    expect(screen.getByText('none ever')).toBeTruthy() // null last_seq on dev_Never
    expect(screen.getByText(/showing/)).toBeTruthy()
  })
})
