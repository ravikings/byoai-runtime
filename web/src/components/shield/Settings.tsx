/**
 * Settings: what Shield may keep, what it does before a message leaves, which
 * apps it covers, and the optional link to a Coriqo account. The rail says
 * where every file lives, read from the running shield.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import {
  RETENTION_DAYS, enrolWithCoriqo, fetchCoriqo, fetchInstalledApps, fetchPolicy, fetchPrivacy,
  publishSeal, savePolicy, scrubStoredText,
} from '@/api/shield'
import type { ShieldPolicy } from '@/api/shield'
import { APP_NAME, ConfirmDialog, SectionHead, plural } from './shared'

const MODES = [
  ['redact', 'Replace personal details', 'Emails, card numbers, phone numbers, SSNs, wallet addresses and API keys become labels such as [redacted-email].', true],
  ['block', 'Replace, and stop high-risk sends', 'Also stops messages that carry credentials or executable file names. The app sees an error.', false],
  ['observe', 'Record only', 'Messages go out unchanged. Shield records which rules matched.', false],
] as const

type Weaker = { change: Partial<ShieldPolicy>; kind: 'observe' | 'keep_text' }

export function Settings() {
  const qc = useQueryClient()
  const [asking, setAsking] = useState<Weaker | null>(null)
  const policy = useQuery({ queryKey: ['shield-policy'], queryFn: fetchPolicy })
  const installed = useQuery({ queryKey: ['shield-apps'], queryFn: fetchInstalledApps, refetchInterval: 10_000 })
  const save = useMutation({
    mutationFn: (next: Partial<ShieldPolicy> & { acknowledge?: 'less_private' }) => savePolicy(next),
    onSuccess: data => {
      qc.setQueryData(['shield-policy'], data)
      setAsking(null)
      void qc.invalidateQueries({ queryKey: ['shield-privacy'] })
    },
  })
  /** Changes that make Shield less private go through a confirmation first;
   * the server refuses them without the acknowledgement. */
  const change = (next: Partial<ShieldPolicy>) => {
    const p = policy.data
    if (p && next.mode === 'observe' && p.mode !== 'observe') return setAsking({ change: next, kind: 'observe' })
    if (p && next.keep_text && !p.keep_text) return setAsking({ change: next, kind: 'keep_text' })
    save.mutate(next)
  }
  if (policy.isPending) return <p className="empty-row" role="status">Loading settings…</p>
  if (policy.isError) return <p className="empty-row" role="alert">Can't reach Shield on this Mac.</p>
  const p = policy.data
  const covered = new Set(p.covered_apps ?? Object.keys(p.apps))

  return (
    <div className="shield-split">
      <div className="shield-main">
        <section className="settings-block" aria-labelledby="mode-h">
          <SectionHead id="mode-h" title="Before a message leaves" accent
            help="What Shield does to personal details in what you send." />
          {MODES.map(([mode, title, note, recommended]) => (
            <label key={mode} className="mode-row">
              <input type="radio" name="shield-mode" checked={p.mode === mode}
                onChange={() => change({ mode })} />
              <b>{title}{recommended && <> <span className="tag ok">Recommended</span></>}</b>
              <span>{note}</span>
            </label>
          ))}
        </section>

        <PrivacySettings policy={p} save={change} saving={save.isPending} />

        <section className="settings-block" aria-labelledby="apps-h">
          <SectionHead id="apps-h" title="Apps"
            help="On: Shield checks what the app sends. Off: its traffic passes through, unchecked and unrecorded." />
          {Object.entries(p.apps).map(([app, on]) => {
            const where = installed.data
              ? installed.data[app] ? 'Installed on this Mac.' : 'Not found in Applications; the web version is still covered.'
              : ''
            return covered.has(app) ? (
              <label key={app} className="row-app">
                <span>
                  <b>{APP_NAME[app] ?? app}</b>
                  {where && <span className="muted setting-help">{where}</span>}
                </span>
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
            )
          })}
        </section>

        <CoriqoLink />

        <p className="note">
          Changes reach the proxy within about two seconds. No restart needed.
          {save.isError && !asking ? ` Last change wasn't saved: ${save.error.message}` : ''}
        </p>
      </div>
      <aside className="shield-rail">
        <DataLocation />
      </aside>

      {asking && (
        <ConfirmDialog
          title={asking.kind === 'observe' ? 'Send messages unchanged?' : 'Keep message previews?'}
          confirmLabel={asking.kind === 'observe' ? 'Send unchanged' : 'Keep previews'}
          busy={save.isPending}
          onCancel={() => { setAsking(null); save.reset() }}
          onConfirm={() => save.mutate({ ...asking.change, acknowledge: 'less_private' })}>
          {asking.kind === 'observe'
            ? <p>Emails, card numbers, phone numbers and keys will reach the AI app as typed. Shield
              will only record which rules matched. The notice on this Mac will say so.</p>
            : <p>Shield will store up to 200 characters of each message on this Mac, with personal
              details removed. The rest of what you type still isn't kept.</p>}
          {save.isError && <p role="alert">Not saved: {save.error.message}</p>}
        </ConfirmDialog>
      )}
    </div>
  )
}

