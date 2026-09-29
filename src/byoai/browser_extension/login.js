/**
 * Coriqo account sign-in for the extension — the login building block for
 * the "public self-serve" install path (`internal_doc/shield_sync_spec.md`,
 * "Public (self-serve) install: login required, desktop upsell").
 *
 * Deliberately NOT wired into `chrome.runtime.onInstalled` yet: today's
 * extension only supports pairing with a LOCAL Shield server
 * (`background.js`'s `isLocalEndpoint()` refuses anything else) — the
 * direct-to-Coriqo capture pipeline this login is *for* (S3 in the sync
 * spec: WebCrypto device key, JS redaction rules, direct sync) does not
 * exist yet. Gating the current, working, local-only flow behind a login
 * wall with no capture behind it would only add friction for zero benefit.
 * This page is reachable today as an optional, manual entry point (a link
 * in the popup's Details panel) so the auth half can be built, tested and
 * committed independently of S3's data-plane work.
 *
 * Talks directly to Coriqo's public (no-JWT) magic-link endpoints from the
 * extension's own origin — the same "CORS answers for our origin, no host
 * permission needed" approach already used for the local Shield pairing
 * check in `background.js`, and the one the sync spec's CORS spike already
 * covers for `/v1/shield/*`. `/api/v1/auth/*` needs the same allowance on
 * Coriqo's side; this file assumes it exists rather than working around its
 * absence.
 */
const API_BASE_KEY = 'coriqo_api_base'
const SESSION_KEY = 'coriqo_session'
// Dev default. Production base is configured once Coriqo's real host is
// decided — deliberately not hardcoded to a guessed production URL.
const DEFAULT_API_BASE = 'http://localhost:8000'

const el = (id) => document.getElementById(id)

function getApiBase() {
  return new Promise((resolve) =>
    chrome.storage.local.get(API_BASE_KEY, (v) => resolve(v[API_BASE_KEY] || DEFAULT_API_BASE)))
}

function getSession() {
  return new Promise((resolve) =>
    chrome.storage.local.get(SESSION_KEY, (v) => resolve(v[SESSION_KEY] || null)))
}

function setSession(session) {
  return new Promise((resolve) => chrome.storage.local.set({ [SESSION_KEY]: session }, resolve))
}

function clearSession() {
  return new Promise((resolve) => chrome.storage.local.remove(SESSION_KEY, resolve))
}

function showStep(name) {
  for (const s of ['request', 'consume', 'done']) {
    el(`step-${s}`).classList.toggle('active', s === name)
  }
}

function setMsg(id, text, kind) {
  const node = el(id)
  node.textContent = text || ''
  node.className = `msg${kind ? ` ${kind}` : ''}`
}

// The magic link's URL carries the raw token as `?magic_token=...` (see
// coriqo's `_magic_link_url`). Accept either the full pasted URL or a bare
// token, so "paste the link" and "paste just the code" both work — nobody
// reading this UI knows or should need to know which one they have.
function extractToken(pasted) {
  const trimmed = pasted.trim()
  try {
    const url = new URL(trimmed)
    const fromQuery = url.searchParams.get('magic_token')
    if (fromQuery) return fromQuery
  } catch {
    /* not a URL — treat the whole thing as the token */
  }
  return trimmed
}

let pendingEmail = ''
let pendingWorkspace = ''

async function init() {
  const session = await getSession()
  if (session) {
    el('done-email').textContent = session.email
    el('done-workspace').textContent = session.tenant_slug
    showStep('done')
    return
  }
  showStep('request')

  el('email').addEventListener('input', updateSendEnabled)
  el('workspace').addEventListener('input', updateSendEnabled)
  el('token').addEventListener('input', () => {
    el('consume').disabled = el('token').value.trim().length === 0
  })

  el('send').addEventListener('click', onSend)
  el('consume').addEventListener('click', onConsume)
  el('back').addEventListener('click', () => {
    setMsg('consume-msg', '')
    showStep('request')
  })
  el('sign-out').addEventListener('click', async () => {
    await clearSession()
    el('email').value = ''
    el('workspace').value = ''
    el('token').value = ''
    updateSendEnabled()
    showStep('request')
  })
  el('open-privacy').addEventListener('click', (ev) => {
    ev.preventDefault()
    chrome.tabs.create({ url: chrome.runtime.getURL('PRIVACY.md') })
  })
}

function updateSendEnabled() {
  el('send').disabled = !(el('email').value.includes('@') && el('workspace').value.trim())
}

async function onSend() {
  const email = el('email').value.trim()
  const workspace = el('workspace').value.trim()
  el('send').disabled = true
  setMsg('request-msg', 'Sending…')
  try {
    const base = await getApiBase()
    const res = await fetch(`${base}/api/v1/auth/magic-link`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ email, tenant_slug: workspace }),
    })
    // A response that arrived but said no is a different failure from the
    // request never reaching Coriqo at all — "check your connection" is the
    // wrong advice for a real server error, and retrying it immediately
    // just repeats a doomed request.
    if (!res.ok) throw new Error(`http:${res.status}`)
    // The endpoint is deliberately generic on success (anti-enumeration) —
    // this UI has nothing more specific to say either, and saying more
    // would contradict the one honest thing the backend promises.
    pendingEmail = email
    pendingWorkspace = workspace
    setMsg('request-msg', '')
    showStep('consume')
  } catch (err) {
    const httpStatus = /^http:(\d+)$/.exec(err?.message || '')?.[1]
    setMsg(
      'request-msg',
      httpStatus
        ? `Coriqo had a problem (${httpStatus}). Try again shortly.`
        : "Couldn't reach Coriqo. Check your connection and try again.",
      'bad',
    )
    el('send').disabled = false
  }
}

async function onConsume() {
  const token = extractToken(el('token').value)
  el('consume').disabled = true
  setMsg('consume-msg', 'Signing in…')
  try {
    const base = await getApiBase()
    const res = await fetch(`${base}/api/v1/auth/magic-link/consume`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ token, tenant_slug: pendingWorkspace }),
    })
    if (res.status === 404) throw new Error('invalid')
    if (res.status === 410) throw new Error('expired')
    if (!res.ok) throw new Error(`http:${res.status}`)
    const body = await res.json()
    await setSession({
      access_token: body.access_token,
      email: pendingEmail,
      tenant_slug: pendingWorkspace,
      at: Date.now(),
    })
    setMsg('consume-msg', '')
    el('done-email').textContent = pendingEmail
    el('done-workspace').textContent = pendingWorkspace
    showStep('done')
  } catch (err) {
    const httpStatus = /^http:(\d+)$/.exec(err?.message || '')?.[1]
    const msg = err?.message === 'expired'
      ? 'This sign-in link expired. Go back and send a new one.'
      : err?.message === 'invalid'
        ? "That link or code didn't match — check you copied all of it."
        : httpStatus
          ? `Coriqo had a problem (${httpStatus}). Try again shortly.`
          : "Couldn't reach Coriqo. Check your connection and try again."
    setMsg('consume-msg', msg, 'bad')
    el('consume').disabled = false
  }
}

init()
