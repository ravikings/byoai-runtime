/**
 * Entry detail — `/console/{tenant}/entries/{device_id}/{seq}`.
 *
 * The address rule made visible: a seq only means something beside its
 * device, and this URL — and the endpoint behind it — says both. On the
 * local host the entry *is* the record: the sealed payload, the Merkle path
 * that places it under the checkpoint, and the checkpoint itself, so the
 * offline-verify claim ("fold the leaf through these hashes, check the
 * signature") is readable at the row, not just promised by the product.
 *
 * What the payload can never carry is message text: the seal binds a
 * fingerprint and a length, and the preview only exists if the admin opted
 * into redaction. The page prints the stored shape verbatim rather than
 * summarising it — this is the evidence, and the evidence must be auditable
 * as bytes.
 */
import { createFileRoute } from '@tanstack/react-router'
import type { ReactNode } from 'react'

import { useEntry } from '@/api/queries'
import { HttpError, SchemaMismatchError } from '@/api/client'
import { PanelLoading } from '@/components/fleet/PanelState'
import { n, stamp } from '@/components/fleet/format'
import { useHref } from '@/app/hrefContext'

export const Route = createFileRoute('/console/$tenant/entries/$device/$seq')({
  component: EntryDetailPage,
})

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div style={{ fontSize: 'var(--t-body)' }}>{children}</div>
    </div>
  )
}