function PrivacySettings({ policy, save, saving }: {
  policy: ShieldPolicy
  save: (next: Partial<ShieldPolicy>) => void
  saving: boolean
}) {
  const qc = useQueryClient()
  const privacy = useQuery({ queryKey: ['shield-privacy'], queryFn: fetchPrivacy })
  const scrub = useMutation({
    mutationFn: scrubStoredText,
    onSuccess: data => qc.setQueryData(['shield-privacy'], data),
  })
  const pv = privacy.data
  return (
    <section className="settings-block privacy-block" aria-labelledby="privacy-h">
      <SectionHead id="privacy-h" title="What Shield keeps" />

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
            The strip at the top of Shield that tells whoever uses this Mac what Shield checks and keeps.
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
                ? `${plural(pv.rows_with_text, 'record')} written before these settings still ${pv.rows_with_text === 1 ? 'holds' : 'hold'} message text. This replaces it with the length and fingerprint, and deletes records older than ${policy.retention_days} days.`
                : `No stored records hold message text.${pv.rows_past_retention ? ` ${pv.rows_past_retention.toLocaleString()} are older than ${policy.retention_days} days and will be deleted.` : ''}`
              : 'Checking the ledger…'}
          </span>
          {pv && pv.sealed_with_text > 0 && (
            <span className="muted setting-help">
              {plural(pv.sealed_with_text, 'sealed entry', 'sealed entries')} include text with personal
              details removed. They are left as they are: changing a sealed entry is what the
              seal exists to detect.
            </span>
          )}
          {scrub.data && (
            <span className="setting-help" role="status">
              Removed text from {plural(scrub.data.scrubbed, 'record')} and deleted{' '}
              {plural(scrub.data.deleted, 'older record')}.
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

function ago(epochSeconds: number) {
  const mins = Math.max(0, Math.round((Date.now() / 1000 - epochSeconds) / 60))
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins} min ago`
  const h = Math.round(mins / 60)
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`
}

function whenNext(epochSeconds: number) {
  const mins = Math.round((epochSeconds - Date.now() / 1000) / 60)
  if (mins <= 1) return 'within a minute'
  if (mins < 60) return `in about ${mins} min`
  return `in about ${Math.round(mins / 60)} h`
}

/** The link to a Coriqo tenant. Set up once with an enrolment token; after
 * that it sends this Mac's signed seal on its own, rarely, and retries by
 * itself. The only state that asks for a person is Coriqo refusing the Mac. */
function CoriqoLink() {
  const qc = useQueryClient()
  const info = useQuery({ queryKey: ['shield-coriqo'], queryFn: fetchCoriqo, refetchInterval: 30_000 })
  const [form, setForm] = useState({ base_url: '', token: '' })
  const [reconnect, setReconnect] = useState(false)
  const enrol = useMutation({
    mutationFn: () => enrolWithCoriqo(form),
    onSuccess: () => {
      setForm({ base_url: '', token: '' })
      setReconnect(false)
      void qc.invalidateQueries({ queryKey: ['shield-coriqo'] })
    },
  })
  const send = useMutation({
    mutationFn: publishSeal,
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['shield-coriqo'] }),
  })
  const ci = info.data
  const showForm = !ci?.connected || reconnect || ci.needs_attention

  return (
    <section className="settings-block coriqo" aria-labelledby="coriqo-h">
      <SectionHead id="coriqo-h" title="Coriqo" help="Optional. Sends this Mac's seal to your Coriqo tenant so it sits with the rest of your AI evidence." />

      {info.isPending && <p className="muted setting-help" role="status">Checking the connection…</p>}

      {ci?.connected && (
        <dl className="keeps-list stacked coriqo-status">
          <dt>Connected to</dt>
          <dd>{ci.tenant ?? 'your tenant'} · <span className="mono">{ci.base_url}</span></dd>
          <dt>Last sent</dt>
          <dd>
            {ci.last_sent_at
              ? `${ago(ci.last_sent_at)}, covering ${plural(ci.last_height, 'entry', 'entries')}`
              : 'Not yet. The first send happens within a minute.'}
          </dd>
          <dt>Next</dt>
          <dd>
            {!ci.has_new
              ? `Nothing new to send. Shield sends at most every ${ci.every_hours} hours, and only when there's something new.`
              : ci.next_attempt_at
                ? `New activity, sending ${whenNext(ci.next_attempt_at)}.`
                : 'New activity, sending within a minute.'}
          </dd>
        </dl>
      )}

      {ci?.last_error && (
        <div className={`banner ${ci.needs_attention ? 'bad' : 'warn'}`} role={ci.needs_attention ? 'alert' : 'status'}>
          <span>{ci.last_error}</span>
        </div>
      )}

      {ci?.connected && !showForm && (
        <div className="coriqo-row">
          <button className="btn" disabled={send.isPending || !ci.has_new}
            onClick={() => send.mutate()}>
            {send.isPending ? 'Sending…' : 'Send now'}
          </button>
          <button className="btn ghost" onClick={() => window.open(ci.base_url ?? '', '_blank', 'noopener')}>
            Open Coriqo →
          </button>
          <button className="btn ghost sm" onClick={() => setReconnect(true)}>Connect again</button>
        </div>
      )}
      {send.isError && <p className="setting-help" role="alert">Not sent: {send.error.message}</p>}

      {showForm && (
        <>
          <p className="muted setting-help">
            In Coriqo, an admin creates an enrolment token for this Mac. Paste it below with your
            Coriqo address. The token works once; after that this Mac identifies itself with its
            own key, so there's no password or API key to keep.
          </p>
          <form className="coriqo-form enrol" onSubmit={e => { e.preventDefault(); enrol.mutate() }}>
            <label>
              <span className="fkey">Coriqo address</span>
              <input value={form.base_url} placeholder={ci?.base_url ?? 'https://app.coriqo.com'}
                inputMode="url" autoComplete="url"
                onChange={e => setForm({ ...form, base_url: e.target.value })} />
            </label>
            <label>
              <span className="fkey">Enrolment token</span>
              <input type="password" autoComplete="off" value={form.token} placeholder="cik_live_…"
                onChange={e => setForm({ ...form, token: e.target.value })} />
            </label>
            <button className="btn primary" type="submit"
              disabled={enrol.isPending || !form.base_url.trim() || !form.token.trim()}>
              {enrol.isPending ? 'Connecting…' : 'Connect this Mac'}
            </button>
          </form>
          {enrol.isError && <p className="setting-help" role="alert">Not connected: {enrol.error.message}</p>}
          {reconnect && <button className="btn ghost sm" onClick={() => setReconnect(false)}>Cancel</button>}
        </>
      )}

      <p className="muted setting-help">
        What leaves this Mac: the seal's root, its entry count and the signature. No messages and no rule matches.
      </p>
    </section>
  )
}

function DataLocation() {
  const privacy = useQuery({ queryKey: ['shield-privacy'], queryFn: fetchPrivacy })
  const pv = privacy.data
  return (
    <section className="panel" aria-labelledby="where-h">
      <SectionHead id="where-h" title="Where your data lives" />
      <dl className="keeps-list stacked">
        <dt>Records</dt>
        <dd className="mono">{pv?.ledger_path ?? '…'}</dd>
        <dt>Seal chain</dt>
        <dd className="mono">{pv?.seal_path ?? '…'}</dd>
        <dt>Device key</dt>
        <dd>
          <span className="mono">{pv?.device_id ?? 'created on first seal'}</span>
          {' · '}Ed25519 private key, file mode 0600. It never leaves this Mac.
        </dd>
        <dt>Leaves this Mac</dt>
        <dd>Nothing, unless you ship the seal to Coriqo. No account, no cloud copy, no sync by default.</dd>
      </dl>
    </section>
  )
}
