/**
 * Coriqo shield — consumer trust surface as a first-party console panel.
 * Same contract as the rest of the register screens: zod-validated numbers,
 * scope stated on every claim, the seal math as the visible product, and a
 * current button/panel skin identical to Fleet/Ledger tools. The copy carries
 * the de-AI pass (plain verbs, no hedges, no slogan tails).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { useState } from 'react'
import {
  fetchCoriqo,
  fetchFeed,
  fetchPolicy,
  fetchPrivacy,
  fetchReceipt,
  fetchVerify,
  publishSeal,
  RETENTION_DAYS,
  savePolicy,
  scrubStoredText,
} from '@/api/shield'
import type { ShieldPolicy, ShieldPrivacy, ShieldVerify } from '@/api/shield'

export const Route = createFileRoute('/console/$tenant/shield')({
  component: Shield,
})

type PageKey = 'trust' | 'ledger' | 'timeline' | 'settings'
const RULE_LABEL: Record<string, string> = {
  emails: 'email address', cards: 'card number', wallets: 'wallet address',
  phone_numbers: 'phone number', ssn_like: 'SSN-like number', api_keys: 'API key',
  password_said: 'credentials mentioned', executable_masquerade: 'dangerous file',
  credential_block: 'credentials in text', tool_intent: 'agent action intent',
  connector_tool_call: 'tool call', reply_pii_echo: 'PII echoed in reply',
  reply_leak: 'secret echoed in reply', reply_toxic: 'harsh language in reply',
}
const APP_NAME: Record<string, string> = {
  claude: 'Claude', chatgpt: 'ChatGPT', gemini: 'Gemini', copilot: 'Copilot',
}

function Shield() {
  const [page, setPage] = useState<PageKey>('trust')
  const verify = useQuery({
    queryKey: ['shield-verify'],
    queryFn: fetchVerify,
    refetchInterval: 10_000,
  })
  const policy = useQuery({ queryKey: ['shield-policy'], queryFn: fetchPolicy })
  const sealState: 'ok' | 'broken' | 'unknown'
    = verify.data ? (verify.data.tamper_evident ? 'ok' : 'broken') : 'unknown'
  return (
    <div className="shield-panel">
      <div className="panel-head">
        <div>
          <div className="title-row">
            <span
              className={`lead-signal ${sealOkClass(sealState)}`}
              title={sealState === 'ok' ? 'seal intact' : 'seal state'}
            />
            <h1>Coriqo shield</h1>
            <span className="tag info">per-Mac · local-first</span>
            {sealState === 'broken' && (
              <span className="tag bad">seal broken — re-check the sidecar</span>
            )}
          </div>
          <p className="one-liner">
            What happens between you and the AI you use — checked, sealed on
            your disk, and provable to anyone later.
          </p>
        </div>
        <div className="keys">
          {verify.data?.checkpoint?.device_id && (
            <>
              <span className="fkey">device</span>
              <span className="hash">{verify.data.checkpoint.device_id.slice(0, 18)}…</span>
            </>
          )}
        </div>
      </div>

      {policy.data?.notice && <NoticeStrip policy={policy.data} />}

      <nav className="shield-nav">
        {(
          [
            ['trust', 'Trust'],
            ['ledger', 'Ledger'],
            ['timeline', 'Timeline'],
            ['settings', 'Settings'],
          ] as const
        ).map(([key, label]) => (
          <button key={key} className={key === page ? 'on' : ''} onClick={() => setPage(key)}>
            {label}
          </button>
        ))}
      </nav>

      {page === 'trust' && (
        <Trust verify={verify.data} policy={policy.data}
          go={() => setPage('ledger')} goSettings={() => setPage('settings')} />
      )}
      {page === 'ledger' && <Ledger />}
      {page === 'timeline' && <Timeline />}
      {page === 'settings' && <Settings />}
    </div>
  )
}

function sealOkClass(state: 'ok' | 'broken' | 'unknown') {
  return state === 'ok' ? 'green' : state === 'broken' ? 'red' : 'grey'
}

/** Always on while `notice` is set: the person at the keyboard can see
 * what Shield does without opening anything. Not dismissible by design. */
