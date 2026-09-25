/**
 * Coriqo Shield — browser capture service worker.
 *
 * Buffers rows from the content scripts and ships them to the local Shield
 * server's /api/browser endpoint, which runs the SAME shared ruleset and
 * seals the interaction into the same Merkle chain as the desktop proxy and
 * the MCP gateway. Offline and shut-downs are fine: unsent rows retry and
 * the ledger dedupes nothing — the row carries what happened, not what was
 * said, so a lost row is a blind spot, never a leak.
 */
const ENDPOINT_KEY = 'endpoint'

const DEFAULT_ENDPOINT = 'http://127.0.0.1:8300/api/browser'

// Even length-only facts are personal data once they accumulate over time
// (a length timeline is still a usage pattern). A queue without a cap keeps
// them indefinitely when the server is off — days or weeks — which turns a
// temporary blind spot into a stored record nobody asked for. 50 rows ≈ the
// send rate of a normal day; anything older is dropped, not retried forever.
const MAX_QUEUED = 50

let queue = []
let timer = null

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
    return host === 'claude.ai' || host === 'chatgpt.com' || host === 'chat.openai.com'
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

function setWorking() {
  chrome.action.setBadgeText({ text: WATCHED_BADGE })
  chrome.action.setBadgeBackgroundColor({ color: '#16a34a' })
  chrome.action.setTitle({ title: WORKING_TITLE })
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.action.setBadgeText({ text: '' })
})

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
    queue.push({ ...msg.row, sent_at: new Date().toISOString() })
    if (queue.length > MAX_QUEUED) queue = queue.slice(-MAX_QUEUED)
    scheduleFlush()
    reply?.({ queued: queue.length })
  } else if (msg?.type === 'agent.getEndpoint') {
    chrome.storage.local.get(ENDPOINT_KEY, (v) =>
      reply?.({ endpoint: v[ENDPOINT_KEY] || DEFAULT_ENDPOINT }))
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
}

async function flush() {
  timer = null
  const batch = queue
  queue = []
  if (!batch.length) { return }
  try {
    const endpoint = await getEndpoint()
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ rows: batch }),
    })
    if (!res.ok) throw new Error(String(res.status))
    setWorking() // synced: green check
  } catch {
    // Server not up yet: put the rows back, retry later — inside the same
    // cap, so an off-again server days out still means dropped rows, not
    // an ever-growing local record.
    queue = [...batch, ...queue].slice(-MAX_QUEUED)
    timer = setTimeout(flush, 10000)
    setOffline() // red dash: server unreachable
  }
}

function getEndpoint() {
  return new Promise((resolve) =>
    chrome.storage.local.get(ENDPOINT_KEY, (v) => resolve(v[ENDPOINT_KEY] || DEFAULT_ENDPOINT)))
}
