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
  FindingList,
  FleetSummary,
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

