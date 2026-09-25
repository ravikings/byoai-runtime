/**
 * Coriqo Shield — browser capture service worker.
 *
 * Buffers rows from the content scripts and ships them to the local Shield
 * server's /api/browser endpoint, which runs the SAME shared ruleset and
 * seals the interaction into the same Merkle chain as the desktop proxy and
 * the MCP gateway.
 *
 * MV3 kills the worker ~30s after it goes idle, and with it any timer it
 * scheduled and any in-memory state. That turns "queued for 50 rows"
 * silently into "lost on the next coffee break" — think a 4-hour server
 * outage while the worker restarted 400 times — a misleading failure mode.
 * Mitigation, being honest about blind spots: the queue and the fields of
 * a pending retry live in chrome.storage.session (cleared with the browser
 * session, ~RAM semantics), and chrome.alarms drives a re-flush so a
 * worker restart still delivers — within the same cap: if the server
 * stays off, old rows are dropped, never stored forever.
 */
const ENDPOINT_KEY = 'endpoint'

const DEFAULT_ENDPOINT = 'http://127.0.0.1:17831/api/browser'
// Earlier builds defaulted to :8300, which Consul and others also use. A saved
// copy of that old default is treated as "never customised" and follows the
// new default; any other saved endpoint is the user's choice and is kept.
const LEGACY_DEFAULT_ENDPOINT = 'http://127.0.0.1:8300/api/browser'
const resolveEndpoint = (saved) =>
  !saved || saved === LEGACY_DEFAULT_ENDPOINT ? DEFAULT_ENDPOINT : saved

const PIN_KEY = 'pinned_shield'
const LAST_CAPTURE_KEY = 'last_capture'
const WITNESS_KEY = 'witness'
const IDENTITY_PREFIX = 'byoai-shield-identity:'

const b64bytes = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))

/*
 * A port is not exclusive: another process on this Mac can listen on Shield's
 * address before Shield does and answer "ok" to anything. So before rows are
 * sent, Shield must prove it holds the device key this extension paired with:
 * it signs a fresh nonce, and the signature is checked against the public key
 * pinned on first contact. First contact is trust-on-first-use; after that, a
 * different server is refused until the user chooses to trust it.
 * Returns 'ok' | 'paired' | 'mismatch' | 'unverifiable' | 'unreachable'.
 */
async function checkServer(endpoint, { trustCurrent = false } = {}) {
  let identity
  try {
    const nonce = Array.from(crypto.getRandomValues(new Uint8Array(16)),
      (b) => b.toString(16).padStart(2, '0')).join('')
    const base = endpoint.replace(/\/api\/browser$/, '')
    const res = await fetch(`${base}/api/identity?nonce=${nonce}`, { cache: 'no-store' })
    if (res.status === 404) return { state: 'unverifiable' }
    if (!res.ok) return { state: 'unreachable' }
    identity = await res.json()
    const key = await crypto.subtle.importKey(
      'raw', b64bytes(identity.public_key), { name: 'Ed25519' }, false, ['verify'])
    const sig = b64bytes(String(identity.sig).replace(/^ed25519:/, ''))
    const signed = new TextEncoder().encode(IDENTITY_PREFIX + nonce)
    if (!(await crypto.subtle.verify({ name: 'Ed25519' }, key, sig, signed))) {
      return { state: 'mismatch' }
    }
  } catch (err) {
    return { state: err instanceof TypeError && !identity ? 'unreachable' : 'unverifiable' }
  }
  const saved = (await chrome.storage.local.get(PIN_KEY))[PIN_KEY]
  if (!saved || trustCurrent) {
    await chrome.storage.local.set({ [PIN_KEY]: identity.public_key })
    await chrome.storage.local.remove(WITNESS_KEY) // a different Shield has its own count
    return { state: 'paired', deviceId: identity.device_id }
  }
  return { state: saved === identity.public_key ? 'ok' : 'mismatch', deviceId: identity.device_id }
}

/*
 * Shield's record lives in files the user's own account can edit. The browser
 * profile is a second place, so the extension remembers the highest count of
 * sealed entries Shield has reported (a single number, never content). If
 * Shield later reports fewer, its record was deleted or rolled back, and the
 * popup says so. Re-pairing with a different Shield starts a fresh witness.
 * Returns true when the reported total is lower than what was seen before.
 */
async function noteWitness(total) {
  if (!Number.isInteger(total)) return false
  const seen = (await chrome.storage.local.get(WITNESS_KEY))[WITNESS_KEY]?.total ?? 0
  if (total < seen) return true
  if (total > seen) await chrome.storage.local.set({ [WITNESS_KEY]: { total } })
  return false
}