function EntryDetailPage() {
  const href = useHref()
  const { tenant, device: deviceId, seq } = Route.useParams()
  const query = useEntry(tenant, deviceId, seq)
  const r = query.data

  if (query.error !== null && r === undefined) {
    const e = query.error
    const isMissing = e instanceof HttpError && e.status === 404
    const body = e instanceof HttpError ? e.body : ''
    return (
      <>
        <div className="row" style={{ marginBottom: 'var(--s3)' }}>
          <h1>Entry</h1>
          <span className={isMissing ? 'tag unknown' : 'tag bad'}>
            {isMissing ? 'no such record' : 'could not read'}
          </span>
        </div>
        <div className={isMissing ? 'banner warn' : 'banner bad'}>
          <span className={isMissing ? 'dot unknown' : 'dot bad'} />
          <span>
            {isMissing ? (
              <b>This host holds no ledger that answers this address.</b>
            ) : (
              <b>The entry could not be read.</b>
            )}{' '}
            <span className="mono">{deviceId} · {seq}</span>
            {e instanceof SchemaMismatchError ? ' — the response did not match the console contract, so nothing shown here is known.' : ''}
          </span>
        </div>
        <p className="mono muted">{isMissing ? body : e.message}</p>
        <a className="btn" href={href.ledger(tenant)}>← Ledger</a>
      </>
    )
  }

  if (r === undefined) {
    return <PanelLoading title={`Entry ${deviceId} · ${seq}`} rows={6} />
  }

  const { entry, payload, proof, checkpoint } = r
  return (
    <>
      <div className="row" style={{ marginBottom: 'var(--s3)' }}>
        <h1>Entry</h1>
        <span className="seq-scoped">
          <span className="dev">…{entry.device_id.slice(-7)}</span>
          <span className="n">{n(entry.seq)}</span>
        </span>
        <span className="tag info">sealed record</span>
        <div className="spacer" style={{ flex: 1 }} />
        <a className="btn sm" href={href.ledger(tenant)}>
          ← Ledger
        </a>
      </div>

      <div className="scope" style={{ marginBottom: 'var(--s4)' }}>
        <span>
          device <b>{entry.device_id}</b>
        </span>
        <span>
          sealed at <b>{entry.ts === null ? 'unreadable stamp' : stamp(entry.ts)}</b>
        </span>
        <span>
          surface <b>{entry.surface}</b>
        </span>
        {entry.agent_id ? <span>agent <b className="mono">{entry.agent_id}</b></span> : null}
      </div>

      <div className="col2" style={{ alignItems: 'start' }}>
        <section className="panel">
          <div className="panel-head">
            <h3 className="label">The fact, as sealed</h3>
            <span className={entry.status === 'blocked' ? 'tag bad' : entry.flags.length > 0 ? 'tag warn' : 'tag ok'}>
              {entry.status === 'blocked' ? 'stopped' : entry.flags.length > 0 ? 'flagged' : 'passed'}
            </span>
          </div>
          <div className="kv">
            <span className="mono muted">kind</span><span className="mono">{entry.kind}</span>
            <span className="mono muted">status</span><span className="mono">{entry.status ?? '—'}</span>
            <span className="mono muted">verdict</span><span className="mono">{entry.verdict ?? '—'}</span>
            <span className="mono muted">flags</span>
            <span className="mono">{entry.flags.length === 0 ? 'none' : entry.flags.join(', ')}</span>
            <span className="mono muted">chars</span>
            <span className="mono">{entry.chars === null ? '—' : n(entry.chars)}</span>
            <span className="mono muted">seal</span>
            <span className="hash">{entry.seal === '' ? '—' : entry.seal}</span>
          </div>

          <h3 className="label" style={{ marginTop: 'var(--s4)' }}>Sealed payload</h3>
          <p className="mono dim" style={{ fontSize: 'var(--t-micro)', margin: 0 }}>
            Stored bytes, verbatim — no message text was ever sealed. The fingerprint
            (text_hmac) is keyed to this Mac only.
          </p>
          <div className="payload">{JSON.stringify(payload, null, 2)}</div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <h3 className="label">Proof</h3>
            {proof === null ? (
              <span className="tag unknown">not in the proof window</span>
            ) : (
              <span className="tag ok">Merkle path present</span>
            )}
          </div>
          {proof === null ? (
            <p className="muted" style={{ fontSize: 'var(--t-body)' }}>
              The chain keeps the newest proof window in memory; this entry's leaf hash is
              still part of the tree, and the full receipt for any recent seal is served at{' '}
              <span className="mono">GET /api/receipt/&lt;seal&gt;</span> — it verifies offline
              with no server at all.
            </p>
          ) : (
            <>
              <Field label="leaf = sha256(0x00 ‖ canonical(payload))">
                <span className="hash">{proof.leaf_hash}</span>
                <div className="hash mono">position {n(proof.leaf_index)}</div>
              </Field>
              <h3 className="label" style={{ marginTop: 'var(--s3)' }}>
                Fold steps — {n(proof.steps.length)} sibling{proof.steps.length === 1 ? '' : 's'}
              </h3>
              {proof.steps.map((s, i) => (
                <div key={i} className="row" style={{ gap: 'var(--s2)', marginBottom: 'var(--s1)' }}>
                  <span className="tag">{s.side}</span>
                  <span className="hash">{s.sibling}</span>
                </div>
              ))}
              <div className="row" style={{ marginTop: 'var(--s3)', gap: 'var(--s2)' }}>
                <span className="mono muted">folds to root</span>
                <span className="hash">{proof.root_hex}</span>
              </div>
            </>
          )}
          <div className="pair" style={{ marginTop: 'var(--s4)', gap: 'var(--s2)' }}>
            {checkpoint === null ? (
              <span className="tag unknown">no checkpoint signed yet</span>
            ) : (
              <>
                <span className={checkpoint.covers_this_entry ? 'tag ok' : 'tag warn'}>
                  {checkpoint.covers_this_entry ? 'covered by the signed checkpoint' : 'awaiting the next checkpoint'}
                </span>
                <span className="mono dim">
                  height {n(checkpoint.height)} · root{' '}
                  <span className="hash">{checkpoint.root ? `${checkpoint.root.slice(0, 16)}…` : '—'}</span>
                  {checkpoint.ts ? ` · signed ${stamp(checkpoint.ts)} UTC` : ''}
                </span>
              </>
            )}
          </div>
          {entry.seal !== '' ? (
            <p className="provenance" style={{ marginTop: 'var(--s3)' }}>
              portable receipt (verifies offline):{' '}
              <a
                className="ref"
                href={`/api/receipt/${encodeURIComponent(entry.seal)}`}
                target="_blank"
                rel="noreferrer"
              >
                GET /api/receipt/{entry.seal.slice(0, 12)}… ↗
              </a>
            </p>
          ) : null}
        </section>
      </div>
    </>
  )
}
