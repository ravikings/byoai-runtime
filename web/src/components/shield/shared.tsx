/**
 * Pieces every Shield tab uses: rule and app labels, the date filter, the
 * shared feed query, the notice strip and the receipt download button.
 */
import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { fetchFeed, fetchReceipt } from '@/api/shield'
import type { ShieldPolicy } from '@/api/shield'

export type Item = Awaited<ReturnType<typeof fetchFeed>>['items'][number]
export type Tier = Item['tier']

export const RULE_LABEL: Record<string, string> = {
  emails: 'email address', cards: 'card number', wallets: 'wallet address',
  phone_numbers: 'phone number', ssn_like: 'SSN-like number', api_keys: 'API key',
  password_said: 'credentials mentioned', executable_masquerade: 'dangerous file',
  credential_block: 'credentials in text', tool_intent: 'agent action intent',
  connector_tool_call: 'tool call', reply_pii_echo: 'personal data echoed in reply',
  reply_leak: 'secret echoed in reply', reply_toxic: 'harsh language in reply',
}

export const APP_NAME: Record<string, string> = {
  claude: 'Claude', chatgpt: 'ChatGPT', gemini: 'Gemini', copilot: 'Copilot',
}

export const ruleLabel = (rule: string) => RULE_LABEL[rule] ?? rule

export function flagTagClass(tier: string) {
  return tier === 'agent' ? 'info' : tier === 'high' ? 'bad' : 'warn'
}

export function tierLabel(it: Item) {
  if (it.status === 'blocked') return 'Stopped'
  return it.tier === 'bad' ? 'High risk' : it.tier === 'warn' ? 'Caught' : 'Clean'
}

export function tierTagClass(tier: Tier) {
  return tier === 'bad' ? 'bad' : tier === 'warn' ? 'warn' : 'ok'
}

/* ------------------------------------------------------------ date filter */

export type When = 'today' | '7' | '30' | 'all'
export const WHEN_OPTIONS: readonly (readonly [When, string])[] = [
  ['today', 'Today'], ['7', 'Last 7 days'], ['30', 'Last 30 days'], ['all', 'All time'],
]

function localDay(ms: number) {
  const d = new Date(ms)
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 10)
}

export const today = () => localDay(Date.now())

/** Whether an item's local date falls inside the chosen window. */
export function inWhen(date: string | undefined, when: When, now = Date.now()): boolean {
  if (when === 'all') return true
  const d = (date ?? '').slice(0, 10)
  const end = localDay(now)
  if (when === 'today') return d === end
  const start = localDay(now - (Number(when) - 1) * 86_400_000)
  return Boolean(d) && d >= start && d <= end
}

/* ------------------------------------------------------------------- feed */

/** The newest 200 interactions, shared by every tab (one fetch, one answer).
 * Filtering and paging happen on this list, so a filter never hides rows that
 * sit on a page the server didn't send. */
export function useFeed() {
  return useQuery({
    queryKey: ['shield-feed'],
    queryFn: () => fetchFeed({ page: 0, perPage: 200 }),
    refetchInterval: 3_000,
  })
}

/* ---------------------------------------------------------------- notice */

/** Always on while `notice` is set, so whoever uses the Mac can see what
 * Shield does without opening anything. Not dismissible by design. */
export function NoticeStrip({ policy }: { policy: ShieldPolicy }) {
  return (
    <div className="banner info shield-notice" role="status">
      <span>
        Shield checks what you send to AI apps from this Mac.{' '}
        {policy.mode === 'observe'
          ? 'Messages go out unchanged; it records which rules matched.'
          : 'Personal details are replaced before a message leaves.'}{' '}
        {policy.keep_text
          ? 'It keeps a short preview of each message with those details removed.'
          : 'It keeps the rule matches and message length, not your words.'}
      </span>
    </div>
  )
}

/* --------------------------------------------------------------- receipt */