// Even length-only facts are personal data once they accumulate over time
// (a length timeline is still a usage pattern). A queue without a cap keeps
// them indefinitely when the server is off — days or weeks — which turns a
// temporary blind spot into a stored record nobody asked for. 50 rows ≈ the
// send rate of a normal day; anything older is dropped, not retried forever.
const MAX_QUEUED = 50

const QUEUE_KEY = 'queue'
const RETRY_AT_KEY = 'retry_at'

let queue = []
let timer = null
let restored = false

const WATCHED_HOST = ['claude.ai', 'chatgpt.com', 'chat.openai.com', 'gemini.google.com', 'copilot.microsoft.com']

/*
 * Badge on the Shield-covered surfaces: while a Shield page (claude.ai,
 * chatgpt.com) is open, the toolbar shows a check — "this page is watched".
 * Cheap, honest signal; per-tab state lives in the SW's memory.
 * Badge = flow of rows. Green "OK" = page watched; number = rows queued
 * (waiting on the local server); "-" = Shield offline (flush failed).
 */
const WATCHED_BADGE = 'OK'
const OFFLINE_BADGE = '-'

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === 'complete' && tab?.url && isPageWatched(tab.url)) {
    chrome.action.setBadgeText({ tabId, text: WATCHED_BADGE })
    chrome.action.setBadgeBackgroundColor({ tabId, color: '#16a34a' })
    chrome.action.setTitle({ tabId, title: 'Shield records on this page: app, time, message length. No message content.' })
  } else if (info.status === 'complete' && tab?.url && !isPageWatched(tab.url)) {
    chrome.action.setBadgeText({ tabId, text: '' })
    chrome.action.setTitle({ tabId, title: defaultTitle() })
  }
})

function isPageWatched(url) {
  try {
    const host = new URL(url).host
    return WATCHED_HOST.includes(host)
  } catch {
    return false
  }
}

function defaultTitle() {
  return 'Coriqo Shield - Agent Capture'
}

const WORKING_TITLE = 'Shield records on this page: app, time, message length. No message content.'

function setOffline() {
  chrome.action.setBadgeText({ text: OFFLINE_BADGE })
  chrome.action.setBadgeBackgroundColor({ color: '#dc2626' })
  chrome.action.setTitle({ title: 'Shield is offline on this Mac — start it with byoai-shield. Rows are being dropped after 50 queued.' })
}

function setRefused(why) {
  chrome.action.setBadgeText({ text: '!' })
  chrome.action.setBadgeBackgroundColor({ color: '#b42318' })
  chrome.action.setTitle({ title: why === 'shorter'
    ? "Shield's record is shorter than it was. Open this popup."
    : "The server on Shield's address isn't your Shield. Nothing is being sent. Open this popup." })
}

function setWorking() {
  chrome.action.setBadgeText({ text: WATCHED_BADGE })
  chrome.action.setBadgeBackgroundColor({ color: '#16a34a' })
  chrome.action.setTitle({ title: WORKING_TITLE })
}

function setOfflineIfQueued() {
  if (queue.length) setOffline()
  else setWorking()
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.action.setBadgeText({ text: '' })
})

// A worker that starts fresh (cold start, after termination, after browser
// restart) resumes from what it persisted — or, finding nothing, honestly
// reports the gap.
chrome.runtime.onStartup.addListener(restore)
restore()

async function restore() {
  if (restored) return
  restored = true
  const store = chrome.storage?.session
  if (!store) return // test harness or older Chrome; memory-only mode
  const data = await store.get([QUEUE_KEY, RETRY_AT_KEY])
  if (Array.isArray(data[QUEUE_KEY])) {
    queue = data[QUEUE_KEY].slice(-MAX_QUEUED)
  }
  const retry_at = data[RETRY_AT_KEY]
  if (retry_at && Date.now() < retry_at) {
    timer = setTimeout(flush, retry_at - Date.now())
  } else if (retry_at) {
    // A retry was due while the worker was dead — deliver now.
    flush()
  }
  setOfflineIfQueued()
}

