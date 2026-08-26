import { useHref } from '@/app/hrefContext'
/**
 * The limit of the screen, in the product's own words.
 *
 * `statement` and `defensible_claim` come from the API rather than from this
 * component, because the boundary of the claim is a property of the data, not
 * of the page rendering it. The claim is never "complete" — it is "complete
 * across N enrolled devices, with M unaccounted for". A product naming the
 * limit of its own claim is the most honest thing on this screen, so it gets
 * the largest type on it.
 */
import type { CoverageReport } from '@/api/schemas'
import { secStyle } from './parts'
import { num } from './format'

export function BlindSpotPanel(props: {
  tenant: string
  blindSpot: CoverageReport['blind_spot']
  enrolled: number
}) {
  const href = useHref()
  const { tenant, blindSpot, enrolled } = props
  return (
    <section className="blindspot" style={secStyle}>
      <div className="label" style={{ color: 'var(--off-scope)' }}>The limit of this screen</div>
      <p
        className="display"
        style={{
          fontSize: 'var(--t-verdict)',
          lineHeight: 1.35,
          color: 'var(--ink)',
          margin: '0 0 var(--s3)',
        }}
      >
        {blindSpot.statement}
      </p>
      <p style={{ maxWidth: '62ch', margin: '0 0 var(--s3)' }}>
        Every count above is computed against <span className="mono">{blindSpot.basis}</span> —{' '}
        {num(enrolled)} rows. An agent running on a laptop that never called{' '}
        <span className="mono">byoai enroll</span>, a container image built without the recorder,
        a fork of the SDK with shipping disabled: none of them appear here, in the fraction in the
        scope chip, or in any number this product will ever show you. They are not counted as
        silent. They are not counted at all.
      </p>
      <p style={{ maxWidth: '62ch', margin: '0 0 var(--s4)' }}>
        So the defensible statement this page supports is <b>&ldquo;{blindSpot.defensible_claim}&rdquo;</b>{' '}
        — never &ldquo;complete&rdquo;. The boundary of the claim is enrolment, and enrolment is an
        act someone has to perform. Closing that gap is a fleet-management problem, not an
        observability one, and no amount of ledger integrity substitutes for it.
      </p>
      <div className="row">
        <a className="btn sm" href={href.enrollment(tenant)}>
          Enrolment policy &amp; MDM reconciliation
        </a>
        <a className="btn sm" href={`/console/${encodeURIComponent(tenant)}/fleet/devices`}>
          Compare {num(enrolled)} enrolled against your asset inventory
        </a>
        <span className="hash" style={{ marginLeft: 'var(--s3)' }}>
          coverage.blind_spot.basis = &quot;{blindSpot.basis}&quot;
        </span>
      </div>
    </section>
  )
}
