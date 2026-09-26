import { createFileRoute } from '@tanstack/react-router'
import { EnrolPanel } from '@/components/shieldServer/EnrolPanel'

export const Route = createFileRoute('/console/$tenant/shield-server/enrol')({
  component: () => (
    <>
      <h1>Enrol a Mac</h1>
      <EnrolPanel />
    </>
  ),
})
