/**
 * Scope-preserving links.
 *
 * A link that navigates away from a scoped view must carry the slice with it.
 * Without that, an operator narrows to three devices over seven days, clicks
 * through to Coverage, and is shown the whole fleet over 24h with nothing
 * saying it changed — the opposite of the URL-as-addressing design (spec §4.5).
 *
 * The scope travels through React context rather than a module-level variable.
 * An earlier revision kept it in a module global written during Shell's render:
 * that made `href.*` impure, went stale in any closure that captured it (the
 * keyboard handler did exactly this), and silently produced unscoped links for
 * any caller outside the Shell tree, tests included. Context has none of those
 * failure modes and React already guarantees the freshness this needs.
 *
 * `useHref()` returns the same shape as the plain `href` map, so a component
 * adopts it by swapping the import and adding one line — call sites are
 * untouched, which is what keeps the next new link from quietly forgetting.
 */
import { createContext, useContext, useMemo } from 'react'
import type { ReactNode } from 'react'
import { href as baseHref } from '@/components/fleet/format'

const ScopeQSContext = createContext<string>('')

export function ScopeQSProvider({ value, children }: { value: string; children: ReactNode }) {
  return <ScopeQSContext.Provider value={value}>{children}</ScopeQSContext.Provider>
}

export function useScopeQS(): string {
  return useContext(ScopeQSContext)
}

function append(path: string, qs: string): string {
  if (qs === '') return path
  return path.includes('?') ? `${path}&${qs}` : `${path}?${qs}`
}

export type HrefMap = typeof baseHref

/** The `href` map with the active scope appended to every route it builds. */
export function useHref(): HrefMap {
  const qs = useScopeQS()
  return useMemo(() => {
    const out = {} as Record<string, unknown>
    for (const [key, fn] of Object.entries(baseHref)) {
      out[key] = (...args: unknown[]) =>
        append((fn as (...a: unknown[]) => string)(...args), qs)
    }
    return out as HrefMap
  }, [qs])
}
