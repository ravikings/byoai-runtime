import { createFileRoute } from '@tanstack/react-router'
import { PolicyPanel } from '@/components/shieldServer/PolicyPanel'

export const Route = createFileRoute('/console/$tenant/shield-server/policy')({
  component: () => (
    <>
      <h1>Shield policy</h1>
      <PolicyPanel />
    </>
  ),
})
