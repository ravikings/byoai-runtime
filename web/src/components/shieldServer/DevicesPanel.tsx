/**
 * Shield devices — the fleet table for the free, self-hosted Shield server
 * (Phase 1.5 console section, `internal_doc/shield_msp_plan.md`).
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchDevices, revokeDevice, type ShieldServerDevice } from '@/api/shieldServer'
import { useAuthWatch } from './AdminAuth'
import { ConfirmDialog } from '@/components/shield/shared'

const POLL_MS = 30_000

function shortId(id: string): string {
  return id.length <= 12 ? id : `${id.slice(0, 8)}…${id.slice(-4)}`
}

function ago(iso: string | null): string {
  if (iso === null) return 'never'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const mins = Math.max(0, Math.round((Date.now() - t) / 60_000))
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const h = Math.round(mins / 60)
  return h < 48 ? `${h}h ago` : `${Math.round(h / 24)}d ago`
}

export function DevicesPanel() {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: ['shield-server', 'devices'],
    queryFn: ({ signal }) => fetchDevices(signal),
    refetchInterval: POLL_MS,
  })
  useAuthWatch(query)

  const [confirming, setConfirming] = useState<ShieldServerDevice | null>(null)
  const revoke = useMutation({
    mutationFn: (deviceId: string) => revokeDevice(deviceId),
    onSuccess: () => {
      setConfirming(null)
      void qc.invalidateQueries({ queryKey: ['shield-server', 'devices'] })
    },
  })

  if (query.isPending) return <p className="empty-row" role="status">Loading devices…</p>
  if (query.isError) {
    return (
      <p className="empty-row" role="alert">
        Couldn't load devices: {query.error.message}
      </p>
    )
  }

  const devices = query.data.devices

  return (
    <section className="panel" aria-labelledby="shield-devices-h">
      <header className="sec-head">
        <h2 className="label" id="shield-devices-h">Shield devices</h2>
      </header>
      {devices.length === 0 ? (
        <p className="empty-row">No devices enrolled yet. Mint a token from Enrol a Mac.</p>
      ) : (
        <table className="table" aria-describedby="shield-devices-h">
          <thead>
            <tr>
              <th>Label</th>
              <th>Device ID</th>
              <th>Last seen</th>
              <th>Status</th>
              <th>Policy</th>
              <th>Drift</th>
              <th>Revoked</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {devices.map((d) => (
              <tr key={d.device_id}>
                <td>{d.label ?? <span className="muted">unlabeled</span>}</td>
                <td className="mono" title={d.device_id}>{shortId(d.device_id)}</td>
                <td>{ago(d.last_seen)}</td>
                <td>
                  {d.protecting === null ? (
                    <span className="tag muted">not heard from yet</span>
                  ) : d.protecting ? (
                    <span className="tag ok">protecting</span>
                  ) : (
                    <span className="tag warn" title={d.reasons.join('; ')}>
                      quiet{d.reasons.length > 0 ? ` — ${d.reasons.join('; ')}` : ''}
                    </span>
                  )}
                </td>
                <td className="mono">
                  {d.policy_version ?? '—'} / {d.assigned_version ?? '—'}
                </td>
                <td>
                  {d.drift ? <span className="tag bad">drift</span> : <span className="tag ok">in sync</span>}
                </td>
                <td>{d.revoked ? <span className="tag bad">revoked</span> : <span className="muted">no</span>}</td>
                <td>
                  {!d.revoked && (
                    <button
                      className="btn ghost sm"
                      type="button"
                      onClick={() => setConfirming(d)}
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {confirming && (
        <ConfirmDialog
          title={`Revoke ${confirming.label ?? shortId(confirming.device_id)}?`}
          confirmLabel={revoke.isPending ? 'Revoking…' : 'Revoke device'}
          busy={revoke.isPending}
          onCancel={() => setConfirming(null)}
          onConfirm={() => revoke.mutate(confirming.device_id)}
        >
          <p>
            This device will be refused on its next request. It stops protecting immediately and
            cannot be un-revoked — re-enrol it with a fresh token if it should come back.
          </p>
          {revoke.isError && <p role="alert">Not revoked: {revoke.error.message}</p>}
        </ConfirmDialog>
      )}
    </section>
  )
}
