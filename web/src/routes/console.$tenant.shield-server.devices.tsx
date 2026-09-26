import { createFileRoute } from '@tanstack/react-router'
import { DevicesPanel } from '@/components/shieldServer/DevicesPanel'

export const Route = createFileRoute('/console/$tenant/shield-server/devices')({
  component: () => (
    <>
      <h1>Shield devices</h1>
      <DevicesPanel />
    </>
  ),
})
