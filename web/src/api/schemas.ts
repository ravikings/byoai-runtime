/**
 * The console's API contract.
 *
 * These schemas are not defensive decoration — they are the specification the
 * ingest backend implements (spec §2). They exist because this product's
 * failure mode is not a crash, it is a green tick over data nobody validated.
 *
 * Two rules encoded structurally, not by convention:
 *
 *  1. Tri-states are unions, never booleans. "unverified" is not "intact",
 *     "unchecked" is not "failed", `null` retention is not zero. A boolean
 *     here would collapse a distinction the product exists to preserve.
 *  2. A seq is never addressable without its device_id. Seqs are per-device
 *     and collide across a fleet, so `SeqRef` is the only way to name one.
 */
import { z } from 'zod'

/* ------------------------------------------------------------------ *
 * Primitives
 * ------------------------------------------------------------------ */

/** A seq is meaningless alone — it is an address only when scoped to a device. */
export const SeqRef = z.object({
  device_id: z.string(),
  seq: z.number().int().nonnegative(),
})
export type SeqRef = z.infer<typeof SeqRef>

export const SeqRange = z.object({
  device_id: z.string(),
  seq_start: z.number().int().nonnegative(),
  seq_end: z.number().int().nonnegative(),
})

/**
 * Integrity has three states. `unverified` means no verify walk has ever
 * covered this range — it is not a pass and not a failure, and rendering it
 * as either is the worst bug this UI could ship.
 */
export const IntegrityState = z.enum(['intact', 'broken', 'unverified'])
export type IntegrityState = z.infer<typeof IntegrityState>

/**
 * Key state, same shape of problem. `unchecked` means no public key was
 * supplied, so no signature was found invalid — nothing was proven either way.
 */
export const KeyState = z.enum(['verified', 'unchecked', 'rotating', 'rotation_failed'])
export type KeyState = z.infer<typeof KeyState>

/** Liveness. `never_seen` is distinct from `silent`: one never arrived at all. */
export const LivenessState = z.enum(['reporting', 'late', 'silent', 'never_seen'])
export type LivenessState = z.infer<typeof LivenessState>

export const EventKind = z.enum([
  'tool_use', 'tool_result', 'message', 'api_error', 'record_failure',
  'session_start', 'stream_aborted', 'parse_failure', 'key_rotated',
  'mandate_verdict',
])

export const VerdictKind = z.enum(['allowed', 'flagged', 'denied'])
export const Posture = z.enum(['fail_open', 'fail_closed'])
export const Enforcement = z.enum(['observe', 'enforce'])

/* ------------------------------------------------------------------ *
 * Scope — §2.3
 * ------------------------------------------------------------------ */

export const Scope = z.object({
  tenant: z.string(),
  device_ids: z.array(z.string()).optional(),
  agent_ids: z.array(z.string()).optional(),
  trajectory_id: z.string().optional(),
  from: z.string().datetime().optional(),
  to: z.string().datetime().optional(),
  mandate_version_id: z.string().optional(),
})
export type Scope = z.infer<typeof Scope>

/**
 * Attached to every aggregate figure. An aggregate that silently excludes
 * non-reporting devices is a lie with a number on it, so the denominator
 * travels with the number rather than living in a footnote.
 */
export const Inclusion = z.object({
  devices_included: z.number().int().nonnegative(),
  devices_enrolled: z.number().int().nonnegative(),
})
export type Inclusion = z.infer<typeof Inclusion>

/* ------------------------------------------------------------------ *
 * Fleet — §2.1
 * ------------------------------------------------------------------ */

export const FleetSummary = z.object({
  tenant: z.string(),
  window: z.object({ from: z.string(), to: z.string() }),
  inclusion: Inclusion,
  coverage: z.object({
    reporting: z.number().int(),
    enrolled: z.number().int(),
    silent: z.number().int(),
    never_seen: z.number().int(),
  }),
  integrity: z.object({
    intact: z.number().int(),
    broken: z.number().int(),
    unverified: z.number().int(),
    /** Devices with no verdict at all because they never reported. */
    no_verdict: z.number().int(),
  }),
  ingest: z.object({
    entries_received: z.number().int(),
    /**
     * Backlog is a DEVICE-side fact and the ingest side cannot see it.
     * These describe what a device is still holding and has not sent; a
     * device that stopped shipping looks identical to one with nothing left
     * to ship. Nullable because `0` would state "nothing outstanding" on the
     * strength of data nobody has — which is the failure this console exists
     * to prevent. The UI renders unknown, not zero.
     */
    backlog_entries: z.number().int().nullable(),
    backlog_devices: z.number().int().nullable(),
    oldest_unshipped_at: z.string().nullable(),
    last_batch_at: z.string().nullable(),
    checkpoints_pending: z.number().int().nullable(),
    /** entries/min buckets, oldest first. A flat run is an incident. */
    rate_series: z.array(z.number()),
    rate_flat_for_minutes: z.number().nullable(),
  }),
  denial: z.object({
    denied_per_1k: z.number(),
    previous_per_1k: z.number().nullable(),
    denied: z.number().int(),
    flagged: z.number().int(),
    tool_use_total: z.number().int(),
    top_refused: z.array(z.object({
      tool: z.string(), reason: z.string(), count: z.number().int(),
    })),
  }),
  open_findings: z.number().int(),
})
export type FleetSummary = z.infer<typeof FleetSummary>

