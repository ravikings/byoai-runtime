/**
 * Class 4 — sessions with no checkpoint, and checkpoints never counter-signed.
 *
 * A checkpoint the far side never counter-signed proves nothing to anyone who
 * does not already trust us. The two watermarks — entries and checkpoints —
 * advance independently, so "fully synced" and "nothing counter-signed" are a
 * perfectly ordinary pair of facts and must be read separately.
 */
import type { CoverageReport } from '@/api/schemas'
import { DeviceLink, Methodology, SectionHead, SilenceRow, leadStyle, line2Style } from './parts'
import type { Sev } from './parts'
import { duration, num } from './format'

export function CheckpointGapsSection(props: {
  tenant: string
  gaps: CoverageReport['checkpoint_gaps']
}) {
  const { tenant, gaps } = props
  const rows = [...gaps.detail].sort((a, b) => b.quiet_for_s - a.quiet_for_s)
  const total = gaps.sessions_without_checkpoint + gaps.checkpoints_never_countersigned

  return (
    <section className="panel">
      <SectionHead
        sev="warn"
        title="No checkpoint, or no counter-signature"
        tag={{ text: `${num(total)} affected`, sev: 'warn' }}
        field="coverage.checkpoint_gaps[]"
      />
      <p className="display" style={leadStyle}>
        A checkpoint the far side never counter-signed proves nothing to anyone who does not
        already trust us.
      </p>

      <div className="row" style={{ gap: 'var(--s3)', marginBottom: 'var(--s3)', flexWrap: 'wrap' }}>
        <a className="link-count" href={`/console/${encodeURIComponent(tenant)}/evidence/checkpoints?missing=true`}>
          {num(gaps.sessions_without_checkpoint)} sessions with zero checkpoint rows
        </a>
        <a className="link-count" href={`/console/${encodeURIComponent(tenant)}/evidence/checkpoints?countersigned=false`}>
          {num(gaps.checkpoints_never_countersigned)} checkpoints never counter-signed
        </a>
      </div>

      {rows.length === 0 ? (
        // The all-clear may only be claimed when the COUNTS are zero, not when
        // the detail list happens to be empty. A truncated or paginated detail
        // array alongside a non-zero count would otherwise print "every session
        // carries a checkpoint" directly beneath a badge saying 42 are missing.
        gaps.sessions_without_checkpoint + gaps.checkpoints_never_countersigned === 0 ? (
          <p className="display" style={{ margin: 0 }}>
            Every session carries a checkpoint and every checkpoint has been counter-signed.
          </p>
        ) : (
          <p className="caveat" style={{ margin: 0 }}>
            {num(gaps.sessions_without_checkpoint + gaps.checkpoints_never_countersigned)} affected,
            but the API returned no detail rows for them — this is a gap in the report, not an
            all-clear.
          </p>
        )
      ) : (
        rows.map((g, i) => {
          // The first row is the longest-standing gap in this class; give it
          // the harder colour rather than letting age read as calm.
          const sev: Sev = i === 0 ? 'bad' : 'warn'
          return (
            <SilenceRow
              key={`${g.device_id}:${g.what}:${g.quiet_for_s}`}
              sev={sev}
              quiet={<span className="quiet-for">{duration(g.quiet_for_s)}</span>}
              action={{
                href: `/console/${encodeURIComponent(tenant)}/evidence/checkpoints?device_id=${encodeURIComponent(g.device_id)}`,
                text: `list ${num(g.count)}`,
              }}
            >
              <b>{g.what}</b>
              <div style={line2Style}>
                <DeviceLink tenant={tenant} deviceId={g.device_id} />
                <span className={`tag ${sev}`}>{num(g.count)} affected</span>
              </div>
            </SilenceRow>
          )
        })
      )}

      <Methodology>
        Checkpoints ship on their own watermark and their own endpoint
        (<span className="mono">/v1/checkpoints/batch</span>). Entries can be fully synced while
        every checkpoint for the same seqs is still pending — the two watermarks are independent
        and must be read separately.
      </Methodology>
    </section>
  )
}
