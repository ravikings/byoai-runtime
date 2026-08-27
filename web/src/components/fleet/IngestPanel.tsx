import type { FleetSummary } from '@/api/schemas'
import { useHref } from '@/app/hrefContext'
import { Provenance } from './Provenance'
import { Sparkline } from './Sparkline'
import { ago, clock, duration, n, secondsSince } from './format'

export function IngestPanel({ summary, tenant }: { summary: FleetSummary; tenant: string }) {
  const href = useHref()
  const { ingest, inclusion, window } = summary
  const flat = ingest.rate_flat_for_minutes
  const oldestS = secondsSince(ingest.oldest_unshipped_at)

  return (
    <section className="panel">
      <div className="panel-head">
        <h3 className="label">Ingest</h3>
        {flat === null ? (
          <span className="tag ok">rate live</span>
        ) : (
          <span className="tag bad">rate flat {duration(flat * 60)}</span>
        )}
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'max-content 1fr',
          gap: 'var(--s2) var(--s3)',
          alignItems: 'baseline',
          fontSize: 'var(--t-body)',
        }}
      >
        <span className="mono muted">entries received</span>
        <span className="num">
          <b>{n(ingest.entries_received)}</b> <span className="mono muted">in window</span>
        </span>

        <span className="mono muted">backlog</span>
        <span className="num">
          {/* Backlog lives on the device, not here. The ingest side sees what
              arrived; it cannot see what a device is still holding, and a
              device that stopped shipping looks exactly like one with nothing
              left to send. Rendering 0 would report "nothing outstanding"
              from data nobody has. */}
          {ingest.backlog_entries === null ? (
            <span className="tag unknown">
              unknown — the backlog is only visible on the device
            </span>
          ) : (
            <>
              <b>{n(ingest.backlog_entries)}</b>{' '}
              <span className="mono muted">unsynced,</span>{' '}
              <a className="ref" href={href.devices(tenant)}>
                {n(ingest.backlog_devices ?? 0)} device
                {ingest.backlog_devices === 1 ? '' : 's'} →
              </a>
            </>
          )}
        </span>

        <span className="mono muted">oldest unshipped</span>
        <span className="num">
          {ingest.oldest_unshipped_at === null ? (
            <span className="mono dim">nothing unshipped that this fleet can see</span>
          ) : oldestS === null ? (
            // A stamp that exists but will not parse is unknown, not empty.
            // Collapsing it into the "nothing unshipped" branch would report a
            // healthy backlog on the strength of data we could not read.
            <span className="tag warn">
              unshipped entry exists, but its timestamp could not be read —{' '}
              <span className="mono">{ingest.oldest_unshipped_at}</span>
            </span>
          ) : (
            <>
              {/* Same guard ago() applies: duration() floors negatives, so a
                  stamp ahead of the browser clock rendered "0s" — the freshest
                  possible backlog — while the last-batch field beside it
                  correctly reported skew for the identical kind of stamp. */}
              <b>{oldestS < 0 ? `${duration(-oldestS)} in the future (clock skew)` : duration(oldestS)}</b>{' '}
              <a className="ref" href={href.coverage(tenant)}>
                oldest unshipped entry →
              </a>
            </>
          )}
        </span>

        <span className="mono muted">last batch</span>
        <span className="num">
          {ingest.last_batch_at === null ? (
            <span className="mono dim">no batch has ever arrived in this scope</span>
          ) : (
            <>
              <b>{ago(ingest.last_batch_at)}</b>{' '}
              <a className="ref" href={href.devices(tenant)}>
                which device →
              </a>
            </>
          )}
        </span>

        <span className="mono muted">checkpoints pending</span>
        <span className="num">
          {ingest.checkpoints_pending === null ? (
            <span className="tag unknown">unknown — device-side</span>
          ) : (
            <>
              <b>{n(ingest.checkpoints_pending)}</b>{' '}
              <a className="ref" href={href.coverage(tenant)}>
                checkpoint gaps →
              </a>
            </>
          )}
        </span>
      </div>

      <h3 className="label" style={{ marginTop: 'var(--s3)' }}>
        Ingest rate — entries/min
      </h3>
      <Sparkline
        series={ingest.rate_series}
        flatForMinutes={flat}
        label={
          flat === null
            ? 'ingest rate over the window'
            : `ingest rate flat-lining for ${flat} minutes`
        }
      />
      <div
        className="row"
        style={{
          justifyContent: 'space-between',
          fontFamily: 'var(--font-mono)',
          fontSize: 'var(--t-micro)',
          color: 'var(--ice-dim)',
        }}
      >
        <span>{clock(window.from)}</span>
        <span>
          {ingest.last_batch_at === null ? 'no batch' : `last batch ${clock(ingest.last_batch_at)}`}
        </span>
        <span>now {clock(window.to)}</span>
      </div>

      {flat === null ? null : (
        <div className="caveat" style={{ marginTop: 'var(--s2)' }}>
          A flat rate is an incident, not a quiet period — recorders ship on a timer.{' '}
          <a className="ref" href={href.coverage(tenant)}>
            Why is ingest stalled? →
          </a>
        </div>
      )}
      <Provenance inclusion={inclusion} note="a silent device's backlog is unknown" />
    </section>
  )
}
