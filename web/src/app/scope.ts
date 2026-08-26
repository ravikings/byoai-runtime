/**
 * Scope lives in the URL. Nothing else.
 *
 * Spec §4.5: "no view state lives only in React state; back/forward always
 * work; a pasted URL reconstructs the exact screen." The tenant is a path
 * segment (`/console/{tenant}/…`) because it is identity, not a filter; the
 * scope is the query string because it is a filter *within* that tenant and
 * can never widen past it.
 *
 * The wire shape is deliberately terse and human-typeable — an on-call
 * engineer edits these by hand in an incident channel:
 *
 *   ?devices=d1-ops-runner-02,d1-batch-worker-07
 *   ?agents=agt_4d19b7c2&window=7d
 *   ?trajectory=trj_9f2c4a1e&from=2026-08-25T11:20:00Z&to=2026-08-26T11:20:00Z
 *   ?mandate=mv_2026_08_11
 */
import { createContext, useContext, useMemo } from 'react'
import { useParams, useSearch } from '@tanstack/react-router'
import type { Inclusion, Scope } from '../api/schemas'

/* ------------------------------------------------------------------ *
 * The search-param shape
 * ------------------------------------------------------------------ */

/** Named windows, so the common case is one short token in the URL. */
export const WINDOWS = ['1h', '24h', '7d', '30d', 'all'] as const
export type Window = (typeof WINDOWS)[number]
export const DEFAULT_WINDOW: Window = '24h'

const WINDOW_SECONDS: Record<Exclude<Window, 'all'>, number> = {
  '1h': 3600,
  '24h': 86_400,
  '7d': 604_800,
  '30d': 2_592_000,
}

export const WINDOW_LABEL: Record<Window, string> = {
  '1h': 'last 1h',
  '24h': 'last 24h',
  '7d': 'last 7 days',
  '30d': 'last 30 days',
  all: 'all time',
}

/**
 * Every key here is optional: an absent key means "not filtered", which is
 * not the same as an empty list. `devices=` (empty) would mean "no devices",
 * a scope that can only ever count to zero, so it is normalised away.
 */
export interface ScopeSearch {
  readonly devices?: string[]
  readonly agents?: string[]
  readonly trajectory?: string
  readonly window?: Window
  /** Explicit bounds beat `window` when both are present. */
  readonly from?: string
  readonly to?: string
  readonly mandate?: string
}

function isWindow(v: unknown): v is Window {
  return typeof v === 'string' && (WINDOWS as readonly string[]).includes(v)
}

/** `a,b,,a` → `['a','b']`. Order preserved, blanks and dupes dropped. */
/**
 * The one normalisation rule for comma-separated ids — split, trim, drop
 * blanks, dedupe. Exported because the scope picker parses the same input by
 * hand otherwise, and ids typed into the picker must normalise identically to
 * ids arriving in a URL or the two disagree about what the scope is.
 */
export function csvList(raw: unknown): string[] | undefined {
  // Both forms are supported and may be MIXED in one URL
  // (`?devices=a,b&devices=c`), so every element gets split, not just the
  // single-string case. Without this the array branch kept "a,b" as one
  // literal id and the fleet silently narrowed to a device that cannot exist
  // while the chip claimed two were selected.
  const parts =
    typeof raw === 'string'
      ? raw.split(',')
      : Array.isArray(raw)
        ? raw.flatMap((p) => (typeof p === 'string' ? p.split(',') : []))
        : []
  const out: string[] = []
  for (const part of parts) {
    const v = part.trim()
    if (v !== '' && !out.includes(v)) out.push(v)
  }
  return out.length > 0 ? out : undefined
}

function str(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined
  const v = raw.trim()
  return v === '' ? undefined : v
}

/**
 * Router `validateSearch`. Never throws: a hand-edited URL with a typo in it
 * degrades to a wider, honestly-labelled scope rather than an error screen —
 * but the scope chip always states what actually got applied, so the widening
 * is never silent.
 */
export function parseScopeSearch(raw: Record<string, unknown>): ScopeSearch {
  const out: {
    devices?: string[]
    agents?: string[]
    trajectory?: string
    window?: Window
    from?: string
    to?: string
    mandate?: string
  } = {}
  const devices = csvList(raw['devices'])
  if (devices) out.devices = devices
  const agents = csvList(raw['agents'])
  if (agents) out.agents = agents
  const trajectory = str(raw['trajectory'])
  if (trajectory) out.trajectory = trajectory
  if (isWindow(raw['window'])) out.window = raw['window']
  const from = str(raw['from'])
  if (from) out.from = from
  const to = str(raw['to'])
  if (to) out.to = to
  const mandate = str(raw['mandate'])
  if (mandate) out.mandate = mandate
  return out
}

