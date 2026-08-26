/**
 * The fixture fleet.
 *
 * These are not placeholder numbers. They reproduce the figures the design
 * frames settled on (internal_doc/console_design/frame-8-fleet-overview.html
 * and frame-9-coverage.html), so the React app renders the screens that were
 * actually designed rather than a different fleet that happens to type-check.
 *
 * The shape of the fleet is deliberate, and every awkward property in it is a
 * real property of the system the console is trying to describe:
 *
 *  - 40 enrolled, 37 "reporting" (35 within cadence + 2 late), 2 silent,
 *    1 never seen. Non-reporting devices are excluded from every aggregate,
 *    which is why `inclusion` travels with each figure.
 *  - Integrity sums to 40, not 37: 34 intact + 1 broken + 5 unverified. The
 *    3 non-reporting devices are unverified (not intact), and 2 reporting
 *    devices have never been covered by a verify walk.
 *  - **Seqs collide across devices.** seq 1,203 exists on four devices here
 *    and is four unrelated events; two devices share the exact same
 *    `last_seq_received`. This is true of the real system, and the UI is
 *    designed to expose it, so the fixtures must not paper over it.
 */
import type {
  CoverageReport,
  Device,
  Finding,
  FleetSummary,
  Inclusion,
} from '@/api/schemas'

/* ------------------------------------------------------------------ *
 * Clock
 * ------------------------------------------------------------------ */

/** Frozen "now". The frames are stamped 2026-08-26 11:20 UTC. */
export const NOW_MS = Date.parse('2026-08-26T11:20:00.000Z')
export const NOW_ISO = new Date(NOW_MS).toISOString()
export const WINDOW_FROM_ISO = new Date(NOW_MS - 24 * 3600_000).toISOString()

const MIN = 60
const HOUR = 3600
const DAY = 86_400

/** ISO timestamp for "n seconds before the frozen now". */
function ago(seconds: number): string {
  return new Date(NOW_MS - seconds * 1000).toISOString()
}

export const TENANT = 'acme-prod'


/* ------------------------------------------------------------------ *
 * Devices
 * ------------------------------------------------------------------ */

const REGIONS = ['us-east-2', 'eu-west-1', 'eu-central-1', 'us-west-2', 'ap-south-1'] as const

/**
 * A tiny deterministic PRNG. Fixtures must be byte-identical across runs —
 * a test that asserts "37 of 40" cannot depend on Math.random.
 */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
function pick<T>(rng: () => number, xs: readonly T[]): T {
  const i = Math.floor(rng() * xs.length)
  // noUncheckedIndexedAccess: the modulo cannot go out of range, but the
  // compiler cannot know that and we do not cast our way past it.
  const v = xs[i] ?? xs[0]
  if (v === undefined) throw new Error('pick() called on an empty list')
  return v
}

/** Base for a healthy, reporting, verified, intact device. */
function healthy(
  device_id: string,
  host: string,
  agent_ids: readonly string[],
  opts: {
    last_seq: number
    interval_s: number
    quiet_s: number
    batches: number
    enrolled_days_ago: number
    ship_lag_s?: number
  },
): Device {
  return {
    device_id,
    host,
    agent_ids: [...agent_ids],
    liveness: 'reporting',
    enrolled_at: ago(opts.enrolled_days_ago * DAY),
    last_batch_at: ago(opts.quiet_s),
    last_seq_received: opts.last_seq,
    expected_interval_s: opts.interval_s,
    quiet_for_s: opts.quiet_s,
    overdue_multiple: Number((opts.quiet_s / opts.interval_s).toFixed(2)),
    ship_lag_s: opts.ship_lag_s ?? 12,
    key_state: 'verified',
    integrity: 'intact',
    batches_received: opts.batches,
  }
}

/* -- The named devices the frames call out by id ------------------- */

/** The one broken chain. Findings cite seq 1,203 and the gap 1,438–1,441. */
const CI_RUNNER_4: Device = {
  ...healthy('dev-ci-runner-4', 'ci-runner-04.eu-west-1', ['repo-janitor', 'pr-reviewer'], {
    last_seq: 1_204_882,
    interval_s: 5 * MIN,
    quiet_s: 41 * MIN,
    batches: 4_812,
    enrolled_days_ago: 85,
  }),
  // 41 minutes quiet against a 5-minute cadence is 8.2x overdue. Leaving the
  // default 'reporting' here gave the one device with a broken chain a healthy
  // liveness badge — a fixture that contradicts its own overdue_multiple, and
  // exactly the "looks fine, isn't" rendering these screens exist to refuse.
  liveness: 'late',
  integrity: 'broken',
}

