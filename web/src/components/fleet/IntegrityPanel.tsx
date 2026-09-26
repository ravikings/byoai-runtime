import type { FleetSummary } from '@/api/schemas'
import { useHref } from '@/app/hrefContext'
import { Provenance } from './Provenance'
import { n, clock } from './format'

/**
 * Three segments, never two. `unverified` is not a shade of intact — a verify
 * job that never ran proves nothing, and folding it into the green would be
 * the single worst bug this screen could ship.
 */
export function IntegrityPanel({ summary, tenant }: { summary: FleetSummary; tenant: string }) {
  const href = useHref()
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

      {summary.local ? (
        // The local host walks the chain itself — every entry re-hashed, the
        // checkpoint signature re-checked — on the same read that produced
        // this panel. That is the one claim this screen gets to make with a
        // root hash attached to it, so it shows the root, the moment of the
        // walk, and the device whose key signed it. On every other backend
        // `local` is absent and this block never renders: stored is not
        // verified there, and a footer implying a walk nobody ran would be
        // the exact lie the panel above exists to prevent. The accent is the
        // walk's own outcome — a green rule beside a broken chain would be
        // the same colour-carrying-the-wrong-state bug, in the one block
        // that exists to show the state honestly.
        <div className={summary.integrity.broken > 0 || summary.local.incidents > 0 ? 'local-proof bad' : 'local-proof'}>
          <div className="pair">
            <span className="mono muted">chain walked on this read</span>
            <span className={summary.integrity.broken > 0 || summary.local.incidents > 0 ? 'tag bad' : 'tag ok'}>
              {summary.local.incidents === 0
                ? summary.integrity.broken > 0
                  ? 'walk failed'
                  : 'signature valid'
                : `${n(summary.local.incidents)} incident${summary.local.incidents === 1 ? '' : 's'}`}
            </span>
          </div>
          <div className="row" style={{ gap: 'var(--s2)' }}>
            <span className="hash">root {summary.local.merkle_root ? `${summary.local.merkle_root.slice(0, 10)}…` : '—'}</span>
            <span className="mono dim">{n(summary.local.sealed_total)} entries</span>
            <span className="mono dim">walked {clock(summary.local.chain_verified_at)} UTC</span>
          </div>
          <div className="provenance">
            device <span className="mono">{summary.local.device_id}</span> · host{' '}
            <span className="mono">{summary.local.host}</span>
            {' · '}
            {summary.local.connected ? (
              summary.local.coriqo_tenant === null ? (
                'connected, tenant not named'
              ) : (
                <>
                  ships to <b>{summary.local.coriqo_tenant}</b> — arrival at the fleet server is{' '}
                  <a className="ref" href={href.coverage(tenant)}>that server&apos;s record</a>, not this one
                </>
              )
            ) : (
              'not connected — nothing has left this Mac'
            )}
          </div>
        </div>
      ) : null}

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
