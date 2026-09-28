/**
 * Enrollment — `/console/{tenant}/settings/enrollment`. The overview's blind
 * spot names the one device class no count can see — a Mac that was never
 * enrolled. This is the screen that closes the loop it opens: what this host
 * is connected to, what may leave it, who signed the policy if anyone did,
 * and — read from disk, not from a settings form — what is actually stored
 * and where. Every value is live state; the page proves "no text was kept"
 * by counting the rows that hold text, because an assertion the record can
 * contradict is worth nothing here.
 */
import { useEffect, useMemo } from 'react'
import { createFileRoute } from '@tanstack/react-router'
import { useHref } from '@/app/hrefContext'
import { useEnrollment } from '@/api/queries'
import { toScope, usePublishShellStatus } from '@/app/scope'
import { ScopeLine } from '@/components/ScopeLine'
import { PanelError, PanelLoading } from '@/components/fleet/PanelState'
import { duration, n } from '@/components/fleet/format'

export const Route = createFileRoute('/console/$tenant/settings/enrollment')({
  component: EnrollmentPage,
})

function ago(iso: string | null): string {
  if (iso === null) return 'never'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const s = (Date.now() - t) / 1000
  if (s < 0) return 'in the future (clock skew)'
  return `${duration(s)} ago`
}

