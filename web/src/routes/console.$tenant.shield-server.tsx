/**
 * Shield server section layout — the console for the free, self-hosted,
 * single-org Shield server (Phase 1.5, `internal_doc/shield_msp_plan.md`).
 * Named `shield-server` rather than `shield` because `/console/$tenant/shield`
 * already redirects to the per-Mac `/shield` screen; this is a different
 * product surface (fleet admin) and must not collide with that route.
 *
 * Wraps every child screen in the admin-token gate: a 401 from any panel
 * opens the same prompt, and the token never leaves this browser tab.
 */
import { Outlet, createFileRoute } from '@tanstack/react-router'
import { AdminAuthProvider, AdminTokenGate } from '@/components/shieldServer/AdminAuth'

export const Route = createFileRoute('/console/$tenant/shield-server')({
  component: () => (
    <AdminAuthProvider>
      <AdminTokenGate>
        <Outlet />
      </AdminTokenGate>
    </AdminAuthProvider>
  ),
})