/** Silent: quiet 19h 42m against a 5m cadence. */
const OPS_RUNNER_2: Device = {
  device_id: 'd1-ops-runner-02',
  host: 'agent-worker-11.us-east-2',
  agent_ids: ['invoice-reconciler', 'support-triage'],
  liveness: 'silent',
  enrolled_at: ago(90 * DAY),
  last_batch_at: ago(19 * HOUR + 42 * MIN),
  last_seq_received: 84_213,
  expected_interval_s: 5 * MIN,
  quiet_for_s: 19 * HOUR + 42 * MIN,
  overdue_multiple: 236.4,
  ship_lag_s: null,
  key_state: 'verified',
  integrity: 'unverified',
  batches_received: 3_119,
}

/** Silent: quiet 6h 08m. */
const BATCH_WORKER_7: Device = {
  device_id: 'd1-batch-worker-07',
  host: 'nightly-etl.eu-central-1',
  agent_ids: ['nightly-reconcile'],
  liveness: 'silent',
  enrolled_at: ago(60 * DAY),
  last_batch_at: ago(6 * HOUR + 8 * MIN),
  last_seq_received: 12_940,
  expected_interval_s: DAY,
  quiet_for_s: 6 * HOUR + 8 * MIN,
  // Under a nightly cadence 6h is not yet overdue by the clock that matters —
  // the multiple, not the wall time, is the severity. It is silent because the
  // shipper heartbeat stopped, which is a different signal from lateness.
  overdue_multiple: 0.26,
  ship_lag_s: null,
  key_state: 'unchecked',
  integrity: 'unverified',
  batches_received: 61,
}

/** Never seen: enrolled, key registered, not one batch has ever arrived. */
const MBP_11: Device = {
  device_id: 'dev-mbp-11',
  host: 'mbp-k.torres.local',
  agent_ids: [],
  liveness: 'never_seen',
  enrolled_at: ago(2 * DAY + 18 * HOUR),
  last_batch_at: null,
  last_seq_received: null,
  // Every derived field is null, not zero. A device that never reported has no
  // cadence to be late against; rendering 0 here would read as "on time".
  expected_interval_s: null,
  quiet_for_s: null,
  overdue_multiple: null,
  ship_lag_s: null,
  key_state: 'unchecked',
  integrity: 'unverified',
  batches_received: 0,
}

/** The failed rotation: key_rotated at seq 31,884, still shipping under the old key. */
const OPS_RUNNER_5: Device = {
  ...healthy('d1-ops-runner-05', 'ops-runner-05.us-east-2', ['deploy-bot'], {
    last_seq: 1_338_907,
    interval_s: 5 * MIN,
    quiet_s: 3 * MIN,
    batches: 5_204,
    enrolled_days_ago: 78,
  }),
  key_state: 'rotation_failed',
}

/** Mid-rotation: new key issued, first batch under it not yet accepted. */
const OPS_RUNNER_9: Device = {
  ...healthy('d1-ops-runner-09', 'ops-runner-09.us-west-2', ['deploy-bot', 'repo-janitor'], {
    last_seq: 902_441,
    interval_s: 5 * MIN,
    quiet_s: 2 * MIN,
    batches: 3_540,
    enrolled_days_ago: 70,
  }),
  key_state: 'rotating',
}

/** Stale key usage on 4 entries, retired at seq 1,502. */
const MBP_1: Device = {
  ...healthy('dev-mbp-01', 'mbp-a.rahman.local', ['support-triage'], {
    last_seq: 1_203,
    interval_s: 15 * MIN,
    quiet_s: 9 * MIN,
    batches: 118,
    enrolled_days_ago: 44,
  }),
  // COLLISION: last_seq_received 1,203 is also the seq of the broken link on
  // dev-ci-runner-4. Same number, unrelated events, different chains.
  last_seq_received: 1_203,
}