function NoticeStrip({ policy }: { policy: ShieldPolicy }) {
  return (
    <div className="banner info shield-notice" role="status">
      <span>
        Shield checks what you send to AI apps from this Mac. It records which
        rules matched and how long each message was.{' '}
        {policy.keep_text
          ? 'It also keeps a short preview with personal details removed.'
          : 'It does not keep your words.'}
      </span>
    </div>
  )
}

const MODE_EFFECT: Record<ShieldPolicy['mode'], string> = {
  redact: 'Replaced with a label before the message leaves this Mac.',
  block: 'Replaced before sending. Messages carrying credentials or executable files are stopped here.',
  observe: 'Recorded as rule matches only. Messages are sent unchanged.',
}

/** "What Shield keeps": each line is read from the ledger and the saved
 * policy, so it states what is on disk now, not what we intend. */
function KeepsPanel({ policy, goSettings }: {
  policy: ShieldPolicy | undefined
  goSettings: () => void
}) {
  const privacy = useQuery({
    queryKey: ['shield-privacy'],
    queryFn: fetchPrivacy,
    refetchInterval: 15_000,
  })
  if (!policy || !privacy.data) {
    return privacy.isError
      ? <div className="panel"><p className="muted">Couldn't read what Shield keeps: the shield isn't running.</p></div>
      : null
  }
  const pv = privacy.data
  return (
    <section className="panel keeps" aria-labelledby="keeps-h">
      <div className="panel-head">
        <h3 className="label" id="keeps-h">What Shield keeps on this Mac</h3>
        <button className="btn ghost sm" onClick={goSettings}>Change</button>
      </div>
      <dl className="keeps-list">
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
        <dd>The seal's root, entry count and signed checkpoint. No messages and no rule matches.</dd>
      </dl>
      {pv.rows_with_text > 0 && <CleanupAlert privacy={pv} />}
    </section>
  )
}

/** Rows written before privacy-first still hold text; this removes it. */
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
      <span className="spacer" />
      <button className="btn sm" disabled={scrub.isPending} onClick={() => scrub.mutate()}>
        {scrub.isPending ? 'Removing…' : 'Remove stored text'}
      </button>
      {scrub.isError && <span>Cleanup failed: {scrub.error.message}</span>}
    </div>
  )
}

