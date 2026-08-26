/**
 * Class 2 — last seen N ago, against expected cadence.
 *
 * Ranked by overdue multiple, not raw elapsed time: 76× on a five-minute
 * shipper is an incident, 1.3× on a nightly batch job is a rounding error, and
 * six hours is both of those depending only on the cadence.
 *
 * When `expected_interval_s` is null there are too few batches to infer a
 * cadence. The multiple is then SUPPRESSED rather than guessed, and the row is
 * ranked above every known multiple — an unknown cadence must never render as
 * an on-time one, and it cannot honestly be ordered against one either.
 */
import type { CoverageReport, Device } from '@/api/schemas'
import { DeviceLink, Methodology, SectionHead, SilenceHead, SilenceRow, leadStyle, line2Style, secStyle } from './parts'
import type { Sev } from './parts'
import { cadence, duePercent, duration, multiple, num, ts } from './format'

/**
 * `silent` is worse than `late`; an uninferable cadence is worse than both.
 *
 * The null check must cover BOTH fields the row's own text keys off. With
 * `expected_interval_s` set but `overdue_multiple` null — which the schema
 * permits independently — colouring the row bad while its only visible text
 * reads "cadence not inferable" tells an operator triaging by colour something
 * the row itself denies.
 */
function unknownCadence(d: Device): boolean {
  return d.expected_interval_s === null || d.overdue_multiple === null
}

function severity(d: Device): Sev {
  if (unknownCadence(d)) return 'unknown'
  return d.liveness === 'silent' ? 'bad' : 'warn'
}

function CadenceBar(props: { device: Device }) {
  const d = props.device
  if (d.expected_interval_s === null || d.overdue_multiple === null) return null
  const sev = severity(d)
  return (
    <div className={`cadence ${sev}`}>
      <span className="mult">{multiple(d.overdue_multiple)}</span>
      <div className="track">
        <div className="fill" style={{ width: '100%' }} />
        <div className="due" style={{ left: `${duePercent(d.overdue_multiple)}%` }} />
      </div>
      <div className="terms">
        <span>expected {cadence(d.expected_interval_s)} (median inter-batch)</span>
        <span>quiet {d.quiet_for_s === null ? 'unknown' : duration(d.quiet_for_s)}</span>
      </div>
    </div>
  )
}

export function SilentSection(props: { tenant: string; devices: CoverageReport['silent'] }) {
  const { tenant, devices } = props

  const rows = [...devices].sort((a, b) => {
    // Sort by the SAME predicate that decides the row's tag and colour. Testing
    // only overdue_multiple here let a device with expected_interval_s null but
    // a multiple present render "cadence not inferable" while sorting among the
    // known multiples — the list contradicting its own methodology note that a
    // quantity we cannot compute cannot be ranked below one we can.
    const aUnknown = unknownCadence(a)
    const bUnknown = unknownCadence(b)
    if (aUnknown !== bUnknown) return aUnknown ? -1 : 1
    const am = aUnknown ? null : a.overdue_multiple
    const bm = bUnknown ? null : b.overdue_multiple
    // Rows with no inferable cadence sort first — unbounded, not zero.
    if (am === null && bm === null) {
      // A null quiet_for_s is an UNBOUNDED silence (no last batch to measure
      // from), not a zero-length one. Coalescing it to 0 sorted the worst row
      // in this tier to the bottom — an absent value must never come out
      // looking like a small one.
      if (a.quiet_for_s === null && b.quiet_for_s === null) return 0
      if (a.quiet_for_s === null) return -1
      if (b.quiet_for_s === null) return 1
      return b.quiet_for_s - a.quiet_for_s
    }
    if (am === null) return -1
    if (bm === null) return 1
    return bm - am
  })

  return (
    <section className="panel" style={secStyle}>
      <SectionHead
        sev="warn"
        title="Last seen N ago, against expected cadence"
        tag={{ text: `${num(rows.length)} ${rows.length === 1 ? 'device' : 'devices'}`, sev: 'warn' }}
        field="coverage.silent[]"
      />
      <p className="display" style={leadStyle}>
        Six hours quiet means one thing on a five-minute shipper and another on a nightly one.
        The multiple, not the clock, is the severity.
      </p>

      {rows.length === 0 ? (
        <p className="display" style={{ margin: 0 }}>
          Every device that has ever reported is inside its observed cadence.
        </p>
      ) : (
        <>
          <SilenceHead subject="device / cadence vs silence" />
          {rows.map((d) => {
            const sev = severity(d)
            const cadenceUnknown = unknownCadence(d)
            return (
              <SilenceRow
                key={d.device_id}
                sev={sev}
                quiet={
                  d.quiet_for_s === null
                    ? <span className="never">no last batch</span>
                    : <span className="quiet-for">{duration(d.quiet_for_s)}</span>
                }
                action={{ href: `/console/${encodeURIComponent(tenant)}/fleet/devices/${d.device_id}`, text: 'device detail' }}
              >
                <DeviceLink tenant={tenant} deviceId={d.device_id} />
                <span className="muted"> · {d.host}</span>
                <div style={line2Style}>
                  <span className="hash">
                    last_batch_at {d.last_batch_at === null ? 'null' : ts(d.last_batch_at)}
                  </span>
                  <span className="hash">
                    last_seq_received {d.last_seq_received === null ? 'none' : num(d.last_seq_received)}
                  </span>
                  {cadenceUnknown ? (
                    <span className="tag unknown">
                      cadence not inferable — too few batches, multiple suppressed
                    </span>
                  ) : (
                    <span className={`tag ${sev === 'bad' ? 'bad' : 'warn'}`}>
                      {multiple(d.overdue_multiple ?? 0)} overdue
                    </span>
                  )}
                </div>
                {cadenceUnknown ? (
                  <div style={line2Style}>
                    <span className="hash">expected_interval_s null</span>
                    <span className="muted">
                      — we cannot say whether this device is late, only that it is quiet.
                    </span>
                  </div>
                ) : (
                  <CadenceBar device={d} />
                )}
              </SilenceRow>
            )
          })}
        </>
      )}

      <Methodology>
        Cadence is observed, not declared: the median inter-batch interval over the device&rsquo;s
        recent batches. A device with too few batches has{' '}
        <span className="mono">expected_interval_s: null</span> and is listed with the multiple
        suppressed rather than guessed — an unknown cadence must not be rendered as an on-time one.
        Those rows sort above every known multiple, because a quantity we cannot compute cannot be
        ranked below one we can.
      </Methodology>
    </section>
  )
}
