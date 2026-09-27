/**
 * Ledger — the sealed record, `/console/{tenant}/ledger` (spec §6.2).
 *
 * Every other console screen reads a rollup; this one reads the record
 * itself. On the local host the ledger is the seal chain: heights 1..n, each
 * entry the complete fact the device sealed at capture time. Two things the
 * page does that the rollups cannot: `head()` and `missing_ranges()` are
 * computed over the log while answering the request (a gap in an append-only
 * ledger gets a banner, not a cell, and the banner can only appear because
 * the walk that just ran proved there isn't one), and cursor paging means
 * "showing N of the chain" is a page — never the product's whole reach.
 *
 * Sessions and trajectories are the fleet-server rollups the spec makes the
 * primary list; a single device cannot compute them and the wire says null
 * rather than pretending an empty list is an answer.
 */
import { useEffect, useMemo } from 'react'
import { createFileRoute, useLocation } from '@tanstack/react-router'

import { useLedger } from '@/api/queries'
import { toScope, usePublishShellStatus } from '@/app/scope'
import { ScopeLine } from '@/components/ScopeLine'
import { PanelError, PanelLoading } from '@/components/fleet/PanelState'
import { clock, duration, href as fleetHref, n, stamp } from '@/components/fleet/format'
import { useHref } from '@/app/hrefContext'
import { withScope } from '@/lib/withScope'

export const Route = createFileRoute('/console/$tenant/ledger')({
  component: LedgerPage,
})

const KIND_WORDS: Record<'message' | 'tool_use' | 'mandate_verdict', string> = {
  message: 'message',
  tool_use: 'tool',
  mandate_verdict: 'verdict',
}

function verdictTag(e: { status: string | null; flags: string[] }): { cls: string; word: string } {
  if (e.status === 'blocked') return { cls: 'tag bad', word: 'stopped' }
  if (e.flags.length > 0) return { cls: 'tag warn', word: 'flagged' }
  if (e.status === 'failed') return { cls: 'tag bad', word: 'failed' }
  return { cls: 'tag ok', word: 'passed' }
}

