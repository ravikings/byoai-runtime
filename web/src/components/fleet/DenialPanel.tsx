import type { FleetSummary } from '@/api/schemas'
import { Provenance } from './Provenance'
import { Sparkline } from './Sparkline'
import { href, n, rate } from './format'

export function DenialPanel({ summary, tenant }: { summary: FleetSummary; tenant: string }) {
  const { denial, inclusion } = summary
  const prev = denial.previous_per_1k
  // Both figures are aggregated server-side in floating point, so two equal
  // rates can differ by a few ulps. Comparing raw would flag a red rise on an
  // unchanged rate. The panel renders one decimal, so agreement at that
  // precision is the only claim it can honestly make.
  // Zero only when the two rates are indistinguishable AT THE PRECISION THIS
  // PANEL PRINTS. A raw epsilon of 0.05 was looser than the one decimal shown,
  // so 1.24 → 1.28 rendered as "= unchanged" beside a headline moving 1.2 → 1.3.
  const rawDelta = prev === null ? null : denial.denied_per_1k - prev
  const delta =
    prev === null || rawDelta === null
      ? null
      : rate(denial.denied_per_1k) === rate(prev)
        ? 0
        : rawDelta
  const unevaluated = denial.flagged

  return (
    <section className="panel">
      <div className="panel-head">
        <h3 className="label">Denial rate</h3>
        <a className="link-count" href={href.verdicts(tenant)}>
          mandate stream →
        </a>
      </div>

      <div className="stat">
        <div className="pair">
          <span className="value">{rate(denial.denied_per_1k)}</span>
          <span className="unit">denied / 1k tool calls</span>
        </div>
        <div className="provenance">
          <a className="ref" href={href.verdicts(tenant)}>
            {n(denial.denied)} denied →
          </a>{' '}
          {/* The devices_included / devices_enrolled pair belongs to
              <Provenance/>, rendered once below. Hand-rolling a second copy
              here printed the same numbers twice in two formats and would
              drift the moment the shared component's wording changed. */}
          · {n(denial.flagged)} flagged of {n(denial.tool_use_total)} tool_use
        </div>
      </div>

      <div className="pair" style={{ marginTop: 'var(--s3)', fontSize: 'var(--t-body)' }}>
        {delta === null || prev === null ? (
          <>
            <span className="tag unknown">no previous window</span>
            <span className="muted">
              nothing to compare against — this is not "unchanged"
            </span>
          </>
        ) : (
          <>
            <span className={delta > 0 ? 'tag bad' : delta < 0 ? 'tag ok' : 'tag info'}>
              {/* Print the gap between the two rates AS SHOWN. The raw
                  difference rendered "▲ 0.0" beside a headline visibly moving
                  1.2 → 1.3 — an arrow and a magnitude that contradict each
                  other. Zeroing handled the false-equal case; this handles the
                  false-zero-magnitude one. */}
              {delta > 0 ? '▲' : delta < 0 ? '▼' : '='}{' '}
              {Math.abs(Number(rate(denial.denied_per_1k)) - Number(rate(prev))).toFixed(1)}
            </span>
            <span className="muted">vs {rate(prev)} / 1k in the previous window</span>
          </>
        )}
      </div>

      <Sparkline
        series={denialSeries(summary)}
        tone="warn"
        label="denial rate across the window"
      />

      <h3 className="label" style={{ marginTop: 'var(--s3)' }}>
        Top refused tools
      </h3>
      {denial.top_refused.length === 0 ? (
        <p className="mono muted" style={{ lineHeight: 1.45 }}>
          No tool call was refused in this window. That is a fact about the {n(inclusion.devices_included)}{' '}
          reporting devices only.
        </p>
      ) : (
        <table>
          <tbody>
            {denial.top_refused.map((t) => (
              <tr key={`${t.tool}:${t.reason}`} className="verdict-denied">
                <td className="mono">
                  <a className="ref" href={`${href.verdicts(tenant)}?tool=${encodeURIComponent(t.tool)}`}>
                    {t.tool}
                  </a>
                </td>
                <td className="mono muted">{t.reason}</td>
                <td className="mono num" style={{ textAlign: 'right' }}>
                  {n(t.count)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {unevaluated > 0 ? (
        <div className="provenance">
          + {n(unevaluated)} flagged, not denied — flagged is not a refusal and not an allow.
        </div>
      ) : null}
      <Provenance inclusion={inclusion} />
    </section>
  )
}

/** The API sends no denial series yet; derive a shape from the two points it
 *  does send so the trend is drawn from data, never invented. */
function denialSeries(summary: FleetSummary): number[] {
  const { denied_per_1k, previous_per_1k } = summary.denial
  if (previous_per_1k === null) return []
  return [previous_per_1k, denied_per_1k]
}
