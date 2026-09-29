/**
 * The console's API contract.
 *
 * These schemas are not defensive decoration — they are the specification the
 * ingest backend implements. They exist because this product's
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
 * Scope
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
 * Fleet
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
  /**
   * Present only when the summary comes from the local Shield's own seal
   * chain — the one host where integrity is produced on the read path rather
   * than deferred to a verify job. When it is here, the console can name the
   * root it checked and the moment it checked, instead of just a rollup.
   * Absent everywhere else: a fleet server cannot re-walk a device's chain
   * and must not pretend it verified something it stored.
   */
  local: z.object({
    device_id: z.string(),
    host: z.string(),
    merkle_root: z.string().nullable(),
    sealed_total: z.number().int(),
    chain_verified_at: z.string(),
    incidents: z.number().int(),
    connected: z.boolean(),
    coriqo_tenant: z.string().nullable(),
  }).optional(),
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
  /** How many batches a receiving server took. Null on the local host, which
   *  cannot observe its own shipment's arrival — zero there would report
   *  "nothing received" as a fact when the truth is "nobody here can see". */
  batches_received: z.number().int().nullable(),
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
 * Coverage — the silence report
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
 * Verify. A fleet verdict is a rollup of per-device verdicts,
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
 * Mandate — the verdict stream
 * ------------------------------------------------------------------ */

export const VerdictEvent = z.object({
  device_id: z.string(),
  /** The sealed height this verdict is addressable at — null when the
   *  verdict is not in the chain window the host keeps. Never rendered as
   *  a bare number: a seq is only an address with its device. */
  seq: z.number().int().nonnegative().nullable(),
  ts: z.string(),
  surface: z.string(),
  tool: z.string().nullable(),
  agent_id: z.string().nullable(),
  verdict: VerdictKind,
  reason: z.string().nullable(),
  chars: z.number().int().nullable(),
})
export type VerdictEvent = z.infer<typeof VerdictEvent>

export const VerdictLatch = z.object({
  device_id: z.string(),
  surface: z.string(),
  tool: z.string().nullable(),
  /** Repeat attempts after the first denial — a lone denial is not a latch,
   *  which is why this counts attempts, not denials. */
  attempts: z.number().int(),
  first_seq: z.number().int().nonnegative().nullable(),
})
export type VerdictLatch = z.infer<typeof VerdictLatch>

export const VerdictStream = z.object({
  tenant: z.string(),
  window: z.object({ from: z.string(), to: z.string() }),
  inclusion: Inclusion,
  rollup: z.object({
    allowed: z.number().int(),
    flagged: z.number().int(),
    denied: z.number().int(),
  }),
  /** Observe mode never blocks, so "would have been stopped" is the whole
   *  story; under enforcement the count is meaningless and the host sends
   *  null, not 0 — an absent measurement is not a zero. */
  enforcement: Enforcement,
  observe_flagged: z.number().int().nullable(),
  latches: z.array(VerdictLatch),
  verdicts: z.array(VerdictEvent),
  next_cursor: z.string().nullable(),
})
export type VerdictStream = z.infer<typeof VerdictStream>

/* ------------------------------------------------------------------ *
 * Ledger — the sealed record
 * ------------------------------------------------------------------ */

export const LedgerEntry = z.object({
  device_id: z.string(),
  seq: z.number().int().nonnegative(),
  ts: z.string().nullable(),
  kind: z.enum(['message', 'tool_use', 'mandate_verdict']),
  surface: z.string(),
  tool: z.string().nullable(),
  agent_id: z.string().nullable(),
  status: z.string().nullable(),
  verdict: z.string().nullable(),
  flags: z.array(z.string()),
  chars: z.number().int().nullable(),
  seal: z.string(),
})
export type LedgerEntry = z.infer<typeof LedgerEntry>

export const LedgerPage = z.object({
  tenant: z.string(),
  window: z.object({ from: z.string(), to: z.string() }),
  inclusion: Inclusion,
  head: z.object({
    height: z.number().int().nonnegative(),
    sealed_total: z.number().int().nonnegative(),
    root: z.string().nullable(),
    as_of: z.string(),
  }),
  /** Gaps in an append-only ledger — the single most alarming thing this UI
   *  can show, so it is computed at read time, not a stored assertion. */
  missing_ranges: z.array(z.object({ from: z.number().int(), to: z.number().int() })),
  /** Ship-side rollups. Null means "this host does not compute it" — distinct
   *  from an empty list, which would mean "computed, none found". The ledger
   *  on a single Mac is the chain itself, not a session/trajectory index. */
  sessions: z.array(z.unknown()).nullable(),
  trajectories: z.array(z.unknown()).nullable(),
  entries: z.array(LedgerEntry),
  next_cursor: z.string().nullable(),
})
export type LedgerPage = z.infer<typeof LedgerPage>

