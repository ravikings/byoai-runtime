/**
 * Class 5 — ran with no mandate snapshot at all.
 *
 * Not "allowed" and not "denied": the gate had nothing to evaluate against, so
 * no verdict was ever formed. These calls are outside the claim entirely, and
 * the caveat at the foot of the section says so in those words — a count of
 * calls that cannot be described either way is not a smaller version of a
 * denial count, it is a different quantity.
 */
import type { CoverageReport } from '@/api/schemas'
import { DeviceLink, Methodology, SectionHead, SilenceRow, leadStyle, line2Style } from './parts'
import type { Sev } from './parts'
import { duration, num } from './format'

export function UngovernedAgentsSection(props: {
  tenant: string
  agents: CoverageReport['ungoverned_agents']
}) {
  const { tenant, agents } = props
  // Worst-first by this class's own clock: how long it has been ungoverned.
  const rows = [...agents].sort((a, b) => b.quiet_for_s - a.quiet_for_s)
  const calls = rows.reduce((acc, a) => acc + a.tool_use_count, 0)

  return (
    <section className="panel">
      <SectionHead
        sev="bad"
        title="Ran with no mandate snapshot at all"
        tag={{ text: `${num(rows.length)} ${rows.length === 1 ? 'agent' : 'agents'}`, sev: 'bad' }}
        field="coverage.ungoverned_agents[]"
      />
      <p className="display" style={leadStyle}>
        Not &ldquo;allowed&rdquo; and not &ldquo;denied&rdquo;: the gate had nothing to evaluate
        against, so no verdict was ever formed.
      </p>

      {rows.length === 0 ? (
        <p className="display" style={{ margin: 0 }}>
          Every agent run carried a mandate snapshot. Every tool call has a verdict attached.
        </p>
      ) : (
        rows.map((a, i) => {
          const sev: Sev = i === 0 ? 'bad' : 'warn'
          return (
            <SilenceRow
              key={`${a.agent_id}:${a.device_id}`}
              sev={sev}
              quiet={<span className="quiet-for">{duration(a.quiet_for_s)}</span>}
              action={{
                href:
                  `/console/${encodeURIComponent(tenant)}/ledger/trajectories` +
                  `?agent=${encodeURIComponent(a.agent_id)}&device_id=${encodeURIComponent(a.device_id)}`,
                text: `${num(a.tool_use_count)} calls`,
              }}
            >
              <a
                className="mono"
                href={`/console/${encodeURIComponent(tenant)}/mandate/agents/${encodeURIComponent(a.agent_id)}`}
                style={{ color: 'var(--azure)', textDecoration: 'none' }}
              >
                {a.agent_id}
              </a>
              <span className="muted"> on </span>
              <DeviceLink tenant={tenant} deviceId={a.device_id} />
              <div style={line2Style}>
                <span className="hash">
                  {num(a.tool_use_count)} tool_use · {num(a.mandate_verdict_count)} mandate_verdict
                </span>
                <span className={`tag ${sev}`}>{a.reason}</span>
              </div>
            </SilenceRow>
          )
        })
      )}

      {rows.length > 0 ? (
        <p className="caveat" style={{ marginTop: 'var(--s3)' }}>
          These {num(calls)} calls cannot be described as in-mandate or out-of-mandate.
          They are outside the claim entirely.
        </p>
      ) : null}

      <Methodology>
        A verdict requires a snapshot to evaluate against. Where
        <span className="mono"> mandate_version_id</span> is null there was nothing to compare the
        call to, so the absence of a denial here is not evidence of compliance.
      </Methodology>
    </section>
  )
}
