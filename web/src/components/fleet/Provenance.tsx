import type { Inclusion } from '@/api/schemas'

/**
 * The denominator that travels with every aggregate on this screen. Rule 9:
 * never sum across devices without showing devices_included / enrolled.
 */
export function Provenance({ inclusion, note }: { inclusion: Inclusion; note?: string }) {
  return (
    <div className="provenance">
      devices_included <b>{inclusion.devices_included}</b> / devices_enrolled{' '}
      <b>{inclusion.devices_enrolled}</b>
      {note === undefined ? null : <> · {note}</>}
    </div>
  )
}
