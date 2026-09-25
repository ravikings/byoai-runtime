/**
 * Trust: is Shield working, what did it catch, and what does it keep. The
 * activity list is the work; "What Shield keeps" sits in the rail beside it.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { fetchPrivacy, savePolicy, scrubStoredText } from '@/api/shield'
import type { ShieldPolicy, ShieldPrivacy, ShieldVerify } from '@/api/shield'
import {
  NeedFilter, SectionHead, WhenFilter, flagTagClass, inWhen, matchesNeed, plural, ruleLabel, useFeed,
} from './shared'
import type { Item, Need, When } from './shared'

const PER_PAGE = 20

export function Trust({ verify, policy, onOpen, goSettings }: {
  verify: ShieldVerify | undefined
  policy: ShieldPolicy | undefined
  onOpen: (it: Item) => void
  goSettings: () => void
}) {
  const [need, setNeed] = useState<Need>('all')
  const [when, setWhen] = useState<When>('today')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const feed = useFeed()

  const all = feed.data?.items ?? []
  const todays = all.filter(it => inWhen(it.date, 'today'))
  const counts = {
    checked: todays.length,
    caught: todays.filter(it => it.tier === 'warn').length,
    bad: todays.filter(it => it.tier === 'bad').length,
    mcp: todays.filter(it => it.source === 'mcp').length,
    browser: todays.filter(it => it.source === 'browser').length,
  }
  const q = search.trim().toLowerCase()
  const visible = all.filter(it => matchesNeed(it, need) && inWhen(it.date, when) && (!q
    || `${it.verb} ${it.surface} ${it.reply ?? ''} ${it.flags.map(f => `${f.rule} ${ruleLabel(f.rule)}`).join(' ')}`
      .toLowerCase().includes(q)))
  const pages = Math.max(1, Math.ceil(visible.length / PER_PAGE))
  const cur = Math.min(page, pages - 1)
  const shown = visible.slice(cur * PER_PAGE, (cur + 1) * PER_PAGE)

  const pick = (n: Need) => { setNeed(n); setWhen('today'); setPage(0) }
  const intact = verify?.tamper_evident && verify.checkpoint?.sig_valid !== false
  return (
    <div className="shield-split">
      <div className="shield-main">
        <section className={`verdict ${!verify ? 'unknown' : intact ? 'ok' : 'bad'}`} aria-live="polite">
          <div className="state">
            {!verify ? 'Checking…' : intact ? 'Shield is on' : 'Seal check failed'}
          </div>
          <p className="reading">
            {counts.bad ? `${plural(counts.bad, 'message')} stopped or flagged high risk today.`
              : counts.caught ? `Personal details caught in ${plural(counts.caught, 'message')} today.`
                : 'Nothing flagged today.'}
          </p>
          <p className="hash">
            {verify
              ? intact
                ? `Seal intact · ${plural(verify.entries ?? 0, 'entry', 'entries')} · root ${(verify.merkle_root ?? '').slice(0, 10)}…`
                : `Broken at entry ${verify.broken_at ?? '?'}: ${verify.reason ?? 'mismatch'}`
              : 'Checking the seal…'}
          </p>
        </section>

        <div className="shield-stats" role="group" aria-label="Today, click to filter">
          <StatButton lead label="checked today" value={counts.checked} on={need === 'all'} onClick={() => pick('all')} />
          <StatButton label="caught" value={counts.caught} tone="warn" on={need === 'warn'} onClick={() => pick('warn')} />
          <StatButton label="high risk" value={counts.bad} tone="bad" on={need === 'bad'} onClick={() => pick('bad')} />
          <StatButton label="tool calls" value={counts.mcp} on={need === 'mcp'} onClick={() => pick('mcp')} />
          <StatButton label="in browser" value={counts.browser} on={need === 'browser'} onClick={() => pick('browser')} />
        </div>

        <section aria-labelledby="activity-h">
          <SectionHead id="activity-h" title="Activity" accent />
          <div className="filterbar">
            <NeedFilter value={need} onChange={n => { setNeed(n); setPage(0) }} />
            <WhenFilter value={when} onChange={w => { setWhen(w); setPage(0) }} />
            <input
              type="search" aria-label="Search activity"
              placeholder="Search app, rule or summary"
              value={search}
              onChange={e => { setSearch(e.target.value); setPage(0) }}
            />
          </div>
          <div className="rows">
            {feed.isPending && <div className="empty-row" role="status">Loading activity…</div>}
            {feed.isError && (
              <div className="empty-row" role="alert">
                Can't reach Shield on this Mac. Start it with <code className="mono">byoai-shield</code>.
              </div>
            )}
            {feed.data && all.length === 0 && (
              <div className="empty-row">
                Nothing recorded yet. Chat in Claude or ChatGPT with the capture proxy on, or call the MCP tools.
              </div>
            )}
            {feed.data && all.length > 0 && visible.length === 0 && (
              <div className="empty-row">
                Nothing matches these filters. Try All and All time; filters only change this view.
              </div>
            )}
            {shown.map(it => <Row key={it.id} item={it} onOpen={() => onOpen(it)} />)}
          </div>
          {visible.length > PER_PAGE && (
            <div className="pager">
              <button className="btn sm" disabled={cur === 0} onClick={() => setPage(cur - 1)}>‹ Newer</button>
              <span className="page-stat">Page {cur + 1} of {pages} · {visible.length} matching</span>
              <button className="btn sm" disabled={cur >= pages - 1} onClick={() => setPage(cur + 1)}>Older ›</button>
            </div>
          )}
        </section>
      </div>

      <aside className="shield-rail">
        <KeepsPanel policy={policy} goSettings={goSettings} />
      </aside>
    </div>
  )
}

function StatButton({ label, value, tone, lead, on, onClick }: {
  label: string; value: number; tone?: 'warn' | 'bad'; lead?: boolean; on: boolean; onClick: () => void
}) {
  return (
    <button className={`stat-btn ${tone ?? ''} ${lead ? 'lead' : ''} ${on ? 'on' : ''}`}
      aria-pressed={on} onClick={onClick}>
      <span className="value">{value.toLocaleString()}</span>
      <span className="unit">{label}</span>
    </button>
  )
}

export function Row({ item, onOpen }: { item: Item; onOpen: () => void }) {
  const n = item.redactions?.length ?? 0
  return (
    <button className="shield-row" data-tier={item.tier} onClick={onOpen}>
      <span className="row-body">
        <span className="app-name">{item.surface}</span>
        <span className="what">{item.verb}</span>
        <span className="row-meta">
          {item.source === 'mcp' && <span className="tag info">Tool call</span>}
          {item.source === 'browser' && <span className="tag info">In browser</span>}
          {item.status === 'blocked' && <span className="tag bad">Stopped on this Mac</span>}
          {n > 0 && <span className="tag ok">{n === 1 ? '1 detail replaced' : `${n} details replaced`}</span>}
          {item.flags.map(f => (
            <span key={f.rule} className={`tag ${flagTagClass(f.tier)}`}>{ruleLabel(f.rule)}</span>
          ))}
        </span>
        {item.reply && <span className="reply">{item.reply}</span>}
      </span>
      <span className="when">{item.ts}</span>
    </button>
  )
}

const MODE_EFFECT: Record<ShieldPolicy['mode'], string> = {
  redact: 'Replaced with a label before the message leaves this Mac.',
  block: 'Replaced before sending. Messages carrying credentials or executable files are stopped here.',
  observe: 'Recorded as rule matches only. Messages are sent unchanged.',
}

/** Each line is read from the ledger and the saved policy, so it states what
 * is on disk now, not what we intend. */
