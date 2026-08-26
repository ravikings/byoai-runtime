import type { ReactNode } from 'react'
import { SchemaMismatchError } from '@/api/client'

/** A panel-shaped placeholder. Never a number, never a tick — nothing is known
 *  yet, and this says so rather than showing a zero. */
export function PanelLoading({ title, rows = 3 }: { title: string; rows?: number }) {
  return (
    <section className="panel" aria-busy="true">
      <div className="panel-head">
        <h3 className="label">{title}</h3>
        <span className="mono dim">loading…</span>
      </div>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} style={{ marginTop: 'var(--s3)' }}>
          <span className="skel" style={{ width: `${70 - i * 12}%` }} />
        </div>
      ))}
    </section>
  )
}

/**
 * The state this product exists to get right: a response that did not match
 * the contract is *not* an empty screen and *not* a pass. It is loudly
 * unknown, with the field that failed named.
 */
export function PanelError({ title, error }: { title: string; error: unknown }) {
  const mismatch = error instanceof SchemaMismatchError
  const detail =
    error instanceof Error ? error.message : typeof error === 'string' ? error : 'unknown cause'
  return (
    <section className="panel">
      <div className="panel-head">
        <h3 className="label">{title}</h3>
        <span className={mismatch ? 'tag unknown' : 'tag bad'}>
          {mismatch ? 'unexpected response' : 'request failed'}
        </span>
      </div>
      <div className={mismatch ? 'banner warn' : 'banner bad'}>
        <span className={mismatch ? 'dot unknown' : 'dot bad'} />
        <div>
          {mismatch ? (
            <>
              <b>The server sent something this console does not recognise.</b> Nothing about{' '}
              {title.toLowerCase()} is known — this is not a pass, and no figure below it can be
              trusted.
            </>
          ) : (
            <>
              <b>This panel could not be loaded.</b> {title} is unknown for the current scope; the
              absence of a finding here is not evidence of its absence.
            </>
          )}
        </div>
      </div>
      <p className="mono muted" style={{ margin: 'var(--s2) 0 0', lineHeight: 1.45 }}>
        {detail}
      </p>
    </section>
  )
}

/** Nothing to show, and a reason why that is or isn't good news. */
export function PanelEmpty({ headline, children }: { headline: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="headline">{headline}</div>
      {children}
    </div>
  )
}
