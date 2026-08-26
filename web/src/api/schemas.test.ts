/**
 * Two claims are under test here, and they are the two the console rests on.
 *
 *  1. The fixtures satisfy the contract exactly — so a screen built against
 *     the mocks is built against the same shape the backend must implement.
 *  2. A response that violates the contract raises `SchemaMismatchError` and
 *     nothing catches it. If this test ever goes green while the client
 *     returns data instead, the product is showing a tick over unvalidated
 *     numbers, which is the failure mode the whole design exists to prevent.
 */
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'

import {
  CoverageReport,
  Device,
  DeviceList,
  Finding,
  FindingList,
  FleetSummary,
  Scope,
} from './schemas'
import {
  apiFetch,
  HttpError,
  isSchemaMismatch,
  MalformedResponseError,
  SchemaMismatchError,
  scopeKey,
  scopeToParams,
} from './client'
import {
  COVERAGE,
  DEVICES,
  DEVICE_LIST,
  FINDINGS,
  FINDING_LIST,
  FLEET_SUMMARY,
} from '@/mocks/fixtures'
import { handlers } from '@/mocks/handlers'

const server = setupServer(...handlers)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const SCOPE: Scope = { tenant: 'acme-prod' }

/* ------------------------------------------------------------------ *
 * 1. Fixtures satisfy the contract
 * ------------------------------------------------------------------ */

describe('fixtures parse against the schemas', () => {
  it('every device parses', () => {
    for (const d of DEVICES) expect(Device.safeParse(d).success).toBe(true)
  })

  it('the fleet summary, device list, findings and coverage all parse', () => {
    expect(FleetSummary.safeParse(FLEET_SUMMARY).success).toBe(true)
    expect(DeviceList.safeParse(DEVICE_LIST).success).toBe(true)
    expect(FindingList.safeParse(FINDING_LIST).success).toBe(true)
    expect(CoverageReport.safeParse(COVERAGE).success).toBe(true)
    for (const f of FINDINGS) expect(Finding.safeParse(f).success).toBe(true)
  })
})

describe('the fixture fleet is the fleet the design frames show', () => {
  it('is 40 enrolled, 37 reporting, 2 silent, 1 never seen', () => {
    expect(DEVICES).toHaveLength(40)
    expect(FLEET_SUMMARY.coverage).toEqual({
      reporting: 37,
      enrolled: 40,
      silent: 2,
      never_seen: 1,
    })
  })

  it('integrity sums to 40, not 37 — unverified is not intact', () => {
    const { intact, broken, unverified, no_verdict } = FLEET_SUMMARY.integrity
    expect({ intact, broken, unverified }).toEqual({ intact: 34, broken: 1, unverified: 5 })
    expect(intact + broken + unverified).toBe(40)
    expect(intact + broken + unverified).not.toBe(FLEET_SUMMARY.inclusion.devices_included)
    // The 3 devices with no verdict at all are the 3 that never reported.
    expect(no_verdict).toBe(3)
  })

  it('has one device mid-rotation and one whose rotation failed', () => {
    expect(DEVICES.filter((d) => d.key_state === 'rotating')).toHaveLength(1)
    expect(DEVICES.filter((d) => d.key_state === 'rotation_failed')).toHaveLength(1)
  })

  it('leaves every derived field null on the never-seen device', () => {
    const unheard = DEVICES.find((d) => d.liveness === 'never_seen')
    expect(unheard).toBeDefined()
    // Zero would render as "on time". Null is the only honest value.
    expect(unheard?.last_batch_at).toBeNull()
    expect(unheard?.last_seq_received).toBeNull()
    expect(unheard?.expected_interval_s).toBeNull()
    expect(unheard?.overdue_multiple).toBeNull()
    expect(unheard?.batches_received).toBe(0)
  })

  it('suppresses the overdue multiple when the cadence is unknown', () => {
    for (const d of DEVICES) {
      if (d.expected_interval_s === null && d.liveness !== 'never_seen') {
        expect(d.overdue_multiple).toBeNull()
      }
    }
  })

  it('contains seqs that collide across devices', () => {
    const seqs = DEVICES.map((d) => d.last_seq_received).filter(
      (s): s is number => s !== null,
    )
    expect(new Set(seqs).size).toBeLessThan(seqs.length)

    // And the same seq range appears on two unrelated devices in coverage.
    const ranges = COVERAGE.unverified_ranges.map((r) => `${r.seq_start}-${r.seq_end}`)
    expect(new Set(ranges).size).toBeLessThan(ranges.length)
  })

  it('has a flat ingest tail, which is an incident and not a quiet period', () => {
    const series = FLEET_SUMMARY.ingest.rate_series
    expect(series.length).toBeGreaterThan(0)
    expect(series.slice(-4).every((v) => v === 0)).toBe(true)
    expect(FLEET_SUMMARY.ingest.rate_flat_for_minutes).toBe(41)
  })
})

/* ------------------------------------------------------------------ *
 * 2. Scope serialisation
 * ------------------------------------------------------------------ */