async function persist() {
  const store = chrome.storage?.session
  if (!store) return
  const data = { [QUEUE_KEY]: queue }
  if (timer) data[RETRY_AT_KEY] = Date.now() + 10_000
  await store.set(data)
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg?.type === 'agent.capture' && msg.row) {
    // Only our own content scripts may file capture rows. Another extension
    // or a page can't reach this listener at all, but a compromised page's
    // relay path would — sender.id is our extension's own ID for any
    // listener entry from our content scripts, undefined otherwise.
    if (sender.id !== chrome.runtime.id) {
      reply?.({ error: 'not the Shield capture relay' })
      return false
    }
    noteCapture(sender.tab?.url)
    queue.push({ ...msg.row, sent_at: new Date().toISOString(), row_id: crypto.randomUUID() })
    if (queue.length > MAX_QUEUED) queue = queue.slice(-MAX_QUEUED)
    persist()
    scheduleFlush()
    reply?.({ queued: queue.length })
  } else if (msg?.type === 'agent.checkServer') {
    getEndpoint().then(async (endpoint) => {
      // Re-pairing is the user's decision, made in the popup. Anything coming
      // from a web page's tab (content scripts) can ask for a check, never
      // for a change of who is trusted.
      const r = await checkServer(endpoint, { trustCurrent: msg.trustCurrent === true && !sender.tab })
      reply?.({ ...r, endpoint })
    }).catch(() => reply?.({ state: 'unreachable' }))
    return true
  } else if (msg?.type === 'agent.getEndpoint') {
    chrome.storage.local.get(ENDPOINT_KEY, (v) =>
      reply?.({ endpoint: resolveEndpoint(v[ENDPOINT_KEY]) }))
  } else if (msg?.type === 'agent.setEndpoint') {
    // The batcher carries Shield metadata; redirecting it anywhere but the
    // local Shield server would exfiltrate that record off the machine.
    // Same local-only rule the server enforces on inbound requests.
    if (isLocalEndpoint(msg.endpoint)) {
      chrome.storage.local.set({ [ENDPOINT_KEY]: msg.endpoint }, () => reply?.({ ok: true }))
    } else {
      reply?.({ error: 'endpoint must be a localhost Shield server' })
    }
  }
  return true // async reply
})

// One timestamp per app, overwritten on every send: enough for the popup to
// say "last message noted 3 min ago" and to show when capture has gone quiet,
// and nothing that grows into a usage history.
async function noteCapture(url) {
  try {
    const host = new URL(url).host
    if (!WATCHED_HOST.includes(host)) return
    const seen = (await chrome.storage.local.get(LAST_CAPTURE_KEY))[LAST_CAPTURE_KEY] || {}
    await chrome.storage.local.set({ [LAST_CAPTURE_KEY]: { ...seen, [host]: Date.now() } })
  } catch { /* the badge and queue matter more than this note */ }
}

function isLocalEndpoint(candidate) {
  try {
    const url = new URL(String(candidate))
    return url.pathname.endsWith('/api/browser') &&
      (url.hostname === '127.0.0.1' || url.hostname === 'localhost' ||
        url.hostname === '[::1]' || url.hostname === '::1') &&
      (url.protocol === 'http:' || url.protocol === 'https:')
  } catch {
    return false
  }
}

function scheduleFlush() {
  setWorking()
  if (timer) return
  timer = setTimeout(flush, 2000)
  // The in-page timer dies with the worker; the alarm survives restarts and
  // re-flushes whatever the worker comes back to find (or missed).
  chrome.alarms?.create('shield-retry', { delayInMinutes: 1.1 })
  persist()
}

chrome.alarms?.onAlarm?.addListener((alarm) => {
  if (alarm.name === 'shield-retry') flush()
})

async function flush() {
  timer = null
  const batch = queue
  queue = []
  persist()
  if (!batch.length) { return }
  try {
    const endpoint = await getEndpoint()
    const check = await checkServer(endpoint)
    if (check.state !== 'ok' && check.state !== 'paired') throw new Error(check.state)
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ rows: batch }),
    })
    if (!res.ok) throw new Error(String(res.status))
    const reply = await res.json().catch(() => null)
    if (await noteWitness(reply?.sealed_total)) setRefused('shorter')
    else setWorking() // synced: green check
    chrome.alarms?.clear('shield-retry')
  } catch (err) {
    // Server not up yet: put the rows back, retry later — inside the same
    // cap, so an off-again server days out still means dropped rows, not
    // an ever-growing local record.
    queue = [...batch, ...queue].slice(-MAX_QUEUED)
    const refused = err?.message === 'mismatch' || err?.message === 'unverifiable'
    // A server that failed the identity check is not going to fix itself in
    // 10 s; leave it to the slow alarm instead of asking it again and again.
    if (!refused) timer = setTimeout(flush, 10000)
    chrome.alarms?.create('shield-retry', { delayInMinutes: 1.1 })
    if (refused) setRefused(); else setOffline()
    persist()
  }
}

function getEndpoint() {
  return new Promise((resolve) =>
    chrome.storage.local.get(ENDPOINT_KEY, (v) => resolve(resolveEndpoint(v[ENDPOINT_KEY]))))
}
