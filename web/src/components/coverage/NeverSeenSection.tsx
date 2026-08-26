/**
 * Class 1 — enrolled but never seen.
 *
 * These devices have no `last_batch_at` to subtract from, so there is no
 * elapsed time to print. The `.never` badge fills the "quiet for" slot instead
 * of an em-dash: an em-dash would make the most alarming category the quietest
 * looking cell on the screen.
 */
import type { CoverageReport } from '@/api/schemas'
import { DeviceLink, Methodology, SectionHead, SilenceHead, SilenceRow, leadStyle, line2Style, secStyle } from './parts'
import { num, ts } from './format'

export function NeverSeenSection(props: { tenant: string; devices: CoverageReport['never_seen'] }) {
  const { tenant, devices } = props

  // Worst-first by this class's own clock: the longer a device has been
  // enrolled without ever reporting, the longer we have been wrong about it.
  const rows = [...devices].sort((a, b) => a.enrolled_at.localeCompare(b.enrolled_at))

  return (
    <section className="panel" style={secStyle}>
      <SectionHead
        sev="bad"
        title="Enrolled but never seen"
        tag={{ text: `${num(rows.length)} ${rows.length === 1 ? 'device' : 'devices'}`, sev: 'bad' }}
        field="coverage.never_seen[]"
      />
      <p className="display" style={leadStyle}>
        A key was issued, a device_id exists, and not one signed batch has ever arrived.
        Whatever these {rows.length === 1 ? 'ran' : `${num(rows.length)} ran`}, we hold none of it.
      </p>

      {rows.length === 0 ? (
        <p className="display" style={{ margin: 0 }}>
          Every enrolled device has reported at least once. This class is empty — the only
          class on this page where empty is simply good.
        </p>
      ) : (
        <>
          <SilenceHead subject="device / enrolment" />
          {rows.map((d) => (
            <SilenceRow
              key={d.device_id}
              sev="bad"
              quiet={<span className="never">never seen</span>}
              action={{ href: `/console/${encodeURIComponent(tenant)}/fleet/devices/${encodeURIComponent(d.device_id)}`, text: 'enrolment record' }}
            >
              <DeviceLink tenant={tenant} deviceId={d.device_id} />
              <span className="muted"> · {d.host}</span>
              <div style={line2Style}>
                <span className="hash">enrolled {ts(d.enrolled_at)}</span>
                <span className="hash">key_state {d.key_state}</span>
                <span className="tag bad">batches_received {num(d.batches_received)}</span>
                <span className="tag warn">
                  last_seq_received {d.last_seq_received === null ? 'none ever' : num(d.last_seq_received)}
                </span>
                {d.agent_ids.length > 0 ? (
                  <span className="hash">declared agents {d.agent_ids.join(', ')}</span>
                ) : (
                  <span className="tag unknown">no agents ever declared</span>
                )}
              </div>
            </SilenceRow>
          ))}
        </>
      )}

      <Methodology>
        Enrolment (<span className="mono">enroll.py</span>) registers a key; it does not prove the
        recorder ever started. These rows are the difference between the two. There is no elapsed
        silence to quote because there is no first batch to measure from — the badge, not a blank
        cell, is the honest rendering.
      </Methodology>
    </section>
  )
}
