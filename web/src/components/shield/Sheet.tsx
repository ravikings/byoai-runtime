/**
 * The detail drawer for one interaction: what happened, which rules matched,
 * and a way on to its Timeline journey or its receipt. Opened from a row on
 * Activity, the Ledger or the Timeline.
 */
import { useEffect, useRef } from 'react'
import { ReceiptButton, flagTagClass, ruleLabel, tierLabel, tierTagClass } from './shared'
import type { Item } from './shared'

const VERDICT: Record<Item['tier'], string> = {
  bad: 'A high-risk rule matched: credentials or a dangerous file name.',
  warn: 'Personal details or harsh language matched a rule.',
  ok: 'No rule matched.',
}

function what(it: Item) {
  if (it.status === 'blocked') return 'Stopped on this Mac. The message never reached the AI app.'
  if ((it.redactions?.length ?? 0) > 0) {
    return `Sent with ${it.redactions!.length === 1 ? '1 detail' : `${it.redactions!.length} details`} replaced.`
  }
  if (it.status === 'running') return 'Still running.'
  if (it.status === 'failed') return 'The call failed.'
  return it.verdict === 'observe' ? 'Sent unchanged (record-only mode).' : 'Sent.'
}

export function Sheet({ item, onClose, onOpenTimeline }: {
  item: Item
  onClose: () => void
  onOpenTimeline?: (id: string) => void
}) {
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    closeRef.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const { verb, ...record } = item
  return (
    <div className="sheet-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <aside className="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-h">
        <header className="sheet-head">
          <span className={`tag ${tierTagClass(item.tier)}`}>{tierLabel(item)}</span>
          <h2 id="sheet-h">{item.surface}</h2>
          <button ref={closeRef} className="btn ghost sm" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <p className="muted sheet-meta">
          {item.date} {item.ts} · {item.source === 'mcp' ? 'MCP tool call' : 'Chat message'}
          {item.chars != null && ` · ${item.chars.toLocaleString()} characters`}
        </p>
        <p className="sheet-line">{verb}</p>
        <div className={`banner ${item.tier === 'ok' ? 'info' : item.tier === 'bad' ? 'bad' : 'warn'}`}>
          <span><b>{what(item)}</b> {VERDICT[item.tier]}</span>
        </div>

        <h3 className="label">Rules matched</h3>
        {item.flags.length === 0
          ? <p className="muted">None.</p>
          : (
            <ul className="sheet-flags">
              {item.flags.map(f => (
                <li key={f.rule}>
                  <span className={`tag ${flagTagClass(f.tier)}`}>{ruleLabel(f.rule)}</span>
                  {f.tier === 'conduct' && <span className="muted"> conduct, not personal data</span>}
                </li>
              ))}
            </ul>
          )}

        <h3 className="label">Seal</h3>
        <p className="mono">{item.seal || 'Not sealed yet: the call is still running.'}</p>

        <div className="sheet-actions">
          {onOpenTimeline && (
            <button className="btn primary" onClick={() => onOpenTimeline(item.id)}>Open in Timeline</button>
          )}
          {item.seal && <ReceiptButton seal={item.seal} label="Download receipt" />}
        </div>

        <details className="sheet-raw">
          <summary>The record as stored</summary>
          <pre className="mono">{JSON.stringify(record, null, 1)}</pre>
        </details>
      </aside>
    </div>
  )
}