export const Device = z.object({
  device_id: z.string(),
  host: z.string(),
  agent_ids: z.array(z.string()),
  liveness: LivenessState,
  enrolled_at: z.string(),
  last_batch_at: z.string().nullable(),
  last_seq_received: z.number().int().nullable(),
  /** Median inter-batch interval, observed not declared. Null when too few
   *  batches to infer — an unknown cadence must never render as on-time. */
  expected_interval_s: z.number().nullable(),
  quiet_for_s: z.number().nullable(),
  overdue_multiple: z.number().nullable(),
  ship_lag_s: z.number().nullable(),
  key_state: KeyState,
  integrity: IntegrityState,
  batches_received: z.number().int(),
})
export type Device = z.infer<typeof Device>

export const DeviceList = z.object({
  inclusion: Inclusion,
  devices: z.array(Device),
  next_cursor: z.string().nullable(),
})
export type DeviceList = z.infer<typeof DeviceList>

/** A single verify-report finding, carrying the device it belongs to. */
export const Finding = z.object({
  id: z.string(),
  kind: z.enum([
    'broken_links', 'gaps', 'bad_signatures', 'stale_key_usage',
    'unpaired_tool_uses', 'orphan_tool_results', 'failed_rotation',
    'unverified_ranges',
  ]),
  severity: z.enum(['bad', 'warn', 'unknown']),
  /** Plain-language sentence, reusing the CLI's own wording. */
  what: z.string(),
  device_id: z.string(),
  ref: z.union([SeqRef, SeqRange, z.object({ session_id: z.string(), device_id: z.string() })]).nullable(),
})
export type Finding = z.infer<typeof Finding>

export const FindingList = z.object({
  inclusion: Inclusion,
  findings: z.array(Finding),
  total: z.number().int(),
})
export type FindingList = z.infer<typeof FindingList>

/* ------------------------------------------------------------------ *
 * Coverage — the silence report, §6.0.2
 * ------------------------------------------------------------------ */

export const CoverageReport = z.object({
  tenant: z.string(),
  as_of: z.string(),
  enrolled: z.number().int(),
  /** Devices enrolled, key registered, zero batches ever received. */
  never_seen: z.array(Device),
  /** Reported once and then stopped, ranked by overdue multiple. */
  silent: z.array(Device),
  /** Stored is not verified. These arrived and were never walked. */
  unverified_ranges: z.array(SeqRange.extend({
    seqs: z.number().int(),
    accepted_from: z.string(),
    accepted_to: z.string(),
    last_verify_walk: z.string().nullable(),
    unverified_for_s: z.number(),
  })),
  checkpoint_gaps: z.object({
    sessions_without_checkpoint: z.number().int(),
    checkpoints_never_countersigned: z.number().int(),
    detail: z.array(z.object({
      device_id: z.string(),
      what: z.string(),
      quiet_for_s: z.number(),
      count: z.number().int(),
    })),
  }),
  /** Ran with no mandate snapshot: not allowed, not denied — unevaluated. */
  ungoverned_agents: z.array(z.object({
    agent_id: z.string(),
    device_id: z.string(),
    tool_use_count: z.number().int(),
    mandate_verdict_count: z.number().int(),
    reason: z.string(),
    quiet_for_s: z.number(),
  })),
  /**
   * The product naming the limit of its own claim. A device that was never
   * enrolled produces no device_id, ships no batch, raises no finding — it is
   * absent from every number here, including the denominator.
   */
  blind_spot: z.object({
    basis: z.literal('device_enrolments'),
    statement: z.string(),
    defensible_claim: z.string(),
  }),
})
export type CoverageReport = z.infer<typeof CoverageReport>

/* ------------------------------------------------------------------ *
 * Verify — §2.2. A fleet verdict is a rollup of per-device verdicts,
 * never a single tick.
 * ------------------------------------------------------------------ */

export const VerifyReport = z.object({
  device_id: z.string(),
  ok: z.boolean(),
  scope: SeqRange.nullable(),
  entries_checked: z.number().int(),
  broken_links: z.array(z.number().int()),
  bad_signatures: z.array(z.number().int()),
  gaps: z.array(z.tuple([z.number().int(), z.number().int()])),
  unpaired_tool_uses: z.array(z.string()),
  orphan_tool_results: z.array(z.string()),
  stale_key_usage: z.array(z.number().int()),
  checkpoints_checked: z.number().int(),
  /** false + no key supplied means UNCHECKED. The boolean alone cannot say
   *  which, so the API must send both. */
  signatures_verified: z.boolean(),
  signature_key_supplied: z.boolean(),
  ts_first: z.string().nullable(),
  ts_last: z.string().nullable(),
  notes: z.array(z.string()),
})
export type VerifyReport = z.infer<typeof VerifyReport>

export const VerifyJob = z.object({
  job_id: z.string(),
  state: z.enum(['queued', 'running', 'done', 'failed']),
  progress: z.number().min(0).max(1),
  /** The head each per-device report was computed against, so the UI can say
   *  "verified as of head 9f2c4a1e…, 12m ago" instead of implying freshness. */
  computed_at: z.string().nullable(),
  reports: z.array(VerifyReport).nullable(),
})

/* ------------------------------------------------------------------ *
 * Runtime (proxy) — existing endpoints, §1.1
 * ------------------------------------------------------------------ */

export const PermanentStats = z.object({
  storage: z.string(),
  benchmark: z.object({
    sample_count: z.number().int(),
    real_tokens_original: z.number().int(),
    real_tokens_sent: z.number().int(),
    real_tokens_saved: z.number().int(),
    real_savings_percentage: z.string(),
  }),
  usage_totals: z.record(z.unknown()),
  /** null means all-time — no prune has run. It does not mean zero. */
  retention_days: z.number().int().nullable(),
  methodology: z.string(),
})