/** Reporting but never verified — one of the two "no verify job has covered". */
const BATCH_WORKER_3: Device = {
  ...healthy('d1-batch-worker-03', 'batch-worker-03.ap-south-1', ['invoice-reconciler'], {
    last_seq: 52_377,
    interval_s: 30 * MIN,
    quiet_s: 11 * MIN,
    batches: 1_402,
    enrolled_days_ago: 85,
  }),
  integrity: 'unverified',
}

/** The other never-verified reporting device. */
const CI_RUNNER_9: Device = {
  ...healthy('dev-ci-runner-9', 'ci-runner-09.eu-west-1', ['pr-reviewer'], {
    last_seq: 52_377,
    interval_s: 5 * MIN,
    quiet_s: 4 * MIN,
    batches: 2_884,
    enrolled_days_ago: 51,
  }),
  // COLLISION: identical last_seq_received to d1-batch-worker-03 above. Two
  // devices at the same seq is coincidence, not correlation, and a UI that
  // keys anything on seq alone would merge these two rows.
  integrity: 'unverified',
}

/** Late (not yet silent): overdue but inside the tolerance band. */
const SALES_BOT_3: Device = {
  ...healthy('d1-sales-bot-03', 'sales-bot-03.us-east-2', ['sales-outreach'], {
    last_seq: 95_884,
    interval_s: 5 * MIN,
    quiet_s: 41 * MIN,
    batches: 1_960,
    enrolled_days_ago: 33,
  }),
  liveness: 'late',
  overdue_multiple: 8.2,
}

/** Late, and too few batches to infer a cadence at all. */
const EDGE_GATEWAY_2: Device = {
  device_id: 'd1-edge-gateway-02',
  host: 'edge-gateway-02.ap-south-1',
  agent_ids: ['edge-triage'],
  liveness: 'late',
  enrolled_at: ago(6 * DAY),
  last_batch_at: ago(2 * HOUR + 14 * MIN),
  last_seq_received: 4_118,
  // Fewer than 20 batches: the median inter-batch interval is not yet
  // meaningful, so it is null and the multiple is suppressed rather than
  // guessed. An unknown cadence must never render as an on-time one.
  expected_interval_s: null,
  quiet_for_s: 2 * HOUR + 14 * MIN,
  overdue_multiple: null,
  ship_lag_s: 44,
  key_state: 'unchecked',
  integrity: 'intact',
  batches_received: 11,
}

const NAMED: readonly Device[] = [
  CI_RUNNER_4,
  OPS_RUNNER_2,
  BATCH_WORKER_7,
  MBP_11,
  OPS_RUNNER_5,
  OPS_RUNNER_9,
  MBP_1,
  BATCH_WORKER_3,
  CI_RUNNER_9,
  SALES_BOT_3,
  EDGE_GATEWAY_2,
]

/**
 * The remaining 29 devices: healthy, reporting, intact, verified. They exist
 * so the fleet arithmetic is real — 34 intact devices is a claim the list must
 * be able to back, not a number typed into a summary.
 */
function makeFiller(): Device[] {
  const rng = mulberry32(0x51_1e_4c_e0)
  const roles = [
    ['ops-runner', ['deploy-bot']],
    ['batch-worker', ['invoice-reconciler']],
    ['ci-runner', ['repo-janitor', 'pr-reviewer']],
    ['sales-bot', ['sales-outreach']],
    ['api-node', ['support-triage']],
  ] as const

  const out: Device[] = []
  for (let i = 0; i < 29; i += 1) {
    const role = pick(rng, roles)
    const n = 20 + i
    const id = `d1-${role[0]}-${String(n).padStart(2, '0')}`
    const region = pick(rng, REGIONS)
    const interval = pick(rng, [5 * MIN, 5 * MIN, 5 * MIN, 15 * MIN, 30 * MIN])
    const quiet = Math.floor(interval * (0.1 + rng() * 0.7))
    out.push(
      healthy(id, `${role[0]}-${String(n).padStart(2, '0')}.${region}`, role[1], {
        // Seqs in the millions, as real recorders reach after weeks of runs.
        last_seq: 1_000_000 + Math.floor(rng() * 900_000),
        interval_s: interval,
        quiet_s: quiet,
        batches: 400 + Math.floor(rng() * 6_000),
        enrolled_days_ago: 20 + Math.floor(rng() * 70),
        ship_lag_s: 3 + Math.floor(rng() * 40),
      }),
    )
  }

  // Two filler devices are forced onto the same seq as each other and onto the
  // same seq as dev-ci-runner-4's head. Collisions are not rare in a fleet
  // where every chain starts at 1 and advances at a similar rate.
  const a = out[0]
  const b = out[1]
  if (a) a.last_seq_received = 1_204_882
  if (b) b.last_seq_received = 1_204_882
  // A couple of devices ran without a public key on file: nothing was proven
  // either way, which is "unchecked", not "verified" and not "failed".
  const c = out[2]
  const d = out[3]
  if (c) c.key_state = 'unchecked'
  if (d) d.key_state = 'unchecked'
  return out
}

