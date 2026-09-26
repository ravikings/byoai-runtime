/**
 * Enrol a Mac — mint a token, show it once, and list/revoke outstanding
 * tokens (Phase 1.5 console section).
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  fetchServerInfo,
  fetchTokens,
  mintToken,
  revokeToken,
  type ShieldServerToken,
  type ShieldServerTokenMinted,
} from '@/api/shieldServer'
import { useAuthWatch } from './AdminAuth'
import { ConfirmDialog } from '@/components/shield/shared'

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className="btn ghost sm"
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setCopied(true)
          window.setTimeout(() => setCopied(false), 2000)
        })
      }}
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

export function EnrolPanel() {
  const qc = useQueryClient()
  const infoQuery = useQuery({
    queryKey: ['shield-server', 'info'],
    queryFn: ({ signal }) => fetchServerInfo(signal),
  })
  useAuthWatch(infoQuery)

  const tokensQuery = useQuery({
    queryKey: ['shield-server', 'tokens'],
    queryFn: ({ signal }) => fetchTokens(signal),
  })
  useAuthWatch(tokensQuery)

  const [label, setLabel] = useState('')
  const [expiresIn, setExpiresIn] = useState<'7' | '30' | '90' | 'never'>('30')
  const [minted, setMinted] = useState<ShieldServerTokenMinted | null>(null)

  const mint = useMutation({
    mutationFn: () =>
      mintToken({
        label: label.trim(),
        expires_at: expiresIn === 'never' ? null : addDays(Number(expiresIn)),
      }),
    onSuccess: (data) => {
      setMinted(data)
      setLabel('')
      void qc.invalidateQueries({ queryKey: ['shield-server', 'tokens'] })
    },
  })

  const [confirming, setConfirming] = useState<ShieldServerToken | null>(null)
  const revoke = useMutation({
    mutationFn: (tokenId: string) => revokeToken(tokenId),
    onSuccess: () => {
      setConfirming(null)
      void qc.invalidateQueries({ queryKey: ['shield-server', 'tokens'] })
    },
  })

  const publicUrl = infoQuery.data?.public_url ?? null

  return (
    <section className="panel" aria-labelledby="shield-enrol-h">
      <header className="sec-head">
        <h2 className="label" id="shield-enrol-h">Enrol a Mac</h2>
      </header>

      <form
        className="coriqo-form"
        onSubmit={(e) => {
          e.preventDefault()
          if (label.trim()) mint.mutate()
        }}
      >
        <label>
          <span className="fkey">Label</span>
          <input
            value={label}
            placeholder="e.g. Alice's MacBook Pro"
            onChange={(e) => setLabel(e.target.value)}
          />
        </label>
        <label>
          <span className="fkey">Expires</span>
          <select value={expiresIn} onChange={(e) => setExpiresIn(e.target.value as typeof expiresIn)}>
            <option value="7">In 7 days</option>
            <option value="30">In 30 days</option>
            <option value="90">In 90 days</option>
            <option value="never">Never</option>
          </select>
        </label>
        <button className="btn primary" type="submit" disabled={mint.isPending || !label.trim()}>
          {mint.isPending ? 'Minting…' : 'Mint token'}
        </button>
      </form>
      {mint.isError && <p role="alert">Not minted: {mint.error.message}</p>}

      {minted && (
        <div className="banner ok" role="status" data-testid="minted-token">
          <div>
            <b>Token minted — shown once, not stored anywhere else.</b>
            <p className="mono" style={{ wordBreak: 'break-all' }}>{minted.token}</p>
            <p className="muted setting-help">
              On the Mac, open Shield's settings and paste the server address and this token:
            </p>
            <p className="mono">{publicUrl ?? 'http://127.0.0.1:17840'}</p>
          </div>
          <div className="row" style={{ gap: 'var(--s2)' }}>
            <CopyButton text={minted.token} />
            <CopyButton text={publicUrl ?? 'http://127.0.0.1:17840'} />
          </div>
        </div>
      )}

      <section className="settings-block" aria-labelledby="shield-tokens-h">
        <header className="sec-head">
          <h2 className="label" id="shield-tokens-h">Outstanding tokens</h2>
        </header>
        {tokensQuery.isPending && <p className="empty-row" role="status">Loading tokens…</p>}
        {tokensQuery.isError && (
          <p className="empty-row" role="alert">Couldn't load tokens: {tokensQuery.error.message}</p>
        )}
        {tokensQuery.data && (
          tokensQuery.data.tokens.length === 0 ? (
            <p className="muted">No tokens yet.</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Label</th>
                  <th>Created</th>
                  <th>Expires</th>
                  <th>Status</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {tokensQuery.data.tokens.map((t) => (
                  <tr key={t.token_id}>
                    <td>{t.label}</td>
                    <td>{t.created_at.slice(0, 10)}</td>
                    <td>{t.expires_at ? t.expires_at.slice(0, 10) : 'never'}</td>
                    <td>{t.revoked ? <span className="tag bad">revoked</span> : <span className="tag ok">active</span>}</td>
                    <td>
                      {!t.revoked && (
                        <button className="btn ghost sm" type="button" onClick={() => setConfirming(t)}>
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        )}
      </section>

      {confirming && (
        <ConfirmDialog
          title={`Revoke token "${confirming.label}"?`}
          confirmLabel={revoke.isPending ? 'Revoking…' : 'Revoke token'}
          busy={revoke.isPending}
          onCancel={() => setConfirming(null)}
          onConfirm={() => revoke.mutate(confirming.token_id)}
        >
          <p>Anyone who has not yet used this token to enrol will no longer be able to.</p>
          {revoke.isError && <p role="alert">{revoke.error.message}</p>}
        </ConfirmDialog>
      )}
    </section>
  )
}

function addDays(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString()
}
