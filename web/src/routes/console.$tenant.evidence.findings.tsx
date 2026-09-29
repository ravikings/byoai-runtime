/**
 * Findings — `/console/{tenant}/evidence/findings`.
 *
 * The full list the overview's `all N →` link promises: one row per open
 * finding, each carrying the device it belongs to, each reachable by the
 * seq or session it concerns. The empty state is the one place on this
 * screen where a design lie is tempting — "no findings" reads as "all
 * clear" — and it is not always the same fact: on the local host it means
 * this very read walked the chain and it held; on a fleet server it can
 * mean no verify job has run at all. The page says the host's own version.
 */
import { useEffect, useMemo } from 'react'
import { createFileRoute } from '@tanstack/react-router'

import { useFindings } from '@/api/queries'
import { toScope, usePublishShellStatus, type ShellStatus } from '@/app/scope'
import { ScopeLine } from '@/components/ScopeLine'
import { PanelEmpty, PanelError, PanelLoading } from '@/components/fleet/PanelState'
import { dotClass, refLink } from '@/components/fleet/finding'
import { href as fleetHref, n } from '@/components/fleet/format'
import { useHref } from '@/app/hrefContext'

export const Route = createFileRoute('/console/$tenant/evidence/findings')({
  component: FindingsPage,
})

function FindingsPage() {
  const href = useHref()
  const { tenant } = Route.useParams()
  const search = Route.useSearch()
  const scope = useMemo(() => toScope(tenant, search), [tenant, search])
  const query = useFindings(scope)
  const r = query.data

  const publish = usePublishShellStatus()
  useEffect(() => {
    publish(findingsShell(tenant, r, query.error !== null))
    return () => publish(null)
  }, [publish, tenant, r, query.error])

  return (
    <>
      <div className="row" style={{ marginBottom: 'var(--s3)' }}>
        <h1>Findings</h1>
        <span className="tag info">read-only</span>
        <span className="hash">GET /v1/console/fleet/findings</span>
        <div className="spacer" style={{ flex: 1 }} />
        <a className="btn sm" href={href.verifyUnverified(tenant)}>
          Verify unwalked ranges →
        </a>
        <a className="btn sm" href={href.fleet(tenant)}>
          ← Fleet
        </a>
      </div>

      <ScopeLine
        tenant={tenant}
        search={search}
        inclusion={r?.inclusion}
        extra={r === undefined ? [] : [`${n(r.total)} open finding${r.total === 1 ? '' : 's'}`]}
      />

      {r === undefined ? (
        query.error !== null ? (
          <PanelError title="Open findings" error={query.error} />
        ) : (
          <PanelLoading title="Open findings" rows={5} />
        )
      ) : (
        <section className="panel">
          {r.findings.length === 0 ? (
            <PanelEmpty headline="No open finding is recorded for this scope.">
              <p className="mono muted" style={{ lineHeight: 1.45 }}>
                On the local Shield this means the read that answered the fleet
                screens re-hashed every entry and the checkpoint signature held.
                On a fleet server, findings exist only where a verify job has
                walked the ledger — an empty list there is the absence of a
                check, <b>not a clean bill of health</b> for devices whose
                ranges have never been walked. The Integrity panel and the
                coverage report say which host you are on.
              </p>
            </PanelEmpty>
          ) : (
            r.findings.map((f) => {
              const link = refLink(tenant, f, href)
              return (
                <div key={f.id} className={`finding ${f.severity}`}>
                  <span className={dotClass(f.severity)} style={{ marginTop: '.35rem' }} aria-hidden="true" />
                  <div className="what">
                    <div>
                      <span className="mono dim">{f.severity}</span>{' '}
                      <b>{f.kind}</b> — {f.what}
                    </div>
                    <div className="where">
                      <span className="mono dim">device</span>
                      <a className="ref" href={href.device(tenant, f.device_id)}>
                        {f.device_id}
                      </a>
                      {link === null ? (
                        <span className="mono dim">· no seq recorded for this finding</span>
                      ) : (
                        <>
                          <span className="mono dim">·</span>
                          <a className="ref" href={link.to}>
                            {link.label}
                          </a>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              )
            })
          )}
          <div className="register-foot" style={{ border: 'none', padding: 'var(--s2) 0 0' }}>
            <span>
              showing <b>{n(r.findings.length)}</b> of <b>{n(r.total)}</b> open
            </span>
            <span className="spacer" />
            <a className="ref" href={href.coverage(tenant)}>
              the silence these findings cannot see →
            </a>
          </div>
        </section>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ *
 * Health dots: this screen knows findings, and findings ARE integrity.
 * ------------------------------------------------------------------ */

const UNKNOWN = (worst: string, to: string) => ({
  state: 'unknown' as const,
  state_label: 'unknown',
  worst,
  href: to,
})

function findingsShell(
  tenant: string,
  r: ReturnType<typeof useFindings>['data'],
  failed: boolean,
): ShellStatus {
  if (r === undefined) {
    const why = failed ? 'the findings list could not be loaded' : 'findings still loading'
    return { integrity: UNKNOWN(why, fleetHref.findings(tenant)) }
  }
  const bad = r.findings.filter((f) => f.severity === 'bad').length
  const warn = r.findings.filter((f) => f.severity === 'warn').length
  const integrity =
    bad > 0
      ? {
          state: 'bad' as const,
          state_label: 'broken',
          worst: `${n(bad)} finding${bad === 1 ? '' : 's'} mark this fleet broken`,
          href: fleetHref.findings(tenant),
        }
      : warn > 0
        ? {
            state: 'warn' as const,
            state_label: 'degraded',
            worst: `${n(warn)} warning finding${warn === 1 ? '' : 's'} open`,
            href: fleetHref.findings(tenant),
          }
        : UNKNOWN(
            // An empty findings list is a fact about what was checked, not
            // about the chain: it reads identically whether a verify walk
            // covered every device and found nothing, or no walk ran at all.
            // The list itself cannot tell those apart, so the dot cannot
            // claim intact from it — that rollup lives on the fleet overview,
            // where per-device verdicts are present.
            'no open finding is recorded — the list alone does not say whether a walk covered this fleet',
            fleetHref.findings(tenant),
          )
  return { inclusion: r.inclusion, integrity }
}