/** Serialise back to the query string. Defaults are omitted, so the common
 *  scope produces a clean, short, pasteable URL. */
export function scopeSearchToParams(s: ScopeSearch): Record<string, string> {
  const p: Record<string, string> = {}
  if (s.devices && s.devices.length > 0) p['devices'] = s.devices.join(',')
  if (s.agents && s.agents.length > 0) p['agents'] = s.agents.join(',')
  if (s.trajectory) p['trajectory'] = s.trajectory
  if (s.window && s.window !== DEFAULT_WINDOW) p['window'] = s.window
  if (s.from) p['from'] = s.from
  if (s.to) p['to'] = s.to
  if (s.mandate) p['mandate'] = s.mandate
  return p
}

/* ------------------------------------------------------------------ *
 * Search params → the API's Scope
 * ------------------------------------------------------------------ */

/**
 * Floors an instant to the minute.
 *
 * This is not cosmetic. A relative window recomputed from an unfloored
 * `new Date()` yields a different `from`/`to` on every render, which changes
 * the react-query cache key on every render, which refetches, which
 * re-renders — a loop that never settles and presents as a screen stuck
 * loading forever. It is applied here, at the source, rather than only in
 * `useScope`, so a caller that reaches for `toScope` directly cannot
 * reintroduce it.
 */
function floorToMinute(d: Date): Date {
  return new Date(Math.floor(d.getTime() / 60_000) * 60_000)
}

export function windowBounds(
  w: Window,
  now: Date = new Date(),
): { from?: string; to?: string } {
  if (w === 'all') return {}
  const to = floorToMinute(now)
  const from = new Date(to.getTime() - WINDOW_SECONDS[w] * 1000)
  return { from: from.toISOString(), to: to.toISOString() }
}

/**
 * The scope actually sent to the API. Explicit `from`/`to` win over the named
 * window — a permalink pasted into an incident channel must resolve to the
 * same absolute slice tomorrow as it did today, which a relative window
 * cannot promise.
 */
export function toScope(
  tenant: string,
  s: ScopeSearch,
  now: Date = new Date(),
): Scope {
  // An explicit bound on EITHER side means the caller is naming an absolute
  // slice, so the relative window must not fill in the other side. Mixing them
  // — `?from=…` with `to` silently defaulted to now — would break the promise
  // above: the same permalink would resolve to a wider range every hour, and
  // two people opening one link would see different data. An open-ended
  // absolute range is a real answer; a half-relative one is not.
  const explicit = s.from !== undefined || s.to !== undefined
  const named = explicit
    ? { from: undefined, to: undefined }
    : windowBounds(s.window ?? DEFAULT_WINDOW, now)
  const from = s.from ?? named.from
  const to = s.to ?? named.to
  const scope: {
    tenant: string
    device_ids?: string[]
    agent_ids?: string[]
    trajectory_id?: string
    from?: string
    to?: string
    mandate_version_id?: string
  } = { tenant }
  if (s.devices) scope.device_ids = s.devices
  if (s.agents) scope.agent_ids = s.agents
  if (s.trajectory) scope.trajectory_id = s.trajectory
  if (from) scope.from = from
  if (to) scope.to = to
  if (s.mandate) scope.mandate_version_id = s.mandate
  return scope
}

/* ------------------------------------------------------------------ *
 * The words the chip and the scope line say
 * ------------------------------------------------------------------ */

/**
 * What the scope selects, in words. Never returns an empty string: an
 * unfiltered scope is still a scope, and "all devices" is a claim the user
 * needs to see stated (§6.1).
 */
export function scopeSubject(s: ScopeSearch): string {
  const parts: string[] = []
  // Trajectory used to return early here, which dropped a co-applied mandate
  // exactly as the devices/agents path once did. Every restriction the API is
  // given must appear in the words on screen; there is no branch where one of
  // them may be left out.
  if (s.trajectory) parts.push(`trajectory ${s.trajectory}`)
  // A mandate restriction narrows every figure on screen exactly as a device
  // filter does. Omitting it here rendered a mandate-scoped URL as the
  // unqualified words "all devices" — the applied restriction stated nowhere,
  // which is the silent widening this whole module exists to prevent.
  if (s.mandate) parts.push(`mandate ${s.mandate}`)
  if (s.devices) {
    const first = s.devices[0]
    parts.push(
      s.devices.length === 1 && first !== undefined
        ? `device ${first}`
        : `${s.devices.length} devices`,
    )
  }
  if (s.agents) {
    const first = s.agents[0]
    parts.push(
      s.agents.length === 1 && first !== undefined
        ? `agent ${first}`
        : `${s.agents.length} agents`,
    )
  }
  if (parts.length === 0) return 'all devices'
  return parts.join(' · ')
}