function Trust({ verify, policy, go, goSettings }: {
  verify: ShieldVerify | undefined
  policy: ShieldPolicy | undefined
  go: () => void
  goSettings: () => void
}) {
  const [need, setNeed] = useState<'all' | 'warn' | 'bad' | 'mcp'>('all')
  const [search, setSearch] = useState('')
  const [page, setPageNum] = useState(0)

  const feed = useQuery({
    queryKey: ['shield-feed-trust', page],
    queryFn: () => fetchFeed({ page, perPage: 20 }),
    refetchInterval: 3_000,
  })

  const items = (feed.data?.items ?? []).filter(it => {
    if (need === 'warn' && it.tier !== 'warn') return false
    if (need === 'bad' && it.tier !== 'bad') return false
    if (need === 'mcp' && it.source !== 'mcp') return false
    if (search) {
      const hay =
        `${it.verb} ${it.surface} ${it.reply ?? ''} ${it.flags.map(f => f.rule).join(' ')}`.toLowerCase()
      if (!hay.includes(search.toLowerCase())) return false
    }
    return true
  })

  const ok = verify?.tamper_evident && verify.checkpoint?.sig_valid !== false
  return (
    <>
      <div className="status-card">
        <div className="left">
          <div className="headline">
            {ok ? (
              <>
                <span className="tag ok">You're protected</span>
                <span className="protect-note">
                  {feed.data?.counts.high_risk
                    ? 'Something high-risk already stopped on your disk today'
                    : feed.data?.counts.caught
                      ? 'Personal details caught today'
                      : "Everything's quiet"}
                </span>
              </>
            ) : (
              'Capture is paused — start the sidecar to resume'
            )}
          </div>
          <p className="status-line">
            {feed.data?.counts.caught
              ? 'Coriqo inspected and flagged items today. Tap a row for the reason.'
              : 'Coriqo reads your ledger in real time and seals every check locally.'}
          </p>
          <p className="seal-status">
            {ok
              ? `Seal intact · ${verify?.entries ?? 0} entries · root ${(verify?.merkle_root ?? '').slice(0, 10)}…`
              : `Seal broken at entry ${verify?.broken_at} — ${verify?.reason ?? 'mismatch'}`}
          </p>
        </div>
        <div className="counts">
          <CountBox label="checked" value={feed.data?.counts.checked} />
          <CountBox label="caught" value={feed.data?.counts.caught} tone="warn" />
          <CountBox label="high risk" value={feed.data?.counts.high_risk} tone="bad" />
        </div>
      </div>

      <KeepsPanel policy={policy} goSettings={goSettings} />

      <div className="filterbar">
        <div className="group">
          <span className="fkey">show</span>
          {(
            [
              ['all', 'All'],
              ['warn', 'caught'],
              ['bad', 'stopped'],
              ['mcp', 'tool calls'],
            ] as const
          ).map(([key, label]) => (
            <button key={key} className={need === key ? 'on' : ''}
              onClick={() => { setNeed(key); setPageNum(0) }}>
              {label}
            </button>
          ))}
        </div>
        <input
          placeholder="Search what you or the agent did"
          value={search}
          onChange={e => { setSearch(e.target.value); setPageNum(0) }}
        />
      </div>

      <div className="rows">
        {feed.isPending && <div className="skeleton-row" />}
        {feed.isError && (
          <div className="empty-row dim">
            The capture ledger is unreachable — sidecar isn't running. Start it with{' '}
            <code className="hash">byoai-shield</code>.
          </div>
        )}
        {feed.data && items.length === 0 && (
          <div className="empty-row">
            Nothing under this filter. Your history keeps everything; this view only narrows.
          </div>
        )}
        {items.map(it => <Row key={it.id} item={it} />)}
        {feed.data && items.length > 0 && (
          <div className="pager">
            <button className="btn sm" disabled={page === 0}
              onClick={() => setPageNum(p => p - 1)}>‹ newer</button>
            <span className="page-stat">
              page {page + 1} of {Math.max(1, Math.ceil(feed.data.total / feed.data.limit))}
            </span>
            <button className="btn" disabled={!feed.data.has_more}
              onClick={() => setPageNum(p => p + 1)}>older›</button>
          </div>
        )}
      </div>

      <div className="cta-row">
        <p>
          What you pay for is the receipt itself —{' '}
          <button onClick={go}>open the Ledger →</button> and hand one out, one
          nobody can rewrite, including Coriqo.
        </p>
      </div>
    </>
  )
}

type Item = Awaited<ReturnType<typeof fetchFeed>>['items'][number]

function Row({ item }: { item: Item }) {
  return (
    <div className="row" data-tier={item.tier}>

      <span className="row-body">
        <span className="app-name">{item.surface}</span>
        <span className="what">{item.verb}</span>
        <span className="row-meta">
          <span className={item.source === 'mcp' ? 'tag info' : 'tag unknown'}>
            {item.source === 'mcp' ? 'MCP' : 'CHAT'}
          </span>
          {item.status === 'blocked' && <span className="tag bad">Stopped on this Mac</span>}
          {(item.redactions?.length ?? 0) > 0 && (
            <span className="tag ok">
              {item.redactions!.length === 1 ? '1 detail replaced' : `${item.redactions!.length} details replaced`}
            </span>
          )}
          {item.flags.map(f => (
            <span key={f.rule}
              className={`tag ${f.tier === 'agent' ? 'info' : f.tier === 'high' ? 'bad' : 'warn'}`}
              title={f.tier === 'conduct' ? 'Conduct flag, not personal data' : undefined}>
              {RULE_LABEL[f.rule] ?? f.rule}
            </span>
          ))}
          {item.seal && <span className="hash">{item.seal}</span>}
        </span>
        {item.reply && <span className="reply">{item.reply}</span>}
      </span>
      <span className="when">{item.ts}</span>
    </div>
  )
}