export const DEVICES: readonly Device[] = [...NAMED, ...makeFiller()]

// Derived, not typed in. This file's docstring insists the fleet arithmetic is
// real; a literal here drifts the moment a device's liveness is edited, and two
// panels reading different Inclusion objects would then disagree about "N of 40".
export const INCLUSION: Inclusion = {
  devices_included: DEVICES.filter((d) => d.liveness === 'reporting' || d.liveness === 'late')
    .length,
  devices_enrolled: DEVICES.length,
}

/* ------------------------------------------------------------------ *
 * Fleet summary — the numbers on frame 8
 * ------------------------------------------------------------------ */

const count = (p: (d: Device) => boolean): number => DEVICES.filter(p).length

export const FLEET_SUMMARY: FleetSummary = {
  tenant: TENANT,
  window: { from: WINDOW_FROM_ISO, to: NOW_ISO },
  inclusion: INCLUSION,
  coverage: {
    // "Reporting" on the summary means "has shipped recently enough to be in
    // the aggregate", which includes the two late devices. Silent and
    // never_seen are the excluded three.
    reporting: count((d) => d.liveness === 'reporting' || d.liveness === 'late'),
    enrolled: DEVICES.length,
    silent: count((d) => d.liveness === 'silent'),
    never_seen: count((d) => d.liveness === 'never_seen'),
  },
  integrity: {
    intact: count((d) => d.integrity === 'intact'),
    broken: count((d) => d.integrity === 'broken'),
    unverified: count((d) => d.integrity === 'unverified'),
    // The 3 non-reporting devices are counted as unverified above AND as
    // having no verdict here — the frame spells this out because 34+1+5 = 40
    // while only 37 devices are in the denominator of everything else.
    no_verdict: count((d) => d.liveness === 'silent' || d.liveness === 'never_seen'),
  },
  ingest: {
    entries_received: 1_284_907,
    backlog_entries: 412,
    backlog_devices: 6,
    oldest_unshipped_at: ago(4 * HOUR + 51 * MIN),
    last_batch_at: ago(41 * MIN),
    checkpoints_pending: 63,
    // entries/min, oldest first, 6h at 10-minute buckets. The tail is flat at
    // zero for the last 41 minutes: recorders ship on a timer, so a flat run
    // is an incident, not a quiet period.
    rate_series: buildRateSeries(),
    rate_flat_for_minutes: 41,
  },
  denial: {
    denied_per_1k: 14.2,
    previous_per_1k: 9.6,
    denied: 2_981,
    flagged: 1_140,
    tool_use_total: 209_884,
    top_refused: [
      { tool: 'shell.exec', reason: 'out of mandate', count: 1_402 },
      { tool: 'fs.write', reason: 'path not allowed', count: 884 },
      { tool: 'http.post', reason: 'host not in mandate', count: 431 },
      { tool: 'db.query', reason: 'flagged, no mandate snapshot', count: 264 },
    ],
  },
  open_findings: 23,
}

function buildRateSeries(): number[] {
  const rng = mulberry32(0xf1a7)
  const buckets: number[] = []
  // 36 ten-minute buckets = 6h.
  for (let i = 0; i < 36; i += 1) {
    buckets.push(820 + Math.floor(rng() * 340))
  }
  // The last ~41 minutes are dead flat. Not low — flat.
  for (let i = 32; i < 36; i += 1) buckets[i] = 0
  return buckets
}

