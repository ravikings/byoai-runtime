/**
 * Coriqo Shield — page-world event relay (isolated world).
 *
 * The page-world capture (content.js, "world": "MAIN") can't call
 * chrome.runtime, and this isolated world can't see the page's fetch. The
 * CustomEvent bridge joins them: relay what the page capture emits into
 * capture rows for the service worker, and pass the user's choice and
 * Shield's mode the other way, so the page capture knows whether to replace
 * personal details. It runs at document_start, before the page's first send.
 *
 * After the extension is reloaded or updated, old content scripts keep
 * running in already-open tabs with no extension behind them ("extension
 * context invalidated"). Every chrome.runtime call here is guarded so that
 * state degrades to doing nothing and a page reload fixes it — it never
 * throws into the host page's console.
 */
(function () {
  'use strict'

  function send(row) {
    try {
      chrome.runtime.sendMessage({ type: 'agent.capture', row }, () => {
        void chrome.runtime.lastError // channel closed without a reply is fine
      })
    } catch {
      // Context invalidated (extension reloaded): stop touching chrome.*.
      disable()
    }
  }

  let disabled = false
  // The key/click fallback's state (see maybeSend); up here because
  // disable() can run before the rest of this script has.
  // Enter and a click within this long are one send.
  const ONE_SEND_MS = 1500
  // How long the fallback row waits for the page capture to report the same
  // send before it is filed. Generous, since a slow page may start its fetch
  // late; a websocket-only send is just noted a few seconds later.
  const FETCH_WAIT_MS = 5000
  let lastSend = 0
  let lastFetchSend = 0
  let pendingFallback = null
  // Nothing is forwarded until the user has agreed on the welcome page. The
  // background worker checks again; this keeps the page-world facts from even
  // leaving this script while capture is off.
  // null until the stored choice has been read. A send in those first moments
  // is held (a few at most) rather than lost, then kept or dropped once known.
  let consented = null
  const held = []
  const announce = () => chrome.runtime.sendMessage({ type: 'agent.pageWatched' }, () => void chrome.runtime.lastError)
  const settle = (value) => {
    consented = value?.version === globalThis.SHIELD_CONSENT_VERSION
    if (consented) held.splice(0).forEach(send); else held.length = 0
    tellPage()
  }

  /*
   * The page-world capture decides whether to replace personal details from
   * what it hears here: the user's choice and Shield's policy, which the
   * service worker saves under `shield_policy` only after the paired Shield
   * proved itself. A string, because objects don't cross between worlds.
   */
  let policy = null
  function tellPage() {
    window.dispatchEvent(new CustomEvent('shield-agent-config', {
      detail: JSON.stringify({ consented: consented === true, mode: policy?.mode, apps: policy?.apps ?? null }),
    }))
  }
  // content.js may start before or after this script; it asks once when it does.
  window.addEventListener('shield-agent-config-request', tellPage)

  try {
    chrome.storage.local.get(['consent', 'shield_policy'], (v) => {
      policy = v?.shield_policy ?? null
      settle(v?.consent)
      announce()
    })
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== 'local') return
      if (changes.shield_policy) { policy = changes.shield_policy.newValue ?? null; tellPage() }
      if (!changes.consent) return
      settle(changes.consent.newValue)
      announce() // so this tab's badge follows the choice without a reload
    })
  } catch { disable() }

  // Sent now if the choice is known to be yes, held (a few) while it is unknown.
  function forward(row) {
    if (consented === null) { if (held.length < 5) held.push(row) } else if (consented) send(row)
  }

  function onCapture(ev) {
    const row = ev.detail
    if (row && row.kind && !disabled && consented !== false) {
      if (row.kind === 'browser.chat.request') {
        // The page capture saw this send, so the key/click fallback for the
        // same send is not a second message.
        lastFetchSend = Date.now()
        clearTimeout(pendingFallback)
        pendingFallback = null
      }
      forward(row)
    }
  }
  function disable() {
    disabled = true
    window.removeEventListener('shield-agent-capture', onCapture)
    window.removeEventListener('shield-agent-config-request', tellPage)
    clearTimeout(pendingFallback)
    document.removeEventListener('keydown', onKey, true)
    document.removeEventListener('click', onClick, true)
  }

  window.addEventListener('shield-agent-capture', onCapture)

  // Fallback for sends that don't go over fetch: watch the send control so a
  // websocket-only send is still recorded as data-free activity. Enter or a
  // click comes before the app's fetch, so the fallback row waits (see
  // FETCH_WAIT_MS) and is dropped if the page capture records the same send.
  function onKey(ev) {
    if (ev.key === 'Enter' && ev.target &&
        ev.target.matches?.('textarea, [contenteditable="true"], input')) {
      maybeSend()
    }
  }
  function onClick(ev) {
    if (ev.target.closest?.('button[data-testid="send-button"], button[aria-label*="Send"]')) {
      maybeSend()
    }
  }
  function maybeSend() {
    if (disabled || consented === false) return
    const now = Date.now()
    if (now - lastSend < ONE_SEND_MS) return // Enter and the click for one send
    lastSend = now
    if (now - lastFetchSend < FETCH_WAIT_MS) return // the page capture already has it
    clearTimeout(pendingFallback)
    pendingFallback = setTimeout(() => {
      pendingFallback = null
      if (disabled || consented === false) return
      forward({ kind: 'browser.chat.request', app: location.host, chars: null, wire: 'input' })
    }, FETCH_WAIT_MS)
  }

  document.addEventListener('keydown', onKey, true)
  document.addEventListener('click', onClick, true)
})()
