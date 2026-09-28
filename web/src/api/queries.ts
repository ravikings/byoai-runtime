/**
 * TanStack Query hooks over the console API.
 *
 * Two conventions, both required by the spec rather than by taste:
 *
 *  1. Every hook takes the whole `Scope` and threads it into the query key via
 *     `scopeKey()` — the same serialiser that builds the request URL. A cache
 *     entry therefore cannot outlive the scope it was fetched for, so a number
 *     on screen can never belong to a scope other than the one in the chip.
 *  2. Every hook re-exports `dataUpdatedAt`. Anything that auto-refreshes must
 *     show visible staleness; a silently stale figure is the same failure as an
 *     unvalidated one, just slower. `dataUpdatedAt` is 0 before the first
 *     success, so screens should render "—" rather than "0s ago" in that case.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import type { z } from 'zod'
import { apiFetch, scopeKey } from './client'
import {
  CoverageReport,
  DeviceList,
  EnrollmentState,
  EntryDetail,
  FindingList,
  FleetSummary,
  LedgerPage,
  VerdictStream,
  type Scope,
} from './schemas'

/**
 * Refetch cadences. Recorders ship on a timer measured in minutes, so polling
 * faster than this buys nothing but makes the "updated Ns ago" stamp lie about
 * how fresh the *underlying* evidence is.
 */
const CADENCE = {
  /** The one-screen summary is the most-watched surface. */
  fleet: { staleTime: 15_000, refetchInterval: 30_000 },
  /** A device list changes only when a batch lands. */
  devices: { staleTime: 30_000, refetchInterval: 60_000 },
  /**
   * Coverage is a list of things that did *not* happen; its inputs move on the
   * scale of hours, and a fast poll would imply a precision it does not have.
   */
  coverage: { staleTime: 60_000, refetchInterval: 120_000 },
  findings: { staleTime: 30_000, refetchInterval: 60_000 },
  /** A verdict stream moves with traffic, so it is watched like the
   *  overview — but a fleet that is idle really is idle, and 30 s is
   *  honest against a recorder that ships on a timer. */
  verdicts: { staleTime: 15_000, refetchInterval: 30_000 },
} as const

/** What every console hook returns: the query result plus an explicit stamp. */
export interface ConsoleQuery<T> {
  readonly query: UseQueryResult<T, Error>
  readonly data: T | undefined
  readonly error: Error | null
  readonly isPending: boolean
  readonly isFetching: boolean
  readonly isError: boolean
  readonly isSuccess: boolean
  /** ms epoch of the last successful fetch; 0 when there has never been one. */
  readonly dataUpdatedAt: number
  readonly refetch: UseQueryResult<T, Error>['refetch']
}

function wrap<T>(query: UseQueryResult<T, Error>): ConsoleQuery<T> {
  return {
    query,
    data: query.data,
    error: query.error,
    isPending: query.isPending,
    isFetching: query.isFetching,
    isError: query.isError,
    isSuccess: query.isSuccess,
    dataUpdatedAt: query.dataUpdatedAt,
    refetch: query.refetch,
  }
}

// `schemas.ts` exports these two as schemas only, not as inferred types.
// Deriving the type here keeps that file's contract shape untouched.
export type DeviceListPayload = z.infer<typeof DeviceList>
export type FindingListPayload = z.infer<typeof FindingList>

export const consoleKeys = {
  fleet: (scope: Scope) => ['console', 'fleet', scopeKey(scope)] as const,
  devices: (scope: Scope) => ['console', 'fleet', 'devices', scopeKey(scope)] as const,
  coverage: (scope: Scope) => ['console', 'fleet', 'coverage', scopeKey(scope)] as const,
  findings: (scope: Scope) => ['console', 'fleet', 'findings', scopeKey(scope)] as const,
  verdicts: (scope: Scope) => ['console', 'verdicts', scopeKey(scope)] as const,
  ledger: (scope: Scope) => ['console', 'ledger', scopeKey(scope)] as const,
  enrollment: (scope: Scope) => ['console', 'enrollment', scopeKey(scope)] as const,
} as const

