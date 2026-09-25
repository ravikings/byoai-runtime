/**
 * Shield moved out of the admin console to `/shield`: it is the screen for
 * the person at one Mac, not a fleet view. Old links land there.
 */
import { createFileRoute, redirect } from '@tanstack/react-router'

export const Route = createFileRoute('/console/$tenant/shield')({
  beforeLoad: ({ location }) => {
    // Keep the old link's tab and focus, so it still lands on its item.
    const s = location.search as Record<string, unknown>
    throw redirect({
      to: '/shield',
      search: {
        tab: typeof s.tab === 'string' ? s.tab as 'trust' : undefined,
        focus: typeof s.focus === 'string' ? s.focus : undefined,
      },
    })
  },
})