export const EntryDetail = z.object({
  entry: LedgerEntry,
  /** The sealed payload, verbatim — never carrying message text unless the
   *  admin opted into redacted previews. */
  payload: z.record(z.unknown()),
  proof: z.object({
    leaf_index: z.number().int(),
    leaf_hash: z.string(),
    steps: z.array(z.object({ sibling: z.string(), side: z.string() })),
    root_hex: z.string(),
  }).nullable(),
  checkpoint: z.object({
    root: z.string().nullable(),
    height: z.number().int(),
    device_id: z.string(),
    ts: z.string().nullable(),
    covers_this_entry: z.boolean(),
  }).nullable(),
})
export type EntryDetail = z.infer<typeof EntryDetail>

/* ------------------------------------------------------------------ *
 * Enrollment — who this host answers to
 * ------------------------------------------------------------------ */

export const EnrollmentState = z.object({
  tenant: z.string(),
  as_of: z.string(),
  device_id: z.string(),
  chain: z.object({
    sealed_total: z.number().int(),
    height: z.number().int(),
    root: z.string().nullable(),
    tamper_evident: z.boolean(),
    record_id: z.string().nullable(),
  }),
  connection: z.object({
    connected: z.boolean(),
    base_url: z.string().nullable(),
    remote_tenant: z.string().nullable(),
    enrolled_at: z.string().nullable(),
    last_sent_at: z.string().nullable(),
    next_attempt_at: z.string().nullable(),
    every_hours: z.number().nullable(),
    has_new: z.boolean(),
    /** Entries sealed since the last accepted send — knowable locally, and
     *  null when unconnected because there is no ship line to be behind on. */
    unsent_entries: z.number().int().nullable(),
    last_error: z.string().nullable(),
    needs_attention: z.boolean(),
    sync_pending: z.number().int().nullable(),
  }),
  managed: z.object({
    by: z.string().nullable(),
    version: z.number().int().nullable(),
    locked: z.array(z.string()),
    fetched_at: z.unknown().nullable(),
    error: z.string().nullable(),
  }).nullable(),
  /** Invariant 4's disclosure: whenever activity may leave, or an org owns
   *  this device, who and what-in-plain-words, computed from the level — a
   *  level above seal is disclosed even when no organisation is named. */
  sharing: z.object({
    level: z.string(),
    by: z.string(),
    since: z.string().nullable(),
    words: z.string(),
  }).nullable(),
  policy: z.object({
    mode: z.enum(['observe', 'redact', 'block']).nullable(),
    keep_text: z.boolean(),
    retention_days: z.number().int().nullable(),
    /** The effective sharing level after the managed merge — a hand-edited
     *  local `sync` is already collapsed back to `seal` by the loader. */
    sync: z.string(),
    apps: z.record(z.boolean()),
  }),
  stored: z.object({
    ledger_rows: z.number().int(),
    rows_with_text: z.number().int(),
    sealed_with_text: z.number().int(),
    oldest_row: z.string().nullable(),
    less_private: z.boolean(),
    paths: z.object({
      ledger: z.string(),
      seal_state: z.string(),
      seal_log: z.string(),
      policy: z.string().nullable(),
      managed_policy: z.string().nullable(),
      keys: z.string(),
    }),
  }),
})
export type EnrollmentState = z.infer<typeof EnrollmentState>

/* ------------------------------------------------------------------ *
 * Runtime (proxy) — existing endpoints
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
export type PermanentStats = z.infer<typeof PermanentStats>

/**
 * The live Redis estimate (`/v1/stats`). Every number here is a character
 * heuristic (`len(json) // 4`), not a tokenizer reading; the API's own
 * methodology string says so, and the console renders that warning permanently
 * beside the figures. Kept because it updates in real time and shows whether
 * the optimizer is even on; never citable.
 */
export const EstimatedStats = z.object({
  optimizer_enabled: z.boolean(),
  tokens_saved: z.number().int(),
  tokens_original: z.number().int(),
  tokens_sent: z.number().int(),
  savings_percentage: z.string(),
  methodology: z.string(),
})
export type EstimatedStats = z.infer<typeof EstimatedStats>

/** One tokenizer-verified sample: the pre- and post-optimization token counts
 *  for a single real request, measured by Anthropic's count_tokens. Variance
 *  across these is the point — an aggregate over a handful is not a rate. */
export const BenchmarkSample = z.object({
  ts: z.number(),
  session_id: z.string(),
  model: z.string().nullable(),
  real_tokens_original: z.number().int(),
  real_tokens_sent: z.number().int(),
  real_tokens_saved: z.number().int(),
})
export type BenchmarkSample = z.infer<typeof BenchmarkSample>

export const StatsHistory = z.object({
  count: z.number().int(),
  samples: z.array(BenchmarkSample),
})
export type StatsHistory = z.infer<typeof StatsHistory>
