/**
 * `/console/{tenant}` — a tenant with no section named. Fleet is the default
 * landing surface (§6.0): the aggregate is the altitude the product is read
 * at, so it is where an unqualified URL lands. Scope survives the redirect.
 */
import { createFileRoute, redirect } from '@tanstack/react-router'

export const Route = createFileRoute('/console/$tenant/')({
  beforeLoad: ({ params, search }) => {
    throw redirect({
      to: '/console/$tenant/fleet',
      params: { tenant: params.tenant },
      search,
    })
  },
})