export function windowLabel(s: ScopeSearch): string {
  if (s.from || s.to) {
    const from = s.from ? shortStamp(s.from) : '…'
    const to = s.to ? shortStamp(s.to) : 'now'
    return `${from} → ${to} UTC`
  }
  return WINDOW_LABEL[s.window ?? DEFAULT_WINDOW]
}

/** `2026-08-26T11:20:00Z` → `2026-08-26 11:20`. Left alone if unparseable —
 *  a bad timestamp is shown as it arrived rather than as `Invalid Date`. */
export function shortStamp(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toISOString().replace('T', ' ').slice(0, 16)
}

/* ------------------------------------------------------------------ *
 * Hooks — the URL is the only source of scope
 * ------------------------------------------------------------------ */

/** The raw search-param scope, straight off the URL. */
export function useScopeSearch(): ScopeSearch {
  const raw = useSearch({ strict: false })
  return useMemo(() => parseScopeSearch(raw), [raw])
}

/** The tenant from the path. `/` (which only redirects) has none. */
export function useTenant(): string {
  const params = useParams({ strict: false })
  const t = params['tenant']
  return typeof t === 'string' && t !== '' ? t : DEFAULT_TENANT
}

/**
 * The scope every data hook takes: the URL's selection resolved against a
 * concrete window.
 *
 * The window edge is floored to the minute on purpose. A relative window
 * recomputed from `Date.now()` on every render would produce a new `from`/`to`
 * pair each time, which means a new query key each time, which means a fetch
 * loop. Minute granularity keeps the identity stable without pretending the
 * window is frozen.
 */
export function useScope(): Scope {
  const tenant = useTenant()
  const search = useScopeSearch()
  const minute = Math.floor(Date.now() / 60_000)
  return useMemo(
    () => toScope(tenant, search, new Date(minute * 60_000)),
    [tenant, search, minute],
  )
}

/* ------------------------------------------------------------------ *
 * Shell status — what the screen has actually loaded
 * ------------------------------------------------------------------ */

/**
 * The chip and the health dots describe data the *page* fetched, so the page
 * publishes it upward rather than the shell fetching a second copy. Until a
 * page publishes, the shell says so: unknown is a state, not a blank.
 */
export type DotState = 'ok' | 'warn' | 'bad' | 'unknown'

export interface HealthRollup {
  readonly state: DotState
  /** Short state word: "intact", "degraded", "broken", "unknown". */
  readonly state_label: string
  /** The worst member, named. "3 of 40 devices silent > 24h". */
  readonly worst: string
  /** Where the dot jumps to. A dot is a jump link, not decoration (§4.2). */
  readonly href?: string
}

export interface ShellStatus {
  readonly inclusion?: Inclusion
  readonly coverage?: HealthRollup
  readonly integrity?: HealthRollup
  readonly ingest?: HealthRollup
}

export type PublishShellStatus = (status: ShellStatus | null) => void

export const ShellStatusContext = createContext<ShellStatus>({})
export const PublishShellStatusContext = createContext<PublishShellStatus>(
  () => undefined,
)

export function useShellStatus(): ShellStatus {
  return useContext(ShellStatusContext)
}

/**
 * Page routes call this and hand the shell what they loaded:
 *
 *   const publish = usePublishShellStatus()
 *   useEffect(() => { publish({ inclusion, coverage, integrity, ingest })
 *                     return () => publish(null) }, [publish, data])
 */
export function usePublishShellStatus(): PublishShellStatus {
  return useContext(PublishShellStatusContext)
}

/* ------------------------------------------------------------------ *
 * Tenancy
 * ------------------------------------------------------------------ */

/**
 * The tenant used when the URL does not name one — only `/` ever hits this,
 * and only to redirect. Tenancy is the data model (§5), so the tenant is
 * never inferred once a real URL exists: it is read from the path.
 */
export const DEFAULT_TENANT: string =
  typeof import.meta.env.VITE_BYOAI_TENANT === 'string' &&
  import.meta.env.VITE_BYOAI_TENANT !== ''
    ? import.meta.env.VITE_BYOAI_TENANT
    : 'acme-prod'
