/**
 * Shared furniture for the silence report.
 *
 * The frame's own `<style>` block carries layout only (grids and gaps); every
 * colour, weight and state word comes from `components.css`. The same split
 * holds here — the inline styles below are geometry, nothing else. If these
 * ever graduate into `components.css` as `.strip` / `.sec-head` /
 * `.silence-head`, the class names are already the ones the frame used.
 */
import type { CSSProperties, ReactNode } from 'react'

/** The four-column silence grid, matching `.silence`'s own tracks. */
const headStyle: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: '1.1rem 1fr auto auto',
  gap: 'var(--s3)',
  paddingBottom: 'var(--s2)',
  borderBottom: 'var(--hairline)',
}
const labelStyle: CSSProperties = { margin: 0 }
const rightLabelStyle: CSSProperties = { margin: 0, textAlign: 'right' }

export const secStyle: CSSProperties = { marginTop: 'var(--s5)' }

export const secHeadStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'baseline',
  gap: 'var(--s3)',
  marginBottom: 'var(--s3)',
}

export const bodyStyle: CSSProperties = { minWidth: 0 }

export const line2Style: CSSProperties = {
  marginTop: 'var(--s1)',
  display: 'flex',
  gap: 'var(--s3)',
  flexWrap: 'wrap',
  alignItems: 'baseline',
}

export const leadStyle: CSSProperties = { margin: '0 0 var(--s3)' }

/** State severity. Three values, never a boolean — `unknown` is a real state. */
export type Sev = 'bad' | 'warn' | 'unknown'

/**
 * A section header: dot, title, count tag, and the API field the section is
 * rendered from. The field name is not decoration — it is how an operator
 * checks the screen against the payload.
 */
export function SectionHead(props: {
  sev: Sev
  title: string
  tag?: { text: string; sev: Sev } | undefined
  field: string
}) {
  return (
    <div style={secHeadStyle}>
      <span className={`dot ${props.sev}`} />
      <h2 style={{ margin: 0 }}>{props.title}</h2>
      {props.tag ? <span className={`tag ${props.tag.sev}`}>{props.tag.text}</span> : null}
      <span className="spacer" style={{ flex: 1 }} />
      <span className="hash">{props.field}</span>
    </div>
  )
}

/** Column headings for a run of `.silence` rows. */
export function SilenceHead(props: { subject: string }) {
  return (
    <div style={headStyle}>
      <span />
      <span className="label" style={labelStyle}>{props.subject}</span>
      <span className="label" style={rightLabelStyle}>quiet for</span>
      <span className="label" style={rightLabelStyle}>next step</span>
    </div>
  )
}

/**
 * One row of absence. The `quiet` slot is always filled: a row whose silence
 * is unbounded gets the `.never` badge rather than an empty cell, so the most
 * alarming category is not also the quietest-looking one.
 */
export function SilenceRow(props: {
  sev: Sev
  children: ReactNode
  quiet: ReactNode
  action: { href: string; text: string }
}) {
  return (
    <div className={`silence ${props.sev}`}>
      <span className={`dot ${props.sev}`} />
      <div className="body" style={bodyStyle}>{props.children}</div>
      {props.quiet}
      <a className="link-count" href={props.action.href}>{props.action.text}</a>
    </div>
  )
}

/** A device_id that is always a link to the device it names. */
export function DeviceLink(props: { tenant: string; deviceId: string }) {
  return (
    <a
      className="mono"
      // Encoded like every sibling link to this route. A device_id carrying
      // '/', '?' or '#' — plausible for host-derived ids — otherwise produces a
      // different route here than the identical id does elsewhere.
      href={`/console/${encodeURIComponent(props.tenant)}/fleet/devices/${encodeURIComponent(
        props.deviceId,
      )}`}
      style={{ color: 'var(--azure)', textDecoration: 'none' }}
    >
      {props.deviceId}
    </a>
  )
}

/** The methodology note that closes each section. How, not just what. */
export function Methodology(props: { children: ReactNode }) {
  return <p className="methodology">{props.children}</p>
}
