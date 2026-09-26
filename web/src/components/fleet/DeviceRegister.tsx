/**
 * The device register (design frame 10) — one row per enrolled device,
 * quietest first. Two rules the markup exists to enforce, both from spec §2/
 * rule 9: a seq is never rendered without its device (the .seq-scoped token
 * makes the pair indivisible, because seqs are per-device and collide across
 * a fleet), and silence is a state with its own row styling, not an absence
 * of one — a device that stopped shipping is the row an operator came to
 * find, so it sorts and reads louder than a row that is merely old.
 */
import type { Device } from '@/api/schemas'
import { useHref } from '@/app/hrefContext'
import { duration, n, stamp } from './format'

const LIVENESS_RANK: Record<Device['liveness'], number> = {
  never_seen: 0,
  silent: 1,
  late: 2,
  reporting: 3,
}

/** Quietest first: never-heard-from, then gone-quiet (longest silence top),
 *  then late, then the reporting rows by how recently they last shipped. */
export function orderRegister(devices: readonly Device[]): Device[] {
  return devices
    .slice()
    .sort((a, b) => {
      const rank = LIVENESS_RANK[a.liveness] - LIVENESS_RANK[b.liveness]
      if (rank !== 0) return rank
      // Unknown quiet time ranks above a measured one inside a group: an
      // absent value is unbounded, never zero.
      const aq = a.quiet_for_s
      const bq = b.quiet_for_s
      if (aq === null && bq === null) return a.device_id.localeCompare(b.device_id)
      if (aq === null) return -1
      if (bq === null) return 1
      return bq - aq
    })
}

function rowClass(d: Device): string {
  if (d.integrity === 'broken') return 'row-broken'
  if (d.liveness === 'never_seen' || d.liveness === 'silent') return 'row-silent'
  if (d.liveness === 'late') return 'row-stale'
  return ''
}

const KEY_TAG: Record<Device['key_state'], string> = {
  verified: 'ok',
  unchecked: 'unknown',
  rotating: 'info',
  rotation_failed: 'bad',
}

const KEY_WORDS: Record<Device['key_state'], string> = {
  verified: 'signatures checked against the device key',
  unchecked: 'no public key on file — unevaluated, not failed',
  rotating: 'promotion pending',
  rotation_failed: 'staged key never promoted',
}

const INTEGRITY_DOT: Record<Device['integrity'], string> = {
  intact: 'dot ok',
  broken: 'dot bad',
  unverified: 'dot unknown',
}

function seqTail(deviceId: string): string {
  return `…${deviceId.slice(-7)}`
}

export function DeviceRegister({
  devices,
  tenant,
  total,
}: {
  devices: readonly Device[]
  tenant: string
  /** Enrolled denominator the list was counted from. */
  total: number
}) {
  const href = useHref()
  const rows = orderRegister(devices)
  return (
    <>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>device_id</th>
              <th>host</th>
              <th>agents</th>
              <th>last seen</th>
              <th>last_seq_received</th>
              <th>key state</th>
              <th>integrity</th>
              <th aria-label="open" />
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => (
              <tr key={d.device_id} className={rowClass(d)}>
                <td>
                  <a className="ref mono" href={href.device(tenant, d.device_id)}>
                    {d.device_id}
                  </a>
                </td>
                <td>{d.host}</td>
                <td>
                  {d.agent_ids.length === 0 ? (
                    <span className="tag unknown">never declared</span>
                  ) : (
                    <div className="cellstack">
                      {d.agent_ids.slice(0, 3).map((a) => (
                        <span key={a} className="mono">
                          {a}
                        </span>
                      ))}
                      {d.agent_ids.length > 3 ? (
                        <span className="hash">+{d.agent_ids.length - 3} more</span>
                      ) : null}
                    </div>
                  )}
                </td>
                <td>
                  {d.liveness === 'never_seen' ? (
                    <>
                      <span className="never">never</span>
                      <div className="hash">enrolled {stamp(d.enrolled_at)}</div>
                    </>
                  ) : d.liveness === 'silent' ? (
                    <>
                      <span className="quiet-for">
                        silence {d.quiet_for_s === null ? 'unknown' : duration(d.quiet_for_s)}
                      </span>
                      <div className="hash">
                        last {d.last_batch_at === null ? '—' : stamp(d.last_batch_at)}
                      </div>
                    </>
                  ) : (
                    <>
                      <span className="mono">{stamp(d.last_batch_at ?? d.enrolled_at)}</span>
                      <div className="hash">
                        {d.last_batch_at === null
                          ? 'no batch stamp'
                          : `${duration((Date.now() - Date.parse(d.last_batch_at)) / 1000)} ago`}
                        {d.expected_interval_s === null
                          ? ''
                          : ` · cadence ${duration(d.expected_interval_s)}`}
                      </div>
                    </>
                  )}
                </td>
                <td>
                  {d.last_seq_received === null ? (
                    <span className="mono dim">none ever</span>
                  ) : (
                    <span className="seq-scoped">
                      <span className="dev">{seqTail(d.device_id)}</span>
                      <span className="n">{n(d.last_seq_received)}</span>
                    </span>
                  )}
                </td>
                <td>
                  <span className={`tag ${KEY_TAG[d.key_state]}`}>{d.key_state}</span>
                  <div className="hash">{KEY_WORDS[d.key_state]}</div>
                </td>
                <td>
                  <span className={INTEGRITY_DOT[d.integrity]} />{' '}
                  <span className="mono">
                    {d.integrity === 'intact'
                      ? `intact → ${d.last_seq_received === null ? '?' : n(d.last_seq_received)}`
                      : d.integrity === 'broken'
                        ? 'broken'
                        : 'unverified — not a pass'}
                  </span>
                </td>
                <td>
                  <a
                    className="go"
                    href={href.device(tenant, d.device_id)}
                    aria-label={`open ${d.device_id}`}
                  >
                    →
                  </a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="register-foot">
        <span>
          showing <b>{n(rows.length)}</b> of <b>{n(total)}</b> enrolled
        </span>
        <span className="mono dim">sorted by last seen — quietest first</span>
      </div>
    </>
  )
}
