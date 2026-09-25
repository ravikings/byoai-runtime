/**
 * Ledger: every sealed entry with its receipt, the chain export, and (in the
 * rail) the offline check anyone can run on a receipt without trusting us.
 */
import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { fetchVerify } from '@/api/shield'
import { checkReceipt } from '@/lib/receipt'
import type { ReceiptCheck } from '@/lib/receipt'
import { ReceiptButton, SectionHead, downloadBlob, plural, tierLabel, tierTagClass, useFeed } from './shared'
import type { Item } from './shared'

export function Ledger({ onOpen }: { onOpen: (it: Item) => void }) {
  const verify = useQuery({ queryKey: ['shield-verify'], queryFn: fetchVerify, refetchInterval: 10_000 })
  const feed = useFeed()
  const sealed = (feed.data?.items ?? []).filter(it => it.seal)
  const v = verify.data

  // Fetched at click time: the export must match the chain now, not the
  // copy the screen last polled.
  const exportChain = useMutation({
    mutationFn: fetchVerify,
    onSuccess: fresh => downloadBlob(
      new Blob([JSON.stringify(fresh, null, 1)], { type: 'application/json' }),
      'byoai-chain-state.json'),
  })

  return (
    <div className="shield-split">
      <div className="shield-main">
        <SectionHead id="ledger-h" title="Sealed entries" accent />
        <div className="ledger-head">
          {v
            ? v.tamper_evident
              ? <span className="tag ok">Intact</span>
              : <span className="tag bad">Broken at entry {v.broken_at ?? '?'}: {v.reason ?? 'mismatch'}</span>
            : <span className="tag unknown">Checking…</span>}
          {v?.tamper_evident && (
            <span className="mono">
              {plural(v.entries ?? 0, 'entry', 'entries')} · root {(v.merkle_root ?? '').slice(0, 12)}… · device{' '}
              {v.checkpoint?.device_id ?? 'not signed yet'}
            </span>
          )}
          <span className="spacer" />
          <button className="btn sm" disabled={exportChain.isPending} onClick={() => exportChain.mutate()}>
            {exportChain.isPending ? 'Exporting…' : 'Export chain state'}
          </button>
          {exportChain.isError && <span className="tag bad" role="alert">Export failed: {exportChain.error.message}</span>}
        </div>
        <p className="ledger-note">
          Each entry was sealed when Shield checked it. A seal covers what
          happened: the app, the verdict, the rules that matched, the message
          length and its fingerprint. It doesn't cover the words. Entries are
          folded into a Merkle tree and signed with this Mac's device key
          (Ed25519, file mode 0600, never leaves the machine). Change an entry
          later and its receipt stops checking out.
        </p>
        <div className="table-scroll">
          <table className="shield-table">
            <thead>
              <tr><th>When</th><th>App</th><th>What happened</th><th>Result</th><th>Seal</th><th /></tr>
            </thead>
            <tbody>
              {sealed.map(it => (
                <tr key={it.id} onClick={() => onOpen(it)} className="clickable">
                  <td>{it.date} {it.ts?.slice(0, 5)}</td>
                  <td>{it.surface.split(' ·')[0]}</td>
                  <td className="verb-cell">{it.verb}</td>
                  <td><span className={`tag ${tierTagClass(it.tier)}`}>{tierLabel(it)}</span></td>
                  <td><span className="hash">{it.seal.slice(0, 16)}</span></td>
                  <td onClick={e => e.stopPropagation()}><ReceiptButton seal={it.seal} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {feed.data && sealed.length === 0 && (
          <p className="empty-row">
            Nothing sealed yet. Everything Shield checks appears here with its seal and a receipt.
          </p>
        )}
      </div>
      <aside className="shield-rail">
        <ReceiptVerifier />
      </aside>
    </div>
  )
}

function ReceiptVerifier() {
  const [text, setText] = useState('')
  const [res, setRes] = useState<ReceiptCheck | null>(null)
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    try { setRes(await checkReceipt(text)) } finally { setBusy(false) }
  }
  return (
    <section className="panel" aria-labelledby="verify-h">
      <SectionHead id="verify-h" title="Check a receipt"
        help="Paste a downloaded receipt. This browser recomputes its hashes; nothing is sent anywhere." />
      <textarea
        className="receipt-input mono" aria-label="Receipt JSON"
        placeholder='{"kind":"byoai.receipt.v2", …}'
        value={text} onChange={e => { setText(e.target.value); setRes(null) }}
      />
      <button className="btn primary" disabled={busy || !text.trim()} onClick={run}>
        {busy ? 'Checking…' : 'Check receipt'}
      </button>
      {res && (
        <div className={`banner ${res.ok ? 'info' : 'bad'} receipt-result`} role="status">
          {res.ok
            ? res.level === 'merkle'
              ? <span><b>Receipt checks out.</b> The entry matches its seal and its proof leads to root {res.root?.slice(0, 12)}…. The device signature on that root is checked on the Mac that exported it{res.deviceId ? ` (${res.deviceId})` : ''}.</span>
              : <span><b>The entry matches its seal.</b> This receipt has no Merkle proof, so it can't be tied to a root.</span>
            : <span><b>Receipt does not check out.</b> {res.why}</span>}
        </div>
      )}
    </section>
  )
}
