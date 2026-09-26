/**
 * Mandate section layout. `/console/{tenant}/mandate` owns the URL segment
 * for the verdict stream and the posture surfaces that follow; a pass-through
 * so its children address real paths instead of the not-built splat.
 */
import { Outlet, createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/console/$tenant/mandate')({
  component: () => <Outlet />,
})
