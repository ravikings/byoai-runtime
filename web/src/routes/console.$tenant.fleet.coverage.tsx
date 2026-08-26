/**
 * Coverage — the silence report (spec §6.0.2, journey J4).
 *
 * The screen no competitor has: a list of things that did NOT happen. The
 * governing constraint is that absence must be as loud as failure. An empty
 * page here is the good outcome, which is exactly why the visual language must
 * not reward silence by making it look calm — every empty class says so in
 * words, every failure state says the page failed rather than showing nothing.
 *
 * Ordering: the five classes are never interleaved. 85 days of unverified seqs
 * and 6 hours of device silence are different units of harm; ranking them
 * against each other would be a presentation, not a fact. Within each class,
 * rows sort worst-first by THAT class's own clock.
 */
import { createFileRoute } from '@tanstack/react-router'
import { useCoverage } from '@/api/queries'
import { href } from '@/components/fleet/format'
import { toScope } from '@/app/scope'
import { ScopeLine } from '@/components/ScopeLine'
import { BlindArea } from '@/components/coverage/BlindArea'
import { BlindSpotPanel } from '@/components/coverage/BlindSpotPanel'
import { CheckpointGapsSection } from '@/components/coverage/CheckpointGapsSection'
import { NeverSeenSection } from '@/components/coverage/NeverSeenSection'
import { SilentSection } from '@/components/coverage/SilentSection'
import { UngovernedAgentsSection } from '@/components/coverage/UngovernedAgentsSection'
import { UnverifiedRangesSection } from '@/components/coverage/UnverifiedRangesSection'
import { CoverageEmpty, CoverageError, CoverageLoading } from '@/components/coverage/states'
import { num, ts } from '@/components/coverage/format'

export const Route = createFileRoute('/console/$tenant/fleet/coverage')({
  component: CoveragePage,
})

function CoveragePage() {
  const { tenant } = Route.useParams()
  const search = Route.useSearch()
  const query = useCoverage(toScope(tenant, search))
  const r = query.data

  return (
    <>
      <div className="row" style={{ marginBottom: 'var(--s3)' }}>
        <h1>Coverage — the silence report</h1>
        <span className="tag bad">5 classes of absence</span>
        <span className="spacer" style={{ flex: 1 }} />
        <a className="btn sm" href={href.devices(tenant)}>
          Devices register →
        </a>
      </div>

      <ScopeLine
        tenant={tenant}
        search={search}
        extra={[
          // "not yet loaded" over a query that has ERRORED describes a state
          // the page is not in, on the one screen whose premise is that
          // absence must be reported honestly. Say which of the two it is.
          r !== undefined
            ? `enrolled ${num(r.enrolled)}`
            : query.isError
              ? 'enrolled unknown — the request failed'
              : 'enrolled unknown — not yet loaded',
          r !== undefined ? `as of ${ts(r.as_of)}` : query.isError ? 'as of — (failed)' : 'as of —',
          'GET /v1/console/fleet/coverage',
        ]}
      />

      <div className="banner bad" style={{ marginTop: 'var(--s4)' }}>
        <span className="dot bad pulse" />
        <span>
          <b>This page is a list of things that did not happen.</b> An empty page here would be
          the good outcome. Everything below is evidence that should exist and does not — sorted
          worst-first within each class, unbounded silence above merely-late silence.
        </span>
      </div>

      <CoverageBody query={query} tenant={tenant} />
    </>
  )
}

function CoverageBody(props: {
  tenant: string
  query: ReturnType<typeof useCoverage>
}) {
  const { tenant, query } = props

  if (query.isError) {
    return <CoverageError error={query.error} />
  }
  const r = query.data
  if (r === undefined) {
    return <CoverageLoading />
  }
  const nothingMissing =
    r.never_seen.length === 0 &&
    r.silent.length === 0 &&
    r.unverified_ranges.length === 0 &&
    r.ungoverned_agents.length === 0 &&
    r.checkpoint_gaps.sessions_without_checkpoint === 0 &&
    r.checkpoint_gaps.checkpoints_never_countersigned === 0

  return (
    <>
      {nothingMissing ? <CoverageEmpty enrolled={r.enrolled} /> : null}

      <BlindArea report={r} />

      {/* Worst-first, and never interleaved: unbounded silence, then late
          silence, then unverified storage, then unsigned evidence, then
          unevaluated action. */}
      <NeverSeenSection tenant={tenant} devices={r.never_seen} />
      <SilentSection tenant={tenant} devices={r.silent} />
      <UnverifiedRangesSection tenant={tenant} ranges={r.unverified_ranges} />

      <div className="col2" style={{ marginTop: 'var(--s5)' }}>
        <CheckpointGapsSection tenant={tenant} gaps={r.checkpoint_gaps} />
        <UngovernedAgentsSection tenant={tenant} agents={r.ungoverned_agents} />
      </div>

      <BlindSpotPanel tenant={tenant} blindSpot={r.blind_spot} enrolled={r.enrolled} />

      <p className="methodology" style={{ marginTop: 'var(--s5)' }}>
        Ordering: unbounded silence first, then each class worst-first by its own clock. Classes
        are not interleaved — {num(r.unverified_ranges.length)} unverified ranges and{' '}
        {num(r.silent.length)} quiet devices are different units of harm, and ranking them against
        each other would be a presentation, not a fact.
      </p>
    </>
  )
}