function EnrollmentPage() {
  const href = useHref()
  const { tenant } = Route.useParams()
  const search = Route.useSearch()
  const scope = useMemo(() => toScope(tenant, search), [tenant, search])
  const query = useEnrollment(scope)
  const r = query.data

  const publish = usePublishShellStatus()
  // Enrollment is not a fleet health reading; publish only the denominator it
  // carries so the scope chip stays honest, and leave the three state dots to
  // the screens that actually observe coverage, integrity and ingest.
  useEffect(() => {
    publish(
      r === undefined
        ? null
        : { inclusion: { devices_included: r.connection.connected ? 1 : 0, devices_enrolled: 1 } },
    )
    return () => publish(null)
  }, [publish, r])

  if (r === undefined) {
    return (
      <>
        <h1 style={{ marginBottom: 'var(--s3)' }}>Enrollment</h1>
        {query.error !== null ? (
          <PanelError title="Enrollment" error={query.error} />
        ) : (
          <PanelLoading title="Enrollment" rows={5} />
        )}
      </>
    )
  }

  const connected = r.connection.connected
  const noTextLeaves = r.chain.tamper_evident && r.stored.sealed_with_text === 0 && !r.policy.keep_text

  return (
    <>
      <div className="row" style={{ marginBottom: 'var(--s3)' }}>
        <h1>Enrollment</h1>
        <span className="tag info">this device</span>
        <span className="hash">GET /v1/console/enrollment</span>
        <div className="spacer" style={{ flex: 1 }} />
        <a className="btn sm" href={href.fleet(tenant)}>← Fleet</a>
      </div>

      <ScopeLine
        tenant={tenant}
        search={search}
        inclusion={{ devices_included: connected ? 1 : 0, devices_enrolled: 1 }}
        extra={[`device ${r.device_id}`, `as of ${r.as_of}`]}
      />

      {/* The privacy-first proof, first: what this device keeps and whether
          any of it is leaving. It leads because it is the claim every number
          on the fleet screens rests on, and the record can check it. */}
      <section className="panel">
        <div className="panel-head">
          <h3 className="label">This device keeps</h3>
          <span className={noTextLeaves ? 'tag ok' : r.stored.less_private ? 'tag bad' : 'tag warn'}>
            {noTextLeaves ? 'no message text' : r.policy.keep_text ? 'previews stored' : 'text present'}
          </span>
        </div>
        <div className="kv">
          <span className="mono muted">sealed entries</span><span className="num">{n(r.chain.sealed_total)}</span>
          <span className="mono muted">ledger rows</span><span className="num">{n(r.stored.ledger_rows)}</span>
          <span className="mono muted">rows carrying text</span>
          <span className="num">
            {n(r.stored.rows_with_text)}
            {r.stored.rows_with_text > 0 ? <span className="tag warn" style={{ marginLeft: 'var(--s2)' }}>pre-privacy-first</span> : null}
          </span>
          <span className="mono muted">sealed with text</span>
          <span className="num">{n(r.stored.sealed_with_text)}</span>
          <span className="mono muted">record id</span>
          <span className="hash">{r.chain.record_id ?? '—'}</span>
          <span className="mono muted">mode / keep_text</span>
          <span className="mono">{r.policy.mode ?? '—'} / {r.policy.keep_text ? 'on' : 'off'}</span>
          <span className="mono muted">effective sharing</span>
          <span className="mono">{r.policy.sync}</span>
        </div>
        <p className="methodology">
          Read from disk at request time, not from a settings form. A row that
          predates privacy-first redaction still carries text until scrubbed;
          this page counts them rather than claiming zero it cannot check.
        </p>
        <div className="stack" style={{ marginTop: 'var(--s2)' }}>
          {Object.entries(r.stored.paths).filter(([, v]) => v).map(([k, v]) => (
            <div className="row" key={k} style={{ gap: 'var(--s3)' }}>
              <span className="mono muted" style={{ minWidth: '7rem' }}>{k}</span>
              <span className="hash">{v}</span>
            </div>
          ))}
        </div>
      </section>

      <div className="col2">
        {/* Connection: is this device enrolled to an org, and what may it send */}
        <section className="panel">
          <div className="panel-head">
            <h3 className="label">Connection</h3>
            <span className={connected ? 'tag ok' : 'tag unknown'}>
              {connected ? 'enrolled' : 'not enrolled'}
            </span>
          </div>
          {connected ? (
            <>
              <p style={{ margin: '0 0 var(--s2)', fontSize: 'var(--t-body)' }}>
                This device ships signed checkpoints to <b>{r.connection.remote_tenant ?? 'an organisation'}</b>
                {r.connection.base_url ? (
                  <> at <span className="mono">{r.connection.base_url}</span></>
                ) : null}.
              </p>
              <div className="kv">
                <span className="mono muted">enrolled</span>
                <span>{r.connection.enrolled_at ?? 'unknown'}</span>
                <span className="mono muted">last send</span>
                <span>{ago(r.connection.last_sent_at)}</span>
                <span className="mono muted">cadence</span>
                <span>{r.connection.every_hours ? `${n(r.connection.every_hours)}h` : '—'}</span>
                <span className="mono muted">unsent entries</span>
                <span className="num">{r.connection.unsent_entries === null ? '—' : n(r.connection.unsent_entries)}</span>
                {r.connection.needs_attention ? (
                  <>
                    <span className="mono muted">attention</span>
                    <span className="tag bad">the organisation refused this device</span>
                  </>
                ) : null}
              </div>
              {r.connection.last_error ? (
                <div className="banner warn" style={{ marginTop: 'var(--s3)', marginBottom: 0 }}>
                  <span className="dot warn" />
                  <span>{r.connection.last_error}</span>
                </div>
              ) : null}
            </>
          ) : (
            <div className="verdict unknown">
              <div className="state"><span className="dot unknown" /> Nothing enrolled</div>
              <p className="reading">
                This device is not enrolled to any organisation. Every fleet
                figure it produces stays on the Mac; the seal chain is still
                whole and provable here, it just reaches no one else.
              </p>
            </div>
          )}
        </section>

        {/* Sharing disclosure (invariant 4) + managed policy */}
        <section className="panel">
          <div className="panel-head">
            <h3 className="label">Who may see activity</h3>
            {r.sharing === null ? (
              <span className="tag ok">seal only</span>
            ) : (
              <span className={r.sharing.level === 'events' ? 'tag bad' : 'tag warn'}>
                {r.sharing.level}
              </span>
            )}
          </div>
          {r.sharing === null ? (
            <p className="mono muted" style={{ lineHeight: 1.45, margin: 0 }}>
              Sharing is at seal: only the checkpoint — root, entry count,
              signature — ever the counts. No per-message activity leaves,
              at any level, without being named here.
            </p>
          ) : (
            <div className="banner info">
              <span className="dot ok" />
              <span><b>{r.sharing.by}</b> — {r.sharing.words}</span>
            </div>
          )}
          <h3 className="label" style={{ marginTop: 'var(--s4)' }}>Managed policy</h3>
          {r.managed === null ? (
            <p className="mono muted" style={{ margin: 0 }}>
              Not managed — the local policy file decides how this device
              behaves; no organisation has locked a setting.
            </p>
          ) : (
            <div className="kv">
              <span className="mono muted">managed by</span><span><b>{r.managed.by ?? '—'}</b></span>
              <span className="mono muted">version</span><span className="num">{r.managed.version === null ? '—' : n(r.managed.version)}</span>
              <span className="mono muted">locked</span>
              <span className="mono">{r.managed.locked.length === 0 ? 'none' : r.managed.locked.join(', ')}</span>
              {r.managed.error ? (
                <>
                  <span className="mono muted">last poll</span>
                  <span className="tag bad">{r.managed.error}</span>
                </>
              ) : null}
            </div>
          )}
        </section>
      </div>
    </>
  )
}
