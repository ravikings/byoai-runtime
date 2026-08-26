import type { Finding } from '@/api/schemas'
import type { Inclusion } from '@/api/schemas'
import { PanelEmpty } from './PanelState'
import { href, n } from './format'

interface RefLink {
  label: string
  to: string
}

/**
 * A finding's ref is one of three shapes and none of them is addressable
 * without its device. Narrowed with `in`, never with a cast.
 */
function refLink(tenant: string, finding: Finding): RefLink | null {
  const ref = finding.ref
  if (ref === null) return null
  if ('seq' in ref) {
    return {
      label: `seq ${n(ref.seq)} →`,
      to: href.entry(tenant, ref.device_id, ref.seq),
    }
  }
  if ('seq_start' in ref) {
    return {
      label: `seq ${n(ref.seq_start)}–${n(ref.seq_end)} →`,
      to: href.entry(tenant, ref.device_id, ref.seq_start),
    }
  }
  return {
    label: `session ${ref.session_id} →`,
    to: href.session(tenant, ref.device_id, ref.session_id),
  }
}

function dotClass(severity: Finding['severity']): string {
  return severity === 'bad' ? 'dot bad' : severity === 'warn' ? 'dot warn' : 'dot unknown'
}

interface Props {
  findings: readonly Finding[]
  total: number
  inclusion: Inclusion
  tenant: string
}

export function FindingsPanel({ findings, total, inclusion, tenant }: Props) {
  return (
    <section className="panel flush">
      <div className="panel-head" style={{ padding: 'var(--s4) var(--s4) var(--s3)' }}>
        <h3 className="label">Open findings</h3>
        <span className="mono dim">
          {inclusion.devices_included} / {inclusion.devices_enrolled} devices
        </span>
        <div style={{ flex: 1 }} />
        <a className="link-count" href={href.findings(tenant)}>
          all {n(total)} →
        </a>
      </div>

      <div style={{ padding: '0 var(--s4) var(--s2)' }}>
        {findings.length === 0 ? (
          <PanelEmpty headline="No open finding on any reporting device.">
            <p className="mono muted" style={{ lineHeight: 1.45 }}>
              Verify raised nothing across the {inclusion.devices_included} reporting devices. The
              other {Math.max(0, inclusion.devices_enrolled - inclusion.devices_included)} shipped
              nothing to check — this is not a clean bill of health for the fleet.
            </p>
          </PanelEmpty>
        ) : (
          findings.map((f) => {
            const link = refLink(tenant, f)
            return (
              <div key={f.id} className={`finding ${f.severity}`}>
                <span className={dotClass(f.severity)} style={{ marginTop: '.35rem' }} />
                <div className="what">
                  <div>
                    <b>{f.kind}</b> — {f.what}
                  </div>
                  <div className="where">
                    <span className="mono dim">device</span>
                    <a className="ref" href={href.device(tenant, f.device_id)}>
                      {f.device_id}
                    </a>
                    {link === null ? (
                      <span className="mono dim">· no seq recorded for this finding</span>
                    ) : (
                      <>
                        <span className="mono dim">·</span>
                        <a className="ref" href={link.to}>
                          {link.label}
                        </a>
                      </>
                    )}
                  </div>
                </div>
              </div>
            )
          })
        )}
      </div>
    </section>
  )
}