function Ledger() {
  const verify = useQuery({
    queryKey: ['shield-verify'],
    queryFn: fetchVerify,
    refetchInterval: 10_000,
  })
  const feed = useQuery({
    queryKey: ['shield-feed-ledger'],
    queryFn: () => fetchFeed({ page: 0, perPage: 200 }),
    refetchInterval: 5_000,
  })
  return (
    <>
      <div className="ledger-head">
        {verify.data?.tamper_evident ? (
          <>
            <span className="tag ok">Intact</span>
            <span className="hash">
              {verify.data.entries ?? 0} entries · root{' '}
              {(verify.data.merkle_root ?? '').slice(0, 12)}… · device{' '}
              {verify.data.checkpoint?.device_id ?? ''}
            </span>
          </>
        ) : (
          <>
            <span className="tag bad">
              Broken at entry {verify.data?.broken_at} — {verify.data?.reason}
            </span>
          </>
        )}
      </div>
      <p className="ledger-note">
        Each entry was sealed when Shield checked it. A seal covers what
        happened: the app, the verdict, the rules that matched, the message
        length and its fingerprint. It doesn't cover the words. Entries are
        folded into a Merkle tree and signed with this Mac's device key
        (Ed25519, file mode 0600, never leaves the machine). Change an entry
        later and its receipt stops checking out.
      </p>
      <table className="shield-table">
        <thead>
          <tr><th>When</th><th>Surface</th><th>Action</th><th>Tier</th><th>Seal</th><th /></tr>
        </thead>
        <tbody>
          {(feed.data?.items ?? []).map(it => (
            <tr key={it.id}>
              <td className="what">{it.date} {it.ts?.slice(0, 5)}</td>
              <td>{it.surface.split(' ·')[0]}</td>
              <td className="verb-cell">{it.verb}</td>
              <td>
                <span className={`tag ${it.tier === 'ok' ? 'ok' : it.tier === 'bad' ? 'bad' : 'warn'}`}>
                  {it.tier === 'bad' ? 'High risk' : it.tier === 'warn' ? 'Caught' : 'Clean'}
                </span>
              </td>
              <td><span className="hash">{it.seal.slice(0, 16)}</span></td>
              <td><ReceiptButton seal={it.seal} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      {feed.data && feed.data.items.length === 0 && (
        <p className="empty-row">
          No entries sealed yet — anything Coriqo checks appears here with its
          seal and a receipt download.
        </p>
      )}
    </>
  )
}

function ReceiptButton({ seal }: { seal: string }) {
  const dl = useMutation({
    mutationFn: () => fetchReceipt(seal),
    onSuccess: receipt => {
      const blob = new Blob([JSON.stringify(receipt, null, 1)], { type: 'application/json' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `byoai-receipt-${seal}.json`
      a.click()
      URL.revokeObjectURL(a.href)
    },
  })
  return (
    <>
      <button onClick={() => dl.mutate()} disabled={dl.isPending} className="btn ghost sm">
        {dl.isPending ? 'Preparing…' : 'Receipt ⤓'}
      </button>
      {dl.isError && <span className="tag bad" role="alert">No receipt: {dl.error.message}</span>}
    </>
  )
}

function Timeline() {
  const [picked, setPicked] = useState<string | null>(null)
  const feed = useQuery({
    queryKey: ['shield-feed-timeline'],
    queryFn: () => fetchFeed({ page: 0, perPage: 40 }),
    refetchInterval: 5_000,
  })
  const it = (feed.data?.items ?? []).find(x => x.id === picked) ?? feed.data?.items[0]
  if (!it) return <p className="empty-row">Waiting for an interaction to timeline.</p>
  const flags = it.flags ?? []
  return (
    <div className="timeline">
      <label className="timeline-pick">
        <span className="fkey">interaction</span>
        <select value={it.id} onChange={e => setPicked(e.target.value)}>
          {(feed.data?.items ?? []).map(x => (
            <option key={x.id} value={x.id}>{x.date} {x.ts} · {x.verb.slice(0, 60)}</option>
          ))}
        </select>
      </label>
      <ol>
        <li><span className="fkey">sent</span><p>{it.verb}</p></li>
        <li><span className="fkey">inspected</span>
          <p>{flags.length ? flags.map(f => RULE_LABEL[f.rule] ?? f.rule).join(' · ')
            : 'nothing flagged'}</p></li>
        {it.status !== 'running' && (
          <li><span className="fkey">sealed</span>
            <p>
              seal <span className="hash">{it.seal}</span> ·{' '}
              {((it.usage ?? {}) as { output_tokens?: number }).output_tokens ?? 0} tokens ·{' '}
              {Math.round(it.latency_ms ?? 0)} ms
            </p></li>
        )}
      </ol>
    </div>
  )
}

function Settings() {
  const qc = useQueryClient()
  const policy = useQuery({ queryKey: ['shield-policy'], queryFn: fetchPolicy })
  const info = useQuery({
    queryKey: ['shield-coriqo'],
    queryFn: fetchCoriqo,
    refetchInterval: 15_000,
  })
  const publish = useMutation({
    mutationFn: publishSeal,
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['shield-verify'] }),
  })
  const save = useMutation({
    mutationFn: (next: Partial<ShieldPolicy>) => savePolicy(next),
    onSuccess: data => {
      qc.setQueryData(['shield-policy'], data)
      void qc.invalidateQueries({ queryKey: ['shield-privacy'] })
    },
  })
  if (policy.isPending) return <p className="empty-row">Loading settings…</p>
  if (policy.isError) return <p className="empty-row">Shield unreachable.</p>
  const p = policy.data
  const ci = info.data
  const covered = new Set(p.covered_apps ?? Object.keys(p.apps))
  return (
    <>
      <PrivacySettings policy={p} save={next => save.mutate(next)} saving={save.isPending} />
      <section className="settings-block">
        <p className="fkey">What happens to personal details before a message leaves this Mac</p>
        {(
          [
            ['redact', 'Replace personal details', 'Emails, card numbers, phone numbers, SSNs, wallet addresses and API keys become labels such as [redacted-email].', true],
            ['block', 'Replace, and stop high-risk sends', 'Also stops messages that carry credentials or executable file names. The app sees an error.', false],
            ['observe', 'Record only', 'Messages go out unchanged. Shield records which rules matched.', false],
          ] as const
        ).map(([mode, title, note, recommended]) => (
          <label key={mode} className="mode-row">
            <input
              type="radio" name="shield-mode" checked={p.mode === mode}
              onChange={() => save.mutate({ mode })}
            />
            <b>
              {title}
              {recommended && <> <span className="tag ok">Recommended</span></>}
            </b>
            <span>{note}</span>
          </label>
        ))}
        </section>
      <div className="settings-block">
        <p className="fkey">apps on this Mac — governed means the capture proxy sees what the app does; off means pure pass-through</p>
        {Object.entries(p.apps).map(([app, on]) => covered.has(app) ? (
          <label key={app} className="row-app">
            <b>{APP_NAME[app] ?? app}</b>
            <input type="checkbox" checked={Boolean(on)}
              onChange={() => save.mutate({ apps: { ...p.apps, [app]: !on } })} />
          </label>
        ) : (
          <div key={app} className="row-app inert">
            <span>
              <b>{APP_NAME[app] ?? app}</b>
              <span className="muted setting-help">
                Not covered yet. Shield can't read this app's messages, so it can't check them.
              </span>
            </span>
            <span className="tag unknown">Not covered</span>
          </div>
        ))}
      </div>
      <div className="settings-block coriqo">
        <p className="fkey">Coriqo main app — publish this Mac's seal</p>
        <div className="coriqo-row">
          <button className="btn ghost" disabled={!ci?.configured || !ci?.app_url}
            onClick={() => window.open(ci?.app_url ?? '', '_blank')}>Open Coriqo →</button>
          <button className="btn" disabled={publish.isPending || !ci?.configured}
            onClick={() => publish.mutate()}>
            {publish.isPending ? 'Shipping…' : 'Ship seal now'}
          </button>
          <span className="fkey">{ci?.configured ? `connected · tenant ${ci?.tenant ?? ''}` : 'not configured'}</span>
        </div>
        {publish.isSuccess && (
          <p className="one-liner" role="status">
            Shipped: {publish.data.height ?? 0} entries, root {(publish.data.root ?? '').slice(0, 12)}…
          </p>
        )}
        {publish.isError && (
          <p className="one-liner" role="alert">Not shipped. {publish.error.message}</p>
        )}
        <p className="one-liner">
          Realtime org-wide sync is scheduled through the admin — from the Coriqo
          app, enroll this device.
        </p>
      </div>
      <p className="note">
        Changes reach the proxy within about two seconds. No restart needed.
        {save.isError ? ` Last change wasn't saved: ${save.error.message}` : ''}
      </p>
    </>
  )
}

/** Settings → Privacy: what Shield may keep, and for how long. */
function PrivacySettings({ policy, save, saving }: {
  policy: ShieldPolicy
  save: (next: Partial<ShieldPolicy>) => void
  saving: boolean
}) {
  const privacy = useQuery({ queryKey: ['shield-privacy'], queryFn: fetchPrivacy })
  const qc = useQueryClient()
  const scrub = useMutation({
    mutationFn: scrubStoredText,
    onSuccess: data => qc.setQueryData(['shield-privacy'], data),
  })
  const pv = privacy.data
  return (
    <section className="settings-block privacy-block" aria-labelledby="privacy-h">
      <p className="fkey" id="privacy-h">Privacy</p>

      <label className="row-app">
        <span>
          <b>Keep a preview of each message</b>
          <span className="muted setting-help">
            Off: Shield keeps the length and a fingerprint, never the words. On:
            it also keeps up to 200 characters with personal details removed,
            so you can see what was sent.
          </span>
        </span>
        <input type="checkbox" checked={policy.keep_text} disabled={saving}
          onChange={() => save({ keep_text: !policy.keep_text })} />
      </label>

      <label className="row-app">
        <span>
          <b>Keep history for</b>
          <span className="muted setting-help">
            Older records are deleted when Shield starts and when you remove stored text below.
            Sealed entries stay, so receipts keep working.
          </span>
        </span>
        <select value={policy.retention_days} disabled={saving}
          onChange={e => save({ retention_days: Number(e.target.value) })}>
          {RETENTION_DAYS.map(d => (
            <option key={d} value={d}>{d === 365 ? '1 year' : `${d} days`}</option>
          ))}
        </select>
      </label>

      <label className="row-app">
        <span>
          <b>Show the notice on this Mac</b>
          <span className="muted setting-help">
            The strip at the top of this page that tells whoever uses this Mac what Shield checks and keeps.
          </span>
        </span>
        <input type="checkbox" checked={policy.notice} disabled={saving}
          onChange={() => save({ notice: !policy.notice })} />
      </label>

      <div className="row-app">
        <span>
          <b>Remove stored text</b>
          <span className="muted setting-help">
            {pv
              ? pv.rows_with_text > 0
                ? `${pv.rows_with_text.toLocaleString()} records written before these settings still hold message text. This replaces it with the length and fingerprint, and deletes records older than ${policy.retention_days} days.`
                : `No stored records hold message text.${pv.rows_past_retention ? ` ${pv.rows_past_retention.toLocaleString()} are older than ${policy.retention_days} days and will be deleted.` : ''}`
              : 'Checking the ledger…'}
          </span>
          {pv && pv.sealed_with_text > 0 && (
            <span className="muted setting-help">
              {pv.sealed_with_text.toLocaleString()} sealed entries from before these settings
              include text with personal details removed. They are left as they are: changing a
              sealed entry is what the seal exists to detect.
            </span>
          )}
          {scrub.data && (
            <span className="setting-help" role="status">
              Removed text from {scrub.data.scrubbed.toLocaleString()} records and deleted{' '}
              {scrub.data.deleted.toLocaleString()} older ones.
            </span>
          )}
          {scrub.isError && <span className="setting-help">Cleanup failed: {scrub.error.message}</span>}
        </span>
        <button className="btn sm" onClick={() => scrub.mutate()}
          disabled={scrub.isPending || !pv || (pv.rows_with_text === 0 && pv.rows_past_retention === 0)}>
          {scrub.isPending ? 'Removing…' : 'Remove now'}
        </button>
      </div>
    </section>
  )
}

function CountBox({ label, value, tone }: {
  label: string
  value: number | undefined
  tone?: 'ok' | 'warn' | 'bad'
}) {
  return (
    <div className={`count ${tone ?? ''}`}>
      <b className="t-num">{value ?? '—'}</b>
      <span>{label}</span>
    </div>
  )
}
