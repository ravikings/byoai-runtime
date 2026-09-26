/**
 * Mandate — the verdict stream (spec §6.5, design §6). The one screen whose
 * whole subject is decisions, so the posture banner sits above the table:
 * the counts mean nothing until you know whether a "flagged" was allowed
 * through or stopped. On the local host the rules run inline at capture and
 * every sealed interaction IS a verdict, so the stream is the ledger read a
 * different way — the same rows, grouped by outcome. The observe-mode
 * counter is the one number that justifies a posture: "N actions would have
 * been stopped" is the whole value of watching without blocking, and under
 * an enforcing mode the host sends null for it rather than a 0 that would
 * claim the counter was checked and found empty.
 */
import { useEffect, useMemo } from 'react'
import { createFileRoute } from '@tanstack/react-router'

import { useVerdicts } from '@/api/queries'
import { toScope, usePublishShellStatus, type ShellStatus } from '@/app/scope'
import { ScopeLine } from '@/components/ScopeLine'
import { PanelError, PanelLoading } from '@/components/fleet/PanelState'
import { clock, href as fleetHref, n } from '@/components/fleet/format'
import { useHref } from '@/app/hrefContext'

export const Route = createFileRoute('/console/$tenant/mandate/verdicts')({
  component: VerdictsPage,
})

const VERDICT_TAG: Record<'allowed' | 'flagged' | 'denied', string> = {
  allowed: 'tag ok',
  flagged: 'tag warn',
  denied: 'tag bad',
}

function deviceTail(deviceId: string): string {
  return `…${deviceId.slice(-7)}`
}

