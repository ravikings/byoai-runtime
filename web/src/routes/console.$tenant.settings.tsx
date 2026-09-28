/**
 * Settings section layout. `/console/{tenant}/settings` owns the segment for
 * the enrollment view and the (server-only) surfaces that will follow; a
 * pass-through so its children address real paths, not the not-built splat.
 */
import { Outlet, createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/console/$tenant/settings')({
  component: () => <Outlet />,
})
