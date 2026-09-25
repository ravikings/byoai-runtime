/**
 * Settings: what Shield may keep, what it does before a message leaves, which
 * apps it covers, and the optional link to a Coriqo account. The rail says
 * where every file lives, read from the running shield.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import {
  RETENTION_DAYS, fetchCoriqo, fetchInstalledApps, fetchPolicy, fetchPrivacy,
  publishSeal, saveCoriqo, savePolicy, scrubStoredText,
} from '@/api/shield'
import type { ShieldPolicy } from '@/api/shield'
import { APP_NAME } from './shared'

const MODES = [
  ['redact', 'Replace personal details', 'Emails, card numbers, phone numbers, SSNs, wallet addresses and API keys become labels such as [redacted-email].', true],
  ['block', 'Replace, and stop high-risk sends', 'Also stops messages that carry credentials or executable file names. The app sees an error.', false],
  ['observe', 'Record only', 'Messages go out unchanged. Shield records which rules matched.', false],
] as const

export function Settings() {
  const qc = useQueryClient()
  const policy = useQuery({ queryKey: ['shield-policy'], queryFn: fetchPolicy })
  const installed = useQuery({ queryKey: ['shield-apps'], queryFn: fetchInstalledApps, refetchInterval: 10_000 })
  const save = useMutation({
    mutationFn: (next: Partial<ShieldPolicy>) => savePolicy(next),
    onSuccess: data => {
      qc.setQueryData(['shield-policy'], data)
      void qc.invalidateQueries({ queryKey: ['shield-privacy'] })
    },
  })
  if (policy.isPending) return <p className="empty-row">Loading settings…</p>
  if (policy.isError) return <p className="empty-row">Can't reach Shield on this Mac.</p>
  const p = policy.data
  const covered = new Set(p.covered_apps ?? Object.keys(p.apps))

  return (
    <div className="shield-split">
      <div className="shield-main">
        <PrivacySettings policy={p} save={next => save.mutate(next)} saving={save.isPending} />

        <section className="settings-block" aria-labelledby="mode-h">
          <p className="fkey" id="mode-h">What happens to personal details before a message leaves this Mac</p>
          {MODES.map(([mode, title, note, recommended]) => (
            <label key={mode} className="mode-row">
              <input type="radio" name="shield-mode" checked={p.mode === mode}
                onChange={() => save.mutate({ mode })} />
              <b>{title}{recommended && <> <span className="tag ok">Recommended</span></>}</b>
              <span>{note}</span>
            </label>
          ))}
        </section>

        <section className="settings-block" aria-labelledby="apps-h">
          <p className="fkey" id="apps-h">
            AI apps on this Mac. On: Shield checks what the app sends. Off: its traffic passes through untouched and unrecorded.
          </p>
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
          {save.isError ? ` Last change wasn't saved: ${save.error.message}` : ''}
        </p>
      </div>
      <aside className="shield-rail">
        <DataLocation />
      </aside>
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
                ? `${pv.rows_with_text.toLocaleString()} records written before these settings still hold message text. This replaces it with the length and fingerprint, and deletes records older than ${policy.retention_days} days.`
                : `No stored records hold message text.${pv.rows_past_retention ? ` ${pv.rows_past_retention.toLocaleString()} are older than ${policy.retention_days} days and will be deleted.` : ''}`
              : 'Checking the ledger…'}
          </span>
          {pv && pv.sealed_with_text > 0 && (
            <span className="muted setting-help">
              {pv.sealed_with_text.toLocaleString()} sealed entries include text with personal
              details removed. They are left as they are: changing a sealed entry is what the
              seal exists to detect.
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

/** The optional link to a Coriqo account: save the connection, then ship the
 * seal. Shipping sends the root and signed checkpoint only. */
function CoriqoLink() {
  const qc = useQueryClient()
  const info = useQuery({ queryKey: ['shield-coriqo'], queryFn: fetchCoriqo, refetchInterval: 15_000 })
  const [form, setForm] = useState({ app_url: '', api_key: '', tenant: '' })
  const save = useMutation({
    mutationFn: () => saveCoriqo(form),
    onSuccess: data => {
      qc.setQueryData(['shield-coriqo'], data)
      setForm(f => ({ ...f, api_key: '' }))
    },
  })
  const publish = useMutation({
    mutationFn: publishSeal,
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['shield-verify'] }),
  })
  const ci = info.data
  return (
    <section className="settings-block coriqo" aria-labelledby="coriqo-h">
      <p className="fkey" id="coriqo-h">Coriqo account (optional)</p>
      <p className="muted setting-help">
        {ci?.configured
          ? `Connected to ${ci.app_url ?? 'Coriqo'}${ci.tenant ? `, tenant ${ci.tenant}` : ''}. Shipping sends the seal's root, entry count and signed checkpoint. No messages and no rule matches.`
          : 'Not connected. Shield works on its own; connecting lets you ship this Mac\'s seal to your Coriqo tenant so it sits with the rest of your evidence.'}
      </p>
      <form className="coriqo-form" onSubmit={e => { e.preventDefault(); save.mutate() }}>
        <label>
          <span className="fkey">App URL</span>
          <input value={form.app_url} placeholder={ci?.app_url ?? 'https://app.coriqo.com'}
            onChange={e => setForm({ ...form, app_url: e.target.value })} />
        </label>
        <label>
          <span className="fkey">API key</span>
          <input type="password" autoComplete="off" value={form.api_key}
            placeholder={ci?.configured ? 'Saved. Enter a new one to replace it.' : 'cq_sa_…'}
            onChange={e => setForm({ ...form, api_key: e.target.value })} />
        </label>
        <label>
          <span className="fkey">Tenant</span>
          <input value={form.tenant} placeholder={ci?.tenant ?? 'acme_bank'}
            onChange={e => setForm({ ...form, tenant: e.target.value })} />
        </label>
        <button className="btn" type="submit"
          disabled={save.isPending || !(form.app_url || form.api_key || form.tenant)}>
          {save.isPending ? 'Saving…' : 'Save connection'}
        </button>
      </form>
      {save.isError && <p className="setting-help" role="alert">Not saved: {save.error.message}</p>}
      <div className="coriqo-row">
        <button className="btn ghost" disabled={!ci?.configured || !ci?.app_url}
          onClick={() => window.open(ci?.app_url ?? '', '_blank', 'noopener')}>Open Coriqo →</button>
        <button className="btn primary" disabled={publish.isPending || !ci?.configured}
          onClick={() => publish.mutate()}>
          {publish.isPending ? 'Shipping…' : 'Ship seal now'}
        </button>
      </div>
      {!ci?.configured && (
        <p className="muted setting-help">Ship seal now turns on once a connection is saved.</p>
      )}
      {publish.isSuccess && (
        <p className="setting-help" role="status">
          Shipped: {publish.data.height ?? 0} entries, root {(publish.data.root ?? '').slice(0, 12)}…
        </p>
      )}
      {publish.isError && <p className="setting-help" role="alert">Not shipped. {publish.error.message}</p>}
      <p className="muted setting-help">
        For every Mac in your organisation to report on its own, your Coriqo admin enrols the
        device from Coriqo. This form covers this Mac only.
      </p>
    </section>
  )
}

function DataLocation() {
  const privacy = useQuery({ queryKey: ['shield-privacy'], queryFn: fetchPrivacy })
  const pv = privacy.data
  return (
    <section className="panel" aria-labelledby="where-h">
      <h3 className="label" id="where-h">Where your data lives</h3>
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