function LedgerPage() {
  const href = useHref()
  const { tenant } = Route.useParams()
  const search = Route.useSearch()
  const scope = useMemo(() => toScope(tenant, search), [tenant, search])

  // The cursor is the oldest seq of the previous page, read straight off the
  // URL: it is view state, and view state lives in the address (§4.5) — the
  // scope params alone are parsed by the tenant route, so the page reads the
  // raw string rather than widening the shared scope type.
  const location = useLocation()
  const cursor = new URLSearchParams(location.searchStr).get('cursor')
  const query = useLedger(scope, cursor)
  const r = query.data

  // The ledger's facts are the record, not a fleet rollup; publish the
  // denominator the screen does know and leave the three state dots to the
  // screens that read the states they describe.
  const publish = usePublishShellStatus()
  useEffect(() => {
    publish(r === undefined ? null : { inclusion: r.inclusion })
    return () => publish(null)
  }, [publish, r])

  return (
    <>
      <div className="row" style={{ marginBottom: 'var(--s3)' }}>
        <h1>Ledger</h1>
        <span className="tag info">read-only</span>
        <span className="hash">GET /v1/console/ledger</span>
        <div className="spacer" style={{ flex: 1 }} />
        <a className="btn sm" href={href.verdicts(tenant)}>
          Verdict stream →
        </a>
        <a className="btn sm" href={href.fleet(tenant)}>
          ← Fleet
        </a>
      </div>

      <ScopeLine
        tenant={tenant}
        search={search}
        inclusion={r?.inclusion}
        extra={r === undefined
          ? []
          : [
              `head ${r.head.root ? `${r.head.root.slice(0, 10)}…` : '—'} · ${n(r.head.height)} entries`,
              cursor ? `cursor before ${cursor}` : 'newest first',
            ]}
      />

      {r === undefined ? (
        query.error !== null ? (
          <PanelError title="Ledger" error={query.error} />
        ) : (
          <PanelLoading title="Ledger" rows={8} />
        )
      ) : r.missing_ranges.length > 0 ? (
        // The one banner that outranks the table: a gap in an append-only
        // ledger means entries are gone, and nothing below this line can be
        // read as a complete record until they are accounted for.
        <div className="banner bad" style={{ marginBottom: 'var(--s4)' }}>
          <span className="dot bad" />
          <span>
            <b>Missing seq ranges.</b>{' '}
            {r.missing_ranges.map((g) => (
              <span key={`${g.from}-${g.to}`} className="mono">
                {` ${n(g.from)}–${n(g.to)} `}
              </span>
            ))}
            This ledger is not complete, and every rollup computed over it is short by those
            entries.
          </span>
          <span className="spacer" />
          <a className="btn sm" href={href.findings(tenant)}>
            Findings →
          </a>
        </div>
      ) : (
        <div className="banner info" style={{ marginBottom: 'var(--s4)' }}>
          <span className="dot ok" />
          <span>
            <b>head() walked while this page was read:</b> heights 1–{n(r.head.height)} are
            contiguous, no ranges missing. <span className="mono">{n(r.head.sealed_total)}</span>{' '}
            sealed entries ever; this view is window{' '}
            <span className="mono">{stamp(r.window.from)} → {clock(r.window.to)} UTC</span>.
          </span>
        </div>
      )}

      {r !== undefined && r.sessions === null ? (
        <p className="methodology" style={{ margin: '0 0 var(--s4)' }}>
          sessions / trajectories: <b>null on this host</b> — the session and
          trajectory indexes are what a fleet server computes across many devices.
          One Mac answers with its own chain and does not pretend an empty list
          is an answer it could give.
        </p>
      ) : null}

      {r !== undefined ? (
        <section className="panel flush">
          <div className="panel-head">
            <h3 className="label">Sealed entries</h3>
            <span className="mono dim">newest first</span>
            <div className="spacer" style={{ flex: 1 }} />
            <span className="mono muted">
              {n(r.entries.length)} shown · {n(r.head.sealed_total)} sealed
            </span>
          </div>
          {r.entries.length === 0 ? (
            <div className="empty">
              <div className="headline">Nothing was sealed inside this window.</div>
              <p className="mono muted" style={{ lineHeight: 1.45, margin: 0 }}>
                The chain still holds {n(r.head.sealed_total)} entries overall — widen the
                window, or read the absence in{' '}
                <a className="ref" href={href.coverage(tenant)}>the silence report</a> before
                you call this quiet.
              </p>
            </div>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>time</th>
                    <th>seq</th>
                    <th>surface / agent</th>
                    <th>kind</th>
                    <th>outcome</th>
                    <th>flags</th>
                    <th>chars</th>
                    <th>seal</th>
                  </tr>
                </thead>
                <tbody>
                  {r.entries.map((e) => {
                    const tag = verdictTag(e)
                    return (
                      <tr key={`${e.device_id}:${e.seq}`} className={tag.word === 'stopped' ? 'verdict-denied' : tag.word === 'flagged' ? 'verdict-flagged' : undefined}>
                        <td className="mono">
                          {e.ts === null ? (
                            <span className="tag warn">stamp unreadable</span>
                          ) : (
                            <>
                              {stamp(e.ts)}
                              <div className="hash">{duration((Date.now() - Date.parse(e.ts)) / 1000)} ago</div>
                            </>
                          )}
                        </td>
                        <td>
                          <a className="seq-scoped" href={href.entry(tenant, e.device_id, e.seq)}>
                            <span className="dev">…{e.device_id.slice(-7)}</span>
                            <span className="n">{n(e.seq)}</span>
                          </a>
                        </td>
                        <td className="mono">{e.surface}</td>
                        <td className="mono">{KIND_WORDS[e.kind]}</td>
                        <td><span className={tag.cls}>{tag.word}</span></td>
                        <td className="mono muted">
                          {e.flags.length === 0 ? '—' : e.flags.join(', ')}
                        </td>
                        <td className="mono num">{e.chars === null ? '—' : n(e.chars)}</td>
                        <td>
                          <a className="hash" href={href.entry(tenant, e.device_id, e.seq)} title="open entry and its Merkle proof">
                            {e.seal === '' ? '—' : `${e.seal.slice(0, 12)}…`}
                          </a>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
          <div className="register-foot">
            <span>
              page of <b>{n(r.entries.length)}</b> · cursor{' '}
              <span className="hash">{cursor ?? 'newest'}</span>
            </span>
            {cursor !== null ? (
              <a
                className="btn sm"
                href={withScope(
                  fleetHref.ledger(tenant),
                  (() => {
                    const p = new URLSearchParams()
                    if (search.window) p.set('window', search.window)
                    if (search.from) p.set('from', search.from)
                    if (search.to) p.set('to', search.to)
                    return p.toString()
                  })(),
                )}
              >
                ← newest
              </a>
            ) : null}
            <span className="spacer" />
            {r.next_cursor !== null ? (
              <a
                className="btn sm"
                href={withScope(
                  fleetHref.ledger(tenant),
                  (() => {
                    const p = new URLSearchParams()
                    if (search.window) p.set('window', search.window)
                    if (search.from) p.set('from', search.from)
                    if (search.to) p.set('to', search.to)
                    p.set('cursor', r.next_cursor as string)
                    return p.toString()
                  })(),
                )}
              >
                Older →
              </a>
            ) : (
              <span className="dim">older: none in this window</span>
            )}
          </div>
        </section>
      ) : null}
    </>
  )
}