export function useFleetSummary(scope: Scope): ConsoleQuery<FleetSummary> {
  return wrap(
    useQuery({
      queryKey: consoleKeys.fleet(scope),
      queryFn: ({ signal }) => apiFetch('/fleet', FleetSummary, { scope, signal }),
      ...CADENCE.fleet,
    }),
  )
}

export function useDevices(scope: Scope): ConsoleQuery<DeviceListPayload> {
  return wrap(
    useQuery({
      queryKey: consoleKeys.devices(scope),
      queryFn: ({ signal }) => apiFetch('/fleet/devices', DeviceList, { scope, signal }),
      ...CADENCE.devices,
    }),
  )
}

export function useCoverage(scope: Scope): ConsoleQuery<CoverageReport> {
  return wrap(
    useQuery({
      queryKey: consoleKeys.coverage(scope),
      queryFn: ({ signal }) => apiFetch('/fleet/coverage', CoverageReport, { scope, signal }),
      ...CADENCE.coverage,
    }),
  )
}

export function useFindings(scope: Scope): ConsoleQuery<FindingListPayload> {
  return wrap(
    useQuery({
      queryKey: consoleKeys.findings(scope),
      queryFn: ({ signal }) => apiFetch('/fleet/findings', FindingList, { scope, signal }),
      ...CADENCE.findings,
    }),
  )
}


export type VerdictStreamPayload = import('zod').z.infer<typeof VerdictStream>

export function useVerdicts(scope: Scope): ConsoleQuery<VerdictStreamPayload> {
  return wrap(
    useQuery({
      queryKey: consoleKeys.verdicts(scope),
      queryFn: ({ signal }) => apiFetch('/verdicts', VerdictStream, { scope, signal }),
      ...CADENCE.verdicts,
    }),
  )
}

export type LedgerPayload = import('zod').z.infer<typeof LedgerPage>
export type EntryDetailPayload = import('zod').z.infer<typeof EntryDetail>

/**
 * The ledger's window is deeper than every other screen: it reads the
 * sealed log, not the in-memory feed, so "showing 40 of 4,812" is a page,
 * not a limit the product has on knowledge. The cursor is the oldest seq
 * rendered — a chain height, stable across refetches, so an entry sealed
 * mid-browse cannot shift the page under the reader.
 */
export function useLedger(scope: Scope, cursor: string | null): ConsoleQuery<LedgerPayload> {
  return wrap(
    useQuery({
      queryKey: [...consoleKeys.ledger(scope), cursor ?? ''] as const,
      queryFn: ({ signal }) =>
        apiFetch('/ledger', LedgerPage, {
          scope,
          signal,
          query: cursor ? { before: cursor } : undefined,
        }),
      ...CADENCE.fleet,
    }),
  )
}

/** A seq is an address only beside its device — the URL, the query key and
 *  the endpoint all say both, and no combination of them can be guessed. */
export function useEntry(tenant: string, deviceId: string, seq: string): ConsoleQuery<EntryDetailPayload> {
  return wrap(
    useQuery({
      queryKey: ['console', 'entry', tenant, deviceId, seq] as const,
      queryFn: ({ signal }) =>
        apiFetch(`/entries/${encodeURIComponent(deviceId)}/${encodeURIComponent(seq)}`,
          EntryDetail, { scope: { tenant }, signal }),
      staleTime: 300_000,
    }),
  )
}

export type EnrollmentPayload = import('zod').z.infer<typeof EnrollmentState>

/** Enrollment state is near-static (it changes when a send succeeds or the
 *  managed policy refreshes), so it polls slower than the fleet screens. */
export function useEnrollment(scope: Scope): ConsoleQuery<EnrollmentPayload> {
  return wrap(
    useQuery({
      queryKey: [...consoleKeys.enrollment(scope)] as const,
      queryFn: ({ signal }) => apiFetch('/enrollment', EnrollmentState, { scope, signal }),
      staleTime: 30_000,
      refetchInterval: 60_000,
    }),
  )
}
