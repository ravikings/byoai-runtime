/**
 * `/console` — the app is served under this prefix, so this, not `/`, is the
 * URL a bookmark or a bare hostname actually lands on. It redirects to Fleet
 * for the default tenant; a real tenant in the path always wins over it.
 */
import { createFileRoute, redirect } from '@tanstack/react-router'
import { DEFAULT_TENANT } from '../app/scope'

export const Route = createFileRoute('/console/')({
  // Forward the incoming search. A permalink is journey J2's exit artifact —
  // dropping its scope on the way to the default tenant lands the reader on an
  // unfiltered fleet while they believe they are looking at the sender's slice.
  beforeLoad: ({ search }) => {
    throw redirect({
      to: '/console/$tenant/fleet',
      params: { tenant: DEFAULT_TENANT },
      search,
    })
  },
})
