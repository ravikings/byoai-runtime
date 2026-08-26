/**
 * The console's entry point.
 *
 * Two deliberate choices:
 *  - The MSW worker starts only in dev, and `main` awaits it before the first
 *    render. Starting it after mount races the first query, which produces an
 *    intermittently empty first paint — the exact class of bug this product
 *    is supposed to be embarrassed by.
 *  - Queries are stale-while-revalidate (§5) but never silently so: screens
 *    stamp "updated Ns ago" from `dataUpdatedAt`.
 */
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createRouter } from '@tanstack/react-router'
import { routeTree } from './routeTree.gen'
import './styles/tokens.css'
import './styles/components.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: true,
      retry: 1,
    },
  },
})

const router = createRouter({ routeTree, defaultPreload: 'intent' })

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}

async function startMocks(): Promise<void> {
  if (!import.meta.env.DEV) return
  let mod: { worker?: { start: (opts?: unknown) => Promise<unknown> } }
  try {
    mod = await import('./mocks/browser')
  } catch (err) {
    // Report unconditionally. An earlier version tried to tell "module absent"
    // (expected) from "module broken" (a bug) by matching the error text — but
    // a missing transitive import reads as "Cannot find module" too, so real
    // breakage was classified as expected and swallowed. The wording is also
    // engine- and bundler-specific, so the test was never dependable.
    //
    // This whole path only runs under `import.meta.env.DEV`, where
    // mocks/browser is always present, so any failure here is a bug worth
    // saying out loud rather than a case worth guessing at.
    console.error(
      '[console] mocks/browser did not load — the app is talking to the real /v1 proxy, not mocks.',
      err,
    )
    return
  }
  try {
    await mod.worker?.start({ onUnhandledRequest: 'bypass' })
  } catch (err) {
    // A worker that exists but fails to register is a broken dev environment,
    // not an intended configuration — silently falling through to the real
    // proxy makes a misconfiguration look deliberate. Say so loudly; the two
    // failures are separated above precisely so this one stays visible.
    console.error(
      '[console] MSW worker failed to start — the app is now talking to the real /v1 proxy, not mocks.',
      err,
    )
  }
}

async function main(): Promise<void> {
  await startMocks()
  const el = document.getElementById('root')
  if (!el) throw new Error('#root is missing from index.html')
  createRoot(el).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </StrictMode>,
  )
}

void main()