function VerdictsPage() {
  const href = useHref()
  const { tenant } = Route.useParams()
  const search = Route.useSearch()
  const scope = useMemo(() => toScope(tenant, search), [tenant, search])
  const query = useVerdicts(scope)
  const r = query.data

  const publish = usePublishShellStatus()
  useEffect(() => {
    publish(shellFor(tenant, r, query.error !== null))
    return () => publish(null)
  }, [publish, tenant, r, query.error])

  return (
    <>
      <div className="row" style={{ marginBottom: 'var(--s3)' }}>
        <h1>Mandate — verdict stream</h1>
        <span className="tag info">read-only</span>
        <span className="hash">GET /v1/console/verdicts</span>
        <div className="spacer" style={{ flex: 1 }} />
        <a className="btn sm" href={href.findings(tenant)}>
          Denial findings →
        </a>
        <a className="btn sm" href={href.fleet(tenant)}>
          ← Fleet
        </a>
      </div>

      <ScopeLine
        tenant={tenant}
        search={search}
        inclusion={r?.inclusion}
        extra={r === undefined ? [] : [`${n(r.verdicts.length)} verdicts in window`]}
      />

      {r === undefined ? (
        query.error !== null ? (
          <PanelError title="Verdict stream" error={query.error} />
        ) : (
          <PanelLoading title="Verdict stream" rows={6} />
        )
      ) : (
        <>
          {/* Posture banner — the verdict counts below are read differently
              under each posture, so it is stated first, not inferred. */}
          <div className={r.enforcement === 'observe' ? 'banner warn' : 'banner info'}>
            <span className={r.enforcement === 'observe' ? 'dot warn' : 'dot ok'} />
            <span>
              <b>
                {r.enforcement === 'observe'
                  ? 'Observe mode — nothing is stopped.'
                  : 'Enforcing — flagged actions are acted on at capture.'}
              </b>{' '}
              {r.observe_flagged === null ? (
                <>
                  {n(r.rollup.flagged)} flagged and {n(r.rollup.denied)} denied across{' '}
                  {n(r.rollup.allowed + r.rollup.flagged + r.rollup.denied)} sealed verdicts.
                  The counter of actions that would have been stopped does not apply to an
                  enforcing policy — it is not zero, it is not this mode's question.
                </>
              ) : (
                <>
                  {n(r.observe_flagged)} action{r.observe_flagged === 1 ? '' : 's'} would have
                  been stopped had this policy enforced. That number is the entire value of
                  observing — read it before you turn enforcement on.
                </>
              )}
            </span>
          </div>

          {/* Denial latches above the stream: a repeat attempt is the fact a
              verdict table flattens into rows. It leads when it exists. */}
          {r.latches.length > 0 ? (
            <section className="panel">
              <div className="panel-head">
                <h3 className="label">Denial latches</h3>
                <span className="mono dim">repeat attempts after a first denial</span>
              </div>
              <ul className="latch">
                {r.latches.map((g) => (
                  <li key={`${g.device_id}:${g.surface}:${g.tool ?? ''}`}>
                    <div className="cellstack">
                      <a className="ref mono" href={href.device(tenant, g.device_id)}>
                        {g.device_id}
                      </a>
                      <span className="mono muted">{g.surface}{g.tool ? ` · ${g.tool}` : ''}</span>
                    </div>
                    <span className="attempts">
                      +{n(g.attempts)} after first
                    </span>
                    {g.first_seq === null ? (
                      <span className="seq-scoped none">
                        <span className="dev">{deviceTail(g.device_id)}</span>
                        <span className="n">first seq —</span>
                      </span>
                    ) : (
                      <a className="seq-scoped" href={href.entry(tenant, g.device_id, g.first_seq)}>
                        <span className="dev">{deviceTail(g.device_id)}</span>
                        <span className="n">{n(g.first_seq)}</span>
                      </a>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <section className="panel flush">
            <div className="panel-head">
              <h3 className="label">Verdict stream</h3>
              <span className="mono dim">
                {n(r.rollup.allowed)} allowed · {n(r.rollup.flagged)} flagged ·{' '}
                {n(r.rollup.denied)} denied
              </span>
              <div className="spacer" style={{ flex: 1 }} />
              <span className="mono muted">newest first</span>
            </div>
            {r.verdicts.length === 0 ? (
              <div className="empty">
                <div className="headline">No sealed verdicts in this window.</div>
                <p className="mono muted" style={{ lineHeight: 1.45, margin: 0 }}>
                  Absence of verdicts is not a posture reading — nothing ran, so nothing was
                  decided. Widen the window, or check{' '}
                  <a className="ref" href={href.coverage(tenant)}>coverage</a> if you expected
                  traffic here.
                </p>
              </div>
            ) : (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>time</th>
                      <th>device · seq</th>
                      <th>surface / agent</th>
                      <th>tool</th>
                      <th>verdict</th>
                      <th>reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.verdicts.map((v, i) => (
                      <tr key={`${v.device_id}:${v.seq ?? 'x'}:${i}`} className={`verdict-${v.verdict}`}>
                        <td className="mono">{clock(v.ts)}</td>
                        <td>
                          {v.seq === null ? (
                            <span className="mono dim">older than the window</span>
                          ) : (
                            <a className="seq-scoped" href={href.entry(tenant, v.device_id, v.seq)}>
                              <span className="dev">{deviceTail(v.device_id)}</span>
                              <span className="n">{n(v.seq)}</span>
                            </a>
                          )}
                        </td>
                        <td>
                          <div className="cellstack">
                            <span>{v.surface}</span>
                            {v.agent_id ? <span className="hash">{v.agent_id}</span> : null}
                          </div>
                        </td>
                        <td className="mono">{v.tool ?? <span className="dim">message</span>}</td>
                        <td>
                          <span className={VERDICT_TAG[v.verdict]}>{v.verdict}</span>
                        </td>
                        <td className="mono muted">{v.reason ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="register-foot">
              <span>
                showing <b>{n(r.verdicts.length)}</b> verdicts in{' '}
                <span className="mono">{clock(r.window.from)}–{clock(r.window.to)} UTC</span>
              </span>
              <span className="spacer" />
              <span className="mono dim">
                the stream covers what this host keeps in memory; every older verdict is still
                sealed in the ledger
              </span>
            </div>
          </section>
        </>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ *
 * Health dots: a stream with a broken-chain finding or repeated denials
 * is posture-degraded; the shell says so only when the stream read it.
 * ------------------------------------------------------------------ */

function shellFor(
  tenant: string,
  r: ReturnType<typeof useVerdicts>['data'],
  failed: boolean,
): ShellStatus {
  if (r === undefined) {
    const why = failed ? 'the verdict stream could not be loaded' : 'verdict stream loading'
    return {
      ingest: { state: 'unknown', state_label: 'unknown', worst: why, href: fleetHref.verdicts(tenant) },
    }
  }
  const total = r.rollup.allowed + r.rollup.flagged + r.rollup.denied
  const ingest =
    total === 0
      ? {
          state: 'unknown' as const,
          state_label: 'unknown',
          worst: 'no verdicts in this window — nothing decided, not a clean pass',
          href: fleetHref.verdicts(tenant),
        }
      : r.enforcement === 'observe'
        ? {
            state: 'warn' as const,
            state_label: 'observing',
            worst: `${n(r.rollup.flagged)} flagged, ${n(r.observe_flagged ?? 0)} would have stopped`,
            href: fleetHref.verdicts(tenant),
          }
        : {
            state: 'ok' as const,
            state_label: 'enforcing',
            worst: `${n(r.rollup.denied)} denied of ${n(total)} verdicts`,
            href: fleetHref.verdicts(tenant),
          }
  return { inclusion: r.inclusion, ingest }
}
