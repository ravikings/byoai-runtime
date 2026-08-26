/**
 * Fleet section layout. `/console/$tenant/fleet` has children (the overview
 * index, the coverage report, the device register), so this file is a
 * pass-through: it owns the URL segment, not the screen. The overview body
 * lives in `console.$tenant.fleet.index.tsx`.
 */
import { Outlet, createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/console/$tenant/fleet')({
  component: () => <Outlet />,
})
