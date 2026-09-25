/**
 * Trust: is Shield working, what did it catch, and what does it keep. The
 * activity list is the work; "What Shield keeps" sits in the rail beside it.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { fetchPrivacy, scrubStoredText } from '@/api/shield'
import type { ShieldPolicy, ShieldPrivacy, ShieldVerify } from '@/api/shield'
import {
  NeedFilter, WhenFilter, flagTagClass, inWhen, matchesNeed, ruleLabel, useFeed,
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
        <div className="status-card">
          <div className="left">
            <div className="headline">
              {!verify
                ? <span className="tag unknown">Checking…</span>
                : intact
                  ? <span className="tag ok">Shield is on</span>
                  : <span className="tag bad">Seal check failed</span>}
              <span className="protect-note">
                {counts.bad ? 'Something high-risk was stopped or flagged today.'
                  : counts.caught ? 'Personal details were caught today.'
                    : 'Nothing flagged today.'}
              </span>
            </div>
            <p className="seal-status">
              {verify
                ? intact
                  ? `Seal intact · ${verify.entries ?? 0} entries · root ${(verify.merkle_root ?? '').slice(0, 10)}…`
                  : `Seal broken at entry ${verify.broken_at ?? '?'}: ${verify.reason ?? 'mismatch'}`
                : 'Checking the seal…'}
            </p>
          </div>
          <div className="counts" aria-label="Today">
            <CountButton label="checked today" value={counts.checked} onClick={() => pick('all')} />
            <CountButton label="caught" value={counts.caught} tone="warn" onClick={() => pick('warn')} />
            <CountButton label="high risk" value={counts.bad} tone="bad" onClick={() => pick('bad')} />
            <CountButton label="tool calls" value={counts.mcp} onClick={() => pick('mcp')} />
          </div>
        </div>

        <section aria-labelledby="activity-h">
          <h3 className="label" id="activity-h">Activity</h3>
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
            {feed.isPending && <div className="empty-row">Loading activity…</div>}
            {feed.isError && (
              <div className="empty-row">
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

function CountButton({ label, value, tone, onClick }: {
  label: string; value: number; tone?: 'warn' | 'bad'; onClick: () => void
}) {
  return (
    <button className={`count ${tone ?? ''}`} onClick={onClick} title={`Show ${label}`}>
      <b className="t-num">{value}</b>
      <span>{label}</span>
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
          <span className={item.source === 'mcp' ? 'tag info' : 'tag unknown'}>
            {item.source === 'mcp' ? 'MCP' : 'Chat'}
          </span>
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
  const privacy = useQuery({ queryKey: ['shield-privacy'], queryFn: fetchPrivacy, refetchInterval: 15_000 })
  if (privacy.isError) {
    return <section className="panel"><p className="muted">Can't read what Shield keeps: Shield isn't running.</p></section>
  }
  if (!policy || !privacy.data) return <section className="panel"><p className="muted">Reading what Shield keeps…</p></section>
  const pv = privacy.data
  return (
    <section className="panel keeps" aria-labelledby="keeps-h">
      <div className="panel-head">
        <h3 className="label" id="keeps-h">What Shield keeps on this Mac</h3>
        <button className="btn ghost sm" onClick={goSettings}>Change</button>
      </div>
      <dl className="keeps-list stacked">
        <dt>Message text</dt>
        <dd>
          {policy.keep_text
            ? 'A preview of up to 200 characters, with personal details removed.'
            : 'Not kept. Shield stores the length and a fingerprint that only this Mac can recompute.'}
        </dd>
        <dt>Personal details</dt>
        <dd>{MODE_EFFECT[policy.mode]}</dd>
        <dt>History</dt>
        <dd>
          {pv.ledger_rows.toLocaleString()} records, kept for {policy.retention_days} days.
          {pv.rows_past_retention > 0 && (
            <> {pv.rows_past_retention.toLocaleString()} are older and go at the next cleanup or restart.</>
          )}
        </dd>
        <dt>Sent to Coriqo</dt>
        <dd>Only when you ship the seal: its root, entry count and signed checkpoint. No messages and no rule matches.</dd>
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
        {privacy.rows_with_text.toLocaleString()} older{' '}
        {privacy.rows_with_text === 1 ? 'record still holds' : 'records still hold'} message
        text from before these settings.
      </span>
      <button className="btn sm" disabled={scrub.isPending} onClick={() => scrub.mutate()}>
        {scrub.isPending ? 'Removing…' : 'Remove stored text'}
      </button>
      {scrub.isError && <span>Cleanup failed: {scrub.error.message}</span>}
    </div>
  )
}
