import { createFileRoute, useLocation } from '@tanstack/react-router'
import { NotBuilt } from '@/components/NotBuilt'

// Splat route: catches every not-yet-built destination under a tenant in one
// place, so a link to a designed-but-unimplemented section lands somewhere
// that says so instead of on the router's not-found fallback.
export const Route = createFileRoute('/console/$tenant/$')({
  component: NotBuiltRoute,
})

function NotBuiltRoute() {
  const location = useLocation()
  const { tenant } = Route.useParams()
  return <NotBuilt path={location.pathname} tenant={tenant} />
}