function KeepsPanel({ policy, goSettings }: {
  policy: ShieldPolicy | undefined
  goSettings: () => void
}) {
  const qc = useQueryClient()
  const privacy = useQuery({ queryKey: ['shield-privacy'], queryFn: fetchPrivacy, refetchInterval: 15_000 })
  // Undo only what is weaker than the default: Record only goes back to
  // redact, previews go off, and a stricter "block" stays. Tightening never
  // needs a confirmation.
  const restore = useMutation({
    mutationFn: () => savePolicy({
      keep_text: false,
      ...(policy?.mode === 'observe' ? { mode: 'redact' as const } : {}),
    }),
    onSuccess: data => {
      qc.setQueryData(['shield-policy'], data)
      void qc.invalidateQueries({ queryKey: ['shield-privacy'] })
    },
  })
  if (privacy.isError) {
    return <section className="panel"><p className="muted" role="alert">Can't read what Shield keeps: Shield isn't running.</p></section>
  }
  if (!policy || !privacy.data) {
    return <section className="panel"><p className="muted" role="status">Reading what Shield keeps…</p></section>
  }
  const pv = privacy.data
  const weaker = policy.mode === 'observe' || policy.keep_text
  return (
    <section className="panel keeps" aria-labelledby="keeps-h">
      <SectionHead id="keeps-h" title="What Shield keeps"
        action={<button className="btn ghost sm" onClick={goSettings}>Change</button>} />
      {weaker && (
        <div className="banner warn keeps-alert" role="status">
          <span><b>Less private than the default.</b>{' '}
            {[policy.mode === 'observe' && 'Messages go out unchanged.',
              policy.keep_text && 'Previews of messages are kept.'].filter(Boolean).join(' ')}</span>
          <button className="btn sm" disabled={restore.isPending} onClick={() => restore.mutate()}>
            {restore.isPending ? 'Restoring…' : 'Restore defaults'}
          </button>
        </div>
      )}
      <dl className="keeps-list stacked">
        <dt>Message text</dt>
        <dd>
          {policy.keep_text
            ? 'A preview of up to 200 characters, with personal details removed.'
            : 'Not kept. Shield stores the length and a fingerprint only this Mac can recompute.'}
        </dd>
        <dt>Personal details</dt>
        <dd>{MODE_EFFECT[policy.mode]}</dd>
        <dt>History</dt>
        <dd>
          {plural(pv.ledger_rows, 'record')}, kept for {policy.retention_days} days.
          {pv.rows_past_retention > 0 && (
            <> {pv.rows_past_retention.toLocaleString()} {pv.rows_past_retention === 1 ? 'is' : 'are'} older
              and go at the next cleanup or restart.</>
          )}
        </dd>
        <dt>Sent to Coriqo</dt>
        <dd>Only if you ship the seal: its root, entry count and signed checkpoint. No messages, no rule matches.</dd>
      </dl>
      {pv.rows_with_text > 0 && <CleanupAlert privacy={pv} />}
    </section>
  )
}

function CleanupAlert({ privacy }: { privacy: ShieldPrivacy }) {
  const qc = useQueryClient()
  const scrub = useMutation({
    mutationFn: scrubStoredText,
    onSuccess: data => qc.setQueryData(['shield-privacy'], data),
  })
  return (
    <div className="banner warn keeps-alert">
      <span>
        {plural(privacy.rows_with_text, 'older record')} still{' '}
        {privacy.rows_with_text === 1 ? 'holds' : 'hold'} message text from before these settings.
      </span>
      <button className="btn sm" disabled={scrub.isPending} onClick={() => scrub.mutate()}>
        {scrub.isPending ? 'Removing…' : 'Remove stored text'}
      </button>
      {scrub.isError && <span>Cleanup failed: {scrub.error.message}</span>}
    </div>
  )
}
