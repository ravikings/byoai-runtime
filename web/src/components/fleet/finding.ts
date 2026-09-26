import type { Finding } from '@/api/schemas'
import type { HrefMap } from '@/app/hrefContext'
import { n } from './format'

/**
 * The finding presentation primitives, shared by the overview panel and the
 * findings page so the two can never drift in wording, dot severity, or —
 * the rule that matters most — in how a ref is addressed: one of three
 * shapes, none renderable without its device_id.
 */
export interface RefLink {
  label: string
  to: string
}

export function refLink(tenant: string, finding: Finding, href: HrefMap): RefLink | null {
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

export function dotClass(severity: Finding['severity']): string {
  return severity === 'bad' ? 'dot bad' : severity === 'warn' ? 'dot warn' : 'dot unknown'
}
