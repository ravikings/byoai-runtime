/**
 * The runtime (proxy) client — savings figures, history, and the optimizer
 * toggle. Two rules distinguish this file from the fleet client:
 *
 * 1. These endpoints are the *caching proxy's* (`/v1/stats*`, `/v1/toggle`),
 *    not the fleet console API's, and not every host that serves this SPA
 *    runs a proxy. The same React app answers on the proxy (:8787), the
 *    Shield server (:17831) and the org server (:17840); the runtime
 *    figures exist on exactly one of them. A 404 from the root API is
 *    therefore not a failure to retry — it is a fact about the host, and
 *    `runtimeAvailability` names it so the screen can say "this host does
 *    not run a caching proxy" instead of a spinner, a red error, or (worst
 *    of all) a wall of zeros that reads as a quiet proxy.
 *
 * 2. Every caveat string the API returns — `methodology`, `scope_note`,
 *    `sample_size_caveat` — is rendered as first-class UI (the
 *    honesty is the feature). Nothing is collapsed into a tooltip.
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { z } from 'zod'
import { apiFetch, HttpError } from './client'
import { EstimatedStats, PermanentStats, StatsHistory } from './schemas'

const ROOT = '/v1'

export type RuntimeAvailability = 'served' | 'not-on-this-host' | 'auth-required' | 'unknown'

/** Classify a stats-endpoint failure by what it says about the host.
 *  `unknown` because react-query types errors as Error even though every
 *  failure this client can see is one of the four ApiError kinds. */
export function runtimeAvailability(error: unknown): RuntimeAvailability {
  if (error === null) return 'served'
  if (error instanceof HttpError) {
    if (error.status === 404) return 'not-on-this-host'
    if (error.status === 401 || error.status === 403) return 'auth-required'
  }
  return 'unknown'
}

export function useStatsEstimate() {
  return useQuery({
    queryKey: ['runtime', 'stats'] as const,
    queryFn: ({ signal }) =>
      apiFetch('/stats', EstimatedStats, { base: ROOT, signal }),
    staleTime: 15_000,
    refetchInterval: 30_000,
    // A 404 here is an answer about the host, not a transient failure;
    // retrying a fact is how a degrade panel blinks as an error loop.
    retry: (count, err) => !(err instanceof HttpError && err.status === 404) && count < 2,
  })
}

export function usePermanentStats() {
  return useQuery({
    queryKey: ['runtime', 'stats', 'permanent'] as const,
    queryFn: ({ signal }) =>
      apiFetch('/stats/permanent', PermanentStats, { base: ROOT, signal }),
    staleTime: 60_000,
    retry: (count, err) => !(err instanceof HttpError && err.status === 404) && count < 2,
  })
}

export function useStatsHistory() {
  return useQuery({
    queryKey: ['runtime', 'stats', 'history'] as const,
    queryFn: ({ signal }) =>
      apiFetch('/stats/history', StatsHistory, { base: ROOT, query: { limit: 200 }, signal }),
    staleTime: 60_000,
    retry: (count, err) => !(err instanceof HttpError && err.status === 404) && count < 2,
  })
}

export type ToggleResult = z.infer<typeof toggleResponse>

const toggleResponse = z.object({
  status: z.string(),
  optimizer_enabled: z.boolean(),
  message: z.string(),
})

/**
 * `/v1/toggle` flips a live proxy behaviour, so the mutation is optimistic
 * with an explicit rollback: the switch answers the operator instantly, and
 * if the POST fails the prior state is restored and the failure is shown —
 * a toggle that silently stays half-optimistic is a control lying about the
 * system, which is the one thing a control may never do.
 */
export function useOptimizerToggle(enabled: boolean) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const res = await fetch(`${ROOT}/toggle`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      })
      const text = await res.text()
      if (!res.ok) throw new HttpError(`${ROOT}/toggle`, res.status, res.statusText, text)
      let json: unknown
      try {
        json = JSON.parse(text) as unknown
      } catch {
        json = null
      }
      return toggleResponse.parse(json)
    },
    onMutate: async () => {
      await qc.cancelQueries({ queryKey: ['runtime', 'stats'] })
      const previous = qc.getQueryData(['runtime', 'stats'])
      if (previous && typeof previous === 'object' && 'optimizer_enabled' in previous) {
        qc.setQueryData(['runtime', 'stats'], {
          ...(previous as object),
          optimizer_enabled: !enabled,
        })
      }
      return { previous }
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.previous !== undefined) qc.setQueryData(['runtime', 'stats'], ctx.previous)
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['runtime', 'stats'] })
      void qc.invalidateQueries({ queryKey: ['runtime', 'stats', 'permanent'] })
    },
  })
}