describe('scope serialisation', () => {
  it('emits array members as repeated keys, never a joined string', () => {
    const qs = scopeToParams({ tenant: 't1', device_ids: ['a', 'b'] }).toString()
    expect(qs).toContain('device_ids=a')
    expect(qs).toContain('device_ids=b')
    expect(qs).not.toContain('a%2Cb')
  })

  it('is stable, so the cache key and the deep link cannot disagree', () => {
    const a: Scope = { tenant: 't', agent_ids: ['x'], device_ids: ['d'], from: '2026-01-01T00:00:00.000Z' }
    const b: Scope = { tenant: 't', from: '2026-01-01T00:00:00.000Z', device_ids: ['d'], agent_ids: ['x'] }
    expect(scopeKey(a)).toBe(scopeKey(b))
  })

  it('omits absent optional fields rather than sending empty values', () => {
    expect(scopeToParams({ tenant: 't' }).toString()).toBe('tenant=t')
  })
})

/* ------------------------------------------------------------------ *
 * 3. The client returns validated data — and refuses to return anything else
 * ------------------------------------------------------------------ */

describe('apiFetch over the mock handlers', () => {
  it('validates and returns each endpoint', async () => {
    await expect(apiFetch('/fleet', FleetSummary, { scope: SCOPE })).resolves.toMatchObject({
      tenant: 'acme-prod',
    })
    const list = await apiFetch('/fleet/devices', DeviceList, { scope: SCOPE })
    expect(list.devices).toHaveLength(40)
    const coverage = await apiFetch('/fleet/coverage', CoverageReport, { scope: SCOPE })
    expect(coverage.blind_spot.basis).toBe('device_enrolments')
    const findings = await apiFetch('/fleet/findings', FindingList, { scope: SCOPE })
    expect(findings.total).toBe(23)
  })

  it('applies the scope rather than ignoring it', async () => {
    const list = await apiFetch('/fleet/devices', DeviceList, {
      scope: { tenant: 'acme-prod', device_ids: ['dev-ci-runner-4'] },
    })
    expect(list.devices).toHaveLength(1)
    expect(list.devices[0]?.device_id).toBe('dev-ci-runner-4')
  })
})

describe('SchemaMismatchError is thrown, never swallowed', () => {
  it('rejects a 200 whose body drifts from the contract', async () => {
    server.use(
      http.get('/v1/console/fleet', () =>
        HttpResponse.json({
          ...FLEET_SUMMARY,
          coverage: { ...FLEET_SUMMARY.coverage, reporting: '37' },
        }),
      ),
    )

    const err = await apiFetch('/fleet', FleetSummary, { scope: SCOPE }).then(
      (data) => {
        throw new Error(
          `apiFetch resolved with unvalidated data instead of throwing: ${JSON.stringify(data).slice(0, 120)}`,
        )
      },
      (e: unknown) => e,
    )

    expect(err).toBeInstanceOf(SchemaMismatchError)
    expect(isSchemaMismatch(err)).toBe(true)
    // Not coerced to a number, not defaulted, not an HttpError.
    expect(err).not.toBeInstanceOf(HttpError)
    if (!isSchemaMismatch(err)) throw new Error('unreachable')
    expect(err.kind).toBe('schema')
    expect(err.issues.length).toBeGreaterThan(0)
    // The offending path is preserved so the UI can name what drifted.
    expect(err.issues.some((i) => i.path.join('.') === 'coverage.reporting')).toBe(true)
    // And the body as received is retained for an operator to inspect.
    expect(err.received).toBeTruthy()
  })

  it('rejects a missing required field just as loudly', async () => {
    const { blind_spot: _dropped, ...withoutBlindSpot } = COVERAGE
    server.use(http.get('/v1/console/fleet/coverage', () => HttpResponse.json(withoutBlindSpot)))
    await expect(
      apiFetch('/fleet/coverage', CoverageReport, { scope: SCOPE }),
    ).rejects.toBeInstanceOf(SchemaMismatchError)
  })

  it('rejects an extra state added to a closed union', async () => {
    // A backend that starts sending liveness: "degraded" must break loudly.
    server.use(
      http.get('/v1/console/fleet/devices', () =>
        HttpResponse.json({
          ...DEVICE_LIST,
          devices: [{ ...DEVICES[0], liveness: 'degraded' }],
        }),
      ),
    )
    await expect(apiFetch('/fleet/devices', DeviceList, { scope: SCOPE })).rejects.toBeInstanceOf(
      SchemaMismatchError,
    )
  })

  it('keeps transport failures distinguishable from contract failures', async () => {
    server.use(
      http.get('/v1/console/fleet', () => new HttpResponse('nope', { status: 503 })),
    )
    const err = await apiFetch('/fleet', FleetSummary, { scope: SCOPE }).catch(
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(HttpError)
    expect(err).not.toBeInstanceOf(SchemaMismatchError)
    expect(isSchemaMismatch(err)).toBe(false)
  })

  it('distinguishes a non-JSON body from a contract violation', async () => {
    server.use(
      http.get('/v1/console/fleet', () =>
        HttpResponse.text('<html>proxy interstitial</html>', {
          headers: { 'Content-Type': 'text/html' },
        }),
      ),
    )
    const err = await apiFetch('/fleet', FleetSummary, { scope: SCOPE }).catch(
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(MalformedResponseError)
    expect(err).not.toBeInstanceOf(SchemaMismatchError)
  })
})