/* ------------------------------------------------------------------ *
 * Findings — frame 8's "Open findings" panel
 * ------------------------------------------------------------------ */

export const FINDINGS: readonly Finding[] = [
  {
    id: 'f_9a41c07b',
    kind: 'broken_links',
    severity: 'bad',
    what:
      'broken_links[0] — entry at seq 1,203 declares prev_hash 4b81ce07…, but seq 1,202 hashes to c70d19af…; everything after it is unanchored.',
    device_id: 'dev-ci-runner-4',
    ref: { device_id: 'dev-ci-runner-4', seq: 1_203 },
  },
  {
    id: 'f_2b70dd15',
    kind: 'gaps',
    severity: 'bad',
    what:
      'gaps[0] — seqs 1,438–1,441 absent; the ledger jumps 1,437 → 1,442. Four entries were never written, or removed after.',
    device_id: 'dev-ci-runner-4',
    ref: { device_id: 'dev-ci-runner-4', seq_start: 1_438, seq_end: 1_441 },
  },
  {
    id: 'f_c31e884f',
    kind: 'failed_rotation',
    severity: 'bad',
    what:
      'failed_rotation[0] — key_rotated at seq 31,884, but no batch signed with the new key has been accepted since: 11h shipping under the retired key.',
    device_id: 'd1-ops-runner-05',
    ref: { device_id: 'd1-ops-runner-05', seq: 31_884 },
  },
  {
    id: 'f_5d02a9e3',
    kind: 'stale_key_usage',
    severity: 'warn',
    what:
      'stale_key_usage[0..3] — 4 entries signed with device key k-8c41…, retired at seq 1,502.',
    device_id: 'dev-mbp-01',
    ref: { device_id: 'dev-mbp-01', seq_start: 1_689, seq_end: 1_733 },
  },
  {
    id: 'f_7fe1b640',
    kind: 'unverified_ranges',
    severity: 'unknown',
    what:
      'unverified_ranges[] — seqs 40,110–52,377 accepted but never covered by a verify job. Not a pass: nothing is known about these 12,267 entries either way.',
    device_id: 'd1-batch-worker-03',
    ref: { device_id: 'd1-batch-worker-03', seq_start: 40_110, seq_end: 52_377 },
  },
  {
    id: 'f_e408c2aa',
    kind: 'unverified_ranges',
    severity: 'unknown',
    what:
      'unverified_ranges[] — seqs 40,110–52,377 accepted but never covered by a verify job. Same seq range as d1-batch-worker-03 and entirely unrelated to it.',
    device_id: 'dev-ci-runner-9',
    ref: { device_id: 'dev-ci-runner-9', seq_start: 40_110, seq_end: 52_377 },
  },
  {
    id: 'f_11c7d930',
    kind: 'unpaired_tool_uses',
    severity: 'warn',
    what:
      'unpaired_tool_uses[0..2] — 3 tool_use entries in session sess_0b9e41c7 have no matching tool_result; the stream was aborted mid-call.',
    device_id: 'd1-ops-runner-02',
    ref: { device_id: 'd1-ops-runner-02', session_id: 'sess_0b9e41c7' },
  },
  {
    id: 'f_36ba5e18',
    kind: 'orphan_tool_results',
    severity: 'warn',
    what:
      'orphan_tool_results[0] — a tool_result arrived for tool_use id tu_4c17…, which does not appear anywhere in this device’s chain.',
    device_id: 'd1-sales-bot-03',
    ref: null,
  },
]

/* ------------------------------------------------------------------ *
 * Coverage — frame 9, the silence report
 * ------------------------------------------------------------------ */

