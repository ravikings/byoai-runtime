/**
 * MSW v2 handlers for every console endpoint the query hooks call.
 *
 * The handlers apply the scope selector (§2.3) rather than ignoring it, because
 * a mock that returns the whole fleet regardless of `device_ids` would let a
 * scope-filtering bug ship: the chip would say "3 devices" and the numbers
 * would be the fleet's.
 */
import { http, HttpResponse, type HttpHandler } from 'msw'
import {
  COVERAGE,
  DEVICES,
  FINDINGS,
  FLEET_SUMMARY,
  INCLUSION,
} from './fixtures'
import type { Device, Inclusion } from '@/api/schemas'

/** Matches the client's default base; overridden by VITE_API_BASE in dev. */
const BASE = import.meta.env.VITE_API_BASE ?? '/v1/console'

function scopedDevices(url: URL): readonly Device[] {
  const ids = url.searchParams.getAll('device_ids')
  const agents = url.searchParams.getAll('agent_ids')
  let out: readonly Device[] = DEVICES
  if (ids.length > 0) out = out.filter((d) => ids.includes(d.device_id))
  if (agents.length > 0) out = out.filter((d) => d.agent_ids.some((a) => agents.includes(a)))
  return out
}

/**
 * The denominator narrows with the scope, but the ratio stays honest: a scoped
 * view still reports how many of the devices it covers actually reported.
 */
function inclusionFor(devices: readonly Device[]): Inclusion {
  if (devices.length === DEVICES.length) return INCLUSION
  return {
    devices_included: devices.filter(
      (d) => d.liveness === 'reporting' || d.liveness === 'late',
    ).length,
    devices_enrolled: devices.length,
  }
}

export const handlers: HttpHandler[] = [
  http.get(`${BASE}/fleet`, ({ request }) => {
    // The summary must narrow with the scope, like every sibling endpoint. An
    // unscoped /fleet beneath a scoped chip is the "chip says 3 devices, the
    // numbers are the fleet's" bug reproduced in the mock — and a mock that
    // models the bug is how the bug ships.
    const devices = scopedDevices(new URL(request.url))
    if (devices.length === DEVICES.length) return HttpResponse.json(FLEET_SUMMARY)
    const by = (l: Device['liveness']) => devices.filter((d) => d.liveness === l).length
    const integrity = (i: Device['integrity']) =>
      devices.filter((d) => d.integrity === i).length
    const ids = devices.map((d) => d.device_id)
    return HttpResponse.json({
      ...FLEET_SUMMARY,
      inclusion: inclusionFor(devices),
      coverage: {
        reporting: by('reporting') + by('late'),
        enrolled: devices.length,
        silent: by('silent'),
        never_seen: by('never_seen'),
      },
      integrity: {
        intact: integrity('intact'),
        broken: integrity('broken'),
        unverified: integrity('unverified'),
        no_verdict: by('silent') + by('never_seen'),
      },
      open_findings: FINDINGS.filter((f) => ids.includes(f.device_id)).length,
    })
  }),

  http.get(`${BASE}/fleet/devices`, ({ request }) => {
    const devices = scopedDevices(new URL(request.url))
    return HttpResponse.json({
      inclusion: inclusionFor(devices),
      devices,
      next_cursor: null,
    })
  }),

  http.get(`${BASE}/fleet/coverage`, ({ request }) => {
    const url = new URL(request.url)
    // Scope by BOTH device_ids and agent_ids, via the same resolver
    // /fleet/devices uses. Filtering on device_ids alone meant an agent-scoped
    // chip ("3 devices") sat above whole-fleet coverage numbers — the exact
    // mismatch this file's header says must not ship.
    const scoped = scopedDevices(url)
    const isFullFleet = scoped.length === DEVICES.length
    if (isFullFleet) return HttpResponse.json(COVERAGE)
    const ids = scoped.map((d) => d.device_id)
    const keep = (d: { device_id: string }) => ids.includes(d.device_id)
    return HttpResponse.json({
      ...COVERAGE,
      enrolled: scoped.length,
      never_seen: COVERAGE.never_seen.filter(keep),
      silent: COVERAGE.silent.filter(keep),
      unverified_ranges: COVERAGE.unverified_ranges.filter(keep),
      checkpoint_gaps: {
        // Recount from the surviving rows. Carrying whole-fleet totals beside a
        // filtered detail list reproduces, in the mock, exactly the count/rows
        // mismatch the UI is built to refuse — and a mock that models the bug
        // is how the bug ships.
        ...COVERAGE.checkpoint_gaps,
        sessions_without_checkpoint: COVERAGE.checkpoint_gaps.detail
          .filter(keep)
          .filter((d) => d.what.includes('checkpoint row'))
          .reduce((acc, d) => acc + d.count, 0),
        checkpoints_never_countersigned: COVERAGE.checkpoint_gaps.detail
          .filter(keep)
          .filter((d) => !d.what.includes('checkpoint row'))
          .reduce((acc, d) => acc + d.count, 0),
        detail: COVERAGE.checkpoint_gaps.detail.filter(keep),
      },
      ungoverned_agents: COVERAGE.ungoverned_agents.filter(keep),
    })
  }),

  http.get(`${BASE}/fleet/findings`, ({ request }) => {
    const url = new URL(request.url)
    // Same rule as /fleet/coverage: agent_ids narrows the fleet too.
    const devices = scopedDevices(url)
    const isFullFleet = devices.length === DEVICES.length
    const ids = devices.map((d) => d.device_id)
    const findings = isFullFleet ? FINDINGS : FINDINGS.filter((f) => ids.includes(f.device_id))
    return HttpResponse.json({
      inclusion: inclusionFor(devices),
      findings,
      // `total` is the count behind the "all N →" affordance, not the page
      // length — they differ, and conflating them understates the backlog.
      total: isFullFleet ? FLEET_SUMMARY.open_findings : findings.length,
    })
  }),
]

/**
 * A deliberately contract-violating fleet response, for exercising the
 * "unexpected response" state by hand in dev (`?mock=broken`) and in tests.
 * `coverage.reporting` arrives as a string; nothing else is wrong, which is
 * exactly the kind of drift that would otherwise render as a green tick.
 */
export const brokenFleetHandler: HttpHandler = http.get(`${BASE}/fleet`, () =>
  HttpResponse.json({
    ...FLEET_SUMMARY,
    coverage: { ...FLEET_SUMMARY.coverage, reporting: '37' },
  }),
)
