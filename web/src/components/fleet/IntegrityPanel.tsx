import type { FleetSummary } from '@/api/schemas'
import { Provenance } from './Provenance'
import { href, n } from './format'

/**
 * Three segments, never two. `unverified` is not a shade of intact — a verify
 * job that never ran proves nothing, and folding it into the green would be
 * the single worst bug this screen could ship.
 */
export function IntegrityPanel({ summary, tenant }: { summary: FleetSummary; tenant: string }) {
  const { integrity, coverage, inclusion } = summary
  const summed = integrity.intact + integrity.broken + integrity.unverified
  const reporting = coverage.reporting
  const noVerdict = integrity.no_verdict
  /** Reporting devices no verify walk has covered — unverified minus the ones
   *  that are unverified only because they never reported at all. */
  const unwalkedReporting = Math.max(0, integrity.unverified - noVerdict)
  const total = summed === 0 ? 1 : summed

  return (
    <section className="panel">
      <div className="panel-head">
        <h3 className="label">Integrity</h3>
        <span className="mono dim">per-device verdicts, summed</span>
      </div>

      <div className="stat">
        <div className="fraction">
          <span className="num">{n(integrity.intact)}</span>
          <span className="of">of {n(reporting)} reporting devices verify intact</span>
        </div>
        <Provenance
          inclusion={inclusion}
          note={
            noVerdict > 0
              ? `the ${n(noVerdict)} silent device${noVerdict === 1 ? ' has' : 's have'} no verdict at all`
              : 'every reporting device has a verdict'
          }
        />
      </div>

      <div
        className="rollup"
        style={{ marginTop: 'var(--s3)' }}
        role="img"
        aria-label={`${integrity.intact} intact, ${integrity.broken} broken, ${integrity.unverified} unverified`}
      >
        <span className="seg ok" style={{ flex: integrity.intact / total }} />
        <span className="seg bad" style={{ flex: integrity.broken / total }} />
        <span className="seg unknown" style={{ flex: integrity.unverified / total }} />
      </div>
      <div className="rollup-key">
        <span>
          <span className="dot ok" /> {n(integrity.intact)} intact
        </span>
        <span>
          <span className="dot bad" /> {n(integrity.broken)} broken
        </span>
        <span>
          <span className="dot unknown" /> {n(integrity.unverified)} unverified
        </span>
      </div>

      <p className="mono muted" style={{ lineHeight: 1.45, margin: 'var(--s2) 0 0' }}>
        {summed === reporting ? (
          <>
            {n(integrity.intact)} + {n(integrity.broken)} + {n(integrity.unverified)} ={' '}
            {n(summed)}, one verdict per reporting device. <b>Unverified is not intact.</b>
          </>
        ) : (
          <>
            {n(integrity.intact)} + {n(integrity.broken)} + {n(integrity.unverified)} ={' '}
            {n(summed)}, not {n(reporting)} —{' '}
            {noVerdict > 0 ? (
              <>
                the {n(noVerdict)} silent device{noVerdict === 1 ? '' : 's'} count as{' '}
                <b>unverified</b>
                {unwalkedReporting > 0 ? (
                  <>
                    , with {n(unwalkedReporting)} reporting device
                    {unwalkedReporting === 1 ? '' : 's'} no verify job has covered
                  </>
                ) : null}
                .{' '}
              </>
            ) : (
              <>the segments and the reporting count are drawn from different denominators. </>
            )}
            <b>Unverified is not intact.</b>
          </>
        )}
      </p>

      <div className="row" style={{ marginTop: 'var(--s3)', gap: 'var(--s2)' }}>
        <a className="btn sm" href={href.findings(tenant)}>
          Per-device verdicts →
        </a>
        {integrity.unverified > 0 ? (
          <a className="btn sm ghost" href={href.verifyUnverified(tenant)}>
            Verify the {n(integrity.unverified)} unverified →
          </a>
        ) : null}
      </div>
    </section>
  )
}
