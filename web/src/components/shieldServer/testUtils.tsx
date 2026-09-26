import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AdminAuthProvider } from './AdminAuth'

/** Accepts an existing `QueryClient` so a test can trigger its own refetches
 * (e.g. `client.invalidateQueries(...)`) against the same cache the rendered
 * component reads from — otherwise a freshly-constructed client per call
 * gives the test no way to simulate a background refetch. */
export function renderWithClient(children: ReactNode, client?: QueryClient) {
  const qc = client ?? new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return (
    <QueryClientProvider client={qc}>
      <AdminAuthProvider>{children}</AdminAuthProvider>
    </QueryClientProvider>
  )
}

export function newTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
}
