/**
 * Evidence section layout. `/console/{tenant}/evidence` owns the URL segment
 * for the findings list and the verify surfaces that follow it; today it is
 * a pass-through so its children address real paths instead of falling to
 * the not-built splat.
 */
import { Outlet, createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/console/$tenant/evidence')({
  component: () => <Outlet />,
})
