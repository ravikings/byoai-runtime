import type { Device, FleetSummary } from '@/api/schemas'
import { Provenance } from './Provenance'
import { PanelEmpty } from './PanelState'
import { duration, href, n } from './format'

/** Silent first, longest quiet first; never-seen devices last because their
 *  quiet time is not a number — nothing has ever arrived to measure from. */
function orderSilent(devices: readonly Device[]): Device[] {
  return devices
    .filter((d) => d.liveness === 'silent' || d.liveness === 'never_seen')
    .slice()
    .sort((a, b) => {
      if (a.liveness !== b.liveness) return a.liveness === 'never_seen' ? 1 : -1
      // An unknown quiet duration is unbounded, not zero. Coalescing it to 0
      // sorted a device whose silence cannot be measured below one quiet for
      // five minutes — an absent value must never rank as a small one.
      if (a.quiet_for_s === null && b.quiet_for_s === null) return 0
      if (a.quiet_for_s === null) return -1
      if (b.quiet_for_s === null) return 1
      return b.quiet_for_s - a.quiet_for_s
    })
}

interface Props {
  summary: FleetSummary
  tenant: string
  /** Undefined while the device list is still loading or failed — the panel
   *  must say which, never render an empty list as "all quiet accounted for". */
  devices: readonly Device[] | undefined
  devicesProblem: 'loading' | 'failed' | null
}

export function CoveragePanel({ summary, tenant, devices, devicesProblem }: Props) {
  const { coverage, inclusion } = summary
  const missing = coverage.enrolled - coverage.reporting
  const partial = missing > 0
  const silent = devices === undefined ? [] : orderSilent(devices)

  return (
    <section className="panel">
      <div className="panel-head">
        <h3 className="label">Coverage</h3>
        <span className={partial ? 'tag warn' : 'tag ok'}>{partial ? 'partial' : 'complete'}</span>
      </div>

      <div className="stat">
        <div className="fraction">
          <span className="num">{n(coverage.reporting)}</span>
          <span className="of">of {n(coverage.enrolled)} devices reporting</span>
        </div>
        <Provenance
          inclusion={inclusion}
          note={
            missing > 0
              ? `${n(missing)} enrolled device${missing === 1 ? '' : 's'} shipped nothing`
              : 'every enrolled device shipped in this window'
          }
        />
      </div>

      <p className="display" style={{ lineHeight: 1.4, margin: 'var(--s2) 0 0' }}>
        {missing > 0 ? (
          <>
            {missing === 1 ? 'One device has' : `${n(missing)} devices have`} gone quiet; every number
            here is computed over the other {n(coverage.reporting)}.
          </>
        ) : (
          <>All {n(coverage.enrolled)} enrolled devices reported in this window.</>
        )}
      </p>

      <h3 className="label" style={{ marginTop: 'var(--s3)' }}>
        Silent devices{' '}
        {/* Count the rows actually rendered, but ONLY when there are rows to
            count. With the device query down the list is empty for lack of
            data, not for lack of silence — printing "0" there asserted the
            fleet was quiet-free directly above a banner naming the devices it
            could not list. Unknown is the honest reading. */}
        {devicesProblem === null ? (
          <>
            <a className="link-count" href={href.coverage(tenant)}>
              {n(silent.length)} →
            </a>
            {silent.length !== missing ? (
              <span className="tag warn" style={{ marginLeft: 'var(--s2)' }}>
                summary says {n(missing)} — the two queries disagree
              </span>
            ) : null}
          </>
        ) : (
          <span className="tag unknown">
            unknown — the register did not load; the summary counts {n(missing)}
          </span>
        )}
      </h3>

      {devicesProblem === 'loading' ? (
        <div className="silence">
          <span className="dot unknown" />
          <div className="mono muted">loading the device register…</div>
        </div>
      ) : devicesProblem === 'failed' ? (
        <div className="silence warn">
          <span className="dot unknown" />
          <div>
            The device register could not be read. <b>{n(missing)}</b> device
            {missing === 1 ? ' is' : 's are'} unaccounted for and this list cannot name{' '}
            {missing === 1 ? 'it' : 'them'}.
          </div>
          <a className="btn sm ghost" href={href.coverage(tenant)}>
            →
          </a>
        </div>
      ) : silent.length === 0 ? (
        <PanelEmpty headline="Nothing has gone quiet in this window.">
          <p className="mono muted" style={{ lineHeight: 1.45 }}>
            Every enrolled device shipped at least one batch. Silence is what this screen watches
            for; there is none to report.
          </p>
        </PanelEmpty>
      ) : (
        silent.map((d) => {
          const never = d.liveness === 'never_seen'
          return (
            <div key={d.device_id} className={never ? 'silence warn' : 'silence bad'}>
              <span className={never ? 'dot unknown' : 'dot bad'} />
              <div>
                <a className="ref mono" href={href.device(tenant, d.device_id)}>
                  {d.device_id}
                </a>
                {never ? (
                  <span className="mono dim">
                    {' '}
                    · <b>never seen</b>, no batch has ever arrived
                  </span>
                ) : (
                  <span className="mono dim">
                    {' '}
                    · last_seq{' '}
                    {d.last_seq_received === null ? 'unknown' : n(d.last_seq_received)}
                  </span>
                )}
              </div>
              <span className="quiet-for">
                {never ? 'never' : d.quiet_for_s === null ? 'unknown' : `quiet ${duration(d.quiet_for_s)}`}
              </span>
              <a className="btn sm ghost" href={href.device(tenant, d.device_id)} aria-label={`open ${d.device_id}`}>
                →
              </a>
            </div>
          )
        })
      )}
    </section>
  )
}