export const COVERAGE: CoverageReport = {
  tenant: TENANT,
  as_of: NOW_ISO,
  enrolled: DEVICES.length,
  never_seen: DEVICES.filter((d) => d.liveness === 'never_seen'),
  // Ranked by overdue multiple, not by wall-clock silence: six hours means one
  // thing on a five-minute shipper and another on a nightly one.
  silent: DEVICES.filter((d) => d.liveness === 'silent').slice().sort(
    (a, b) => (b.overdue_multiple ?? 0) - (a.overdue_multiple ?? 0),
  ),
  unverified_ranges: [
    {
      device_id: 'd1-ops-runner-02',
      seq_start: 1,
      seq_end: 84_213,
      seqs: 84_213,
      accepted_from: '2026-06-02T09:00:00.000Z',
      accepted_to: ago(19 * HOUR + 42 * MIN),
      last_verify_walk: null,
      unverified_for_s: 85 * DAY + 6 * HOUR,
    },
    {
      device_id: 'd1-batch-worker-03',
      seq_start: 40_110,
      seq_end: 52_377,
      seqs: 12_268,
      accepted_from: '2026-08-02T04:19:00.000Z',
      accepted_to: ago(11 * MIN),
      last_verify_walk: null,
      unverified_for_s: 24 * DAY + 7 * HOUR,
    },
    {
      device_id: 'dev-ci-runner-9',
      seq_start: 40_110,
      seq_end: 52_377,
      // COLLISION, stated on purpose: byte-identical seq range to the row
      // above, on a different device, covering entirely different events.
      seqs: 12_268,
      accepted_from: '2026-08-17T12:03:00.000Z',
      accepted_to: ago(4 * MIN),
      last_verify_walk: null,
      unverified_for_s: 8 * DAY + 23 * HOUR,
    },
    {
      device_id: 'd1-edge-gateway-02',
      seq_start: 3_001,
      seq_end: 4_118,
      seqs: 1_118,
      accepted_from: '2026-08-24T08:55:00.000Z',
      accepted_to: ago(2 * HOUR + 14 * MIN),
      last_verify_walk: null,
      unverified_for_s: 2 * DAY + 3 * HOUR,
    },
  ],
  checkpoint_gaps: {
    sessions_without_checkpoint: 11,
    checkpoints_never_countersigned: 4,
    detail: [
      {
        device_id: 'd1-ops-runner-02',
        what:
          'sess_0b9e41c7 covers seqs 1,199,204–1,204,882 with zero checkpoint rows — the oldest of 11 such sessions.',
        quiet_for_s: 12 * DAY + 4 * HOUR,
        count: 11,
      },
      {
        device_id: 'd1-batch-worker-03',
        what:
          'checkpoint at seq_end 52,377 (root 9f2c4a1e77b0d3…) shipped, countersigned_at null; synced_checkpoint_up_to_seq never advanced past 40,109.',
        quiet_for_s: 24 * DAY + 7 * HOUR,
        count: 4,
      },
    ],
  },
  ungoverned_agents: [
    {
      agent_id: 'invoice-reconciler',
      device_id: 'd1-batch-worker-07',
      tool_use_count: 1_942,
      mandate_verdict_count: 0,
      reason: 'mandate_version_id null on every step — no snapshot was ever attached',
      quiet_for_s: 11 * DAY + 6 * HOUR,
    },
    {
      agent_id: 'support-triage',
      device_id: 'd1-sales-bot-03',
      tool_use_count: 318,
      mandate_verdict_count: 0,
      reason: 'snapshot lost at key_rotated seq 77,309 and never reloaded',
      quiet_for_s: 8 * DAY + 23 * HOUR,
    },
    {
      agent_id: 'repo-janitor',
      device_id: 'd1-edge-gateway-02',
      tool_use_count: 57,
      mandate_verdict_count: 0,
      reason: 'observe-only, no mandate attached',
      quiet_for_s: 2 * DAY + 3 * HOUR,
    },
  ],
  blind_spot: {
    basis: 'device_enrolments',
    statement:
      'Every count on this screen is computed against device_enrolments — 40 rows. A host that never ran `byoai enroll` produces no device_id, ships no batch and raises no finding: it is absent from every number here, including the denominator of 40. It is not counted as silent. It is not counted at all.',
    defensible_claim:
      'Complete across 40 enrolled devices, with 3 of them unaccounted for — never simply "complete".',
  },
}

/* ------------------------------------------------------------------ *
 * Response envelopes
 * ------------------------------------------------------------------ */

export const DEVICE_LIST = {
  inclusion: INCLUSION,
  devices: DEVICES,
  next_cursor: null,
} as const

export const FINDING_LIST = {
  inclusion: INCLUSION,
  findings: FINDINGS,
  // The panel shows 8 of 23; the rest are behind "all 23 →".
  total: 23,
} as const
