/**
 * `/console/{tenant}` — the tenant layout. It owns exactly one thing: the
 * scope contract. Every child route inherits `validateSearch`, so scope is
 * parsed once, in one place, and a child cannot invent its own encoding.
 *
 * The tenant is a path segment because it is identity: the scope selector
 * filters *within* a tenant and can never widen past it (§5).
 */
import { Outlet, createFileRoute } from '@tanstack/react-router'
import { parseScopeSearch, type ScopeSearch } from '../app/scope'

export const Route = createFileRoute('/console/$tenant')({
  validateSearch: (search: Record<string, unknown>): ScopeSearch =>
    parseScopeSearch(search),
  component: () => <Outlet />,
})