export function ReceiptButton({ seal, label = 'Receipt ⤓' }: { seal: string; label?: string }) {
  const dl = useMutation({
    mutationFn: () => fetchReceipt(seal),
    onSuccess: receipt => {
      const blob = new Blob([JSON.stringify(receipt, null, 1)], { type: 'application/json' })
      downloadBlob(blob, `byoai-receipt-${seal}.json`)
    },
  })
  return (
    <>
      <button onClick={() => dl.mutate()} disabled={dl.isPending || !seal} className="btn ghost sm">
        {dl.isPending ? 'Preparing…' : label}
      </button>
      {dl.isError && <span className="tag bad" role="alert">No receipt: {dl.error.message}</span>}
    </>
  )
}

export function downloadBlob(blob: Blob, name: string) {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = name
  a.click()
  URL.revokeObjectURL(a.href)
}

export function WhenFilter({ value, onChange }: { value: When; onChange: (w: When) => void }) {
  return (
    <div className="group" role="group" aria-label="Date range">
      <span className="fkey">when</span>
      {WHEN_OPTIONS.map(([key, label]) => (
        <button key={key} className={`filter ${value === key ? 'on' : ''}`}
          aria-pressed={value === key} onClick={() => onChange(key)}>
          {label}
        </button>
      ))}
    </div>
  )
}

export type Need = 'all' | 'warn' | 'bad' | 'mcp'
export const NEED_OPTIONS: readonly (readonly [Need, string])[] = [
  ['all', 'All'], ['warn', 'Caught'], ['bad', 'Stopped or high risk'], ['mcp', 'Tool calls'],
]

export function matchesNeed(it: Item, need: Need) {
  if (need === 'warn') return it.tier === 'warn'
  if (need === 'bad') return it.tier === 'bad'
  if (need === 'mcp') return it.source === 'mcp'
  return true
}

export function NeedFilter({ value, onChange }: { value: Need; onChange: (n: Need) => void }) {
  return (
    <div className="group" role="group" aria-label="Show">
      <span className="fkey">show</span>
      {NEED_OPTIONS.map(([key, label]) => (
        <button key={key} className={`filter ${value === key ? 'on' : ''}`}
          aria-pressed={value === key} onClick={() => onChange(key)}>
          {label}
        </button>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------ primitives */

/** "1 record", "12 records". */
export function plural(n: number, one: string, many = `${one}s`) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`
}

/** Opens every section: a short title, then at most one line of help. The
 * accent tone marks the section the reader came to the page for. */
export function SectionHead({ id, title, help, accent, action }: {
  id: string
  title: string
  help?: ReactNode
  accent?: boolean
  action?: ReactNode
}) {
  return (
    <header className="sec-head">
      <div className="sec-title">
        <span className={`sec-dot ${accent ? 'accent' : ''}`} aria-hidden="true" />
        <h2 className="label" id={id}>{title}</h2>
        {action && <span className="sec-action">{action}</span>}
      </div>
      {help && <p className="sec-help">{help}</p>}
    </header>
  )
}

/** A yes/no question that needs a deliberate answer. Esc or the backdrop
 * cancels; focus starts on Cancel so Enter never confirms by accident. */
export function ConfirmDialog({ title, children, confirmLabel, onConfirm, onCancel, busy }: {
  title: string
  children: ReactNode
  confirmLabel: string
  onConfirm: () => void
  onCancel: () => void
  busy?: boolean
}) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    cancelRef.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])
  return (
    <div className="dialog-backdrop" onClick={e => { if (e.target === e.currentTarget) onCancel() }}>
      <div className="dialog" role="alertdialog" aria-modal="true"
        aria-labelledby="dialog-h" aria-describedby="dialog-body">
        <h2 id="dialog-h">{title}</h2>
        <div id="dialog-body" className="dialog-body">{children}</div>
        <div className="dialog-actions">
          <button ref={cancelRef} className="btn" onClick={onCancel}>Cancel</button>
          <button className="btn danger" onClick={onConfirm} disabled={busy}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  )
}
