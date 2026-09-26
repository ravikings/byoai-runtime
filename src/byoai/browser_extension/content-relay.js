/**
 * Coriqo Shield — page-world event relay (isolated world).
 *
 * The page-world capture (content.js, "world": "MAIN") can't call
 * chrome.runtime, and this isolated world can't see the page's fetch. The
 * CustomEvent bridge joins them: relay what the page capture emits into
 * capture rows for the service worker.
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
  // Nothing is forwarded until the user has agreed on the welcome page. The
  // background worker checks again; this keeps the page-world facts from even
  // leaving this script while capture is off.
  // null until the stored choice has been read. A send in those first moments
  // is held (a few at most) rather than lost, then kept or dropped once known.
  // Keep the version in step with CONSENT_VERSION in background.js.
  let consented = null
  const held = []
  const announce = () => chrome.runtime.sendMessage({ type: 'agent.pageWatched' }, () => void chrome.runtime.lastError)
  const settle = (value) => {
    consented = value?.version === 1
    if (consented) held.splice(0).forEach(send); else held.length = 0
  }
  try {
    chrome.storage.local.get('consent', (v) => { settle(v?.consent); announce() })
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== 'local' || !changes.consent) return
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
      forward(row)
    }
  }
  function disable() {
    disabled = true
    window.removeEventListener('shield-agent-capture', onCapture)
    document.removeEventListener('keydown', onKey, true)
    document.removeEventListener('click', onClick, true)
  }

  window.addEventListener('shield-agent-capture', onCapture)

  // Fallback for sends that don't go over fetch: watch the send control so a
  // websocket-only send is still recorded as data-free activity.
  let lastSend = 0
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
    if (now - lastSend < 1500) return // the fetch patch already recorded it
    lastSend = now
    const row = { kind: 'browser.chat.request', app: location.host, chars: null, wire: 'input' }
    forward(row)
  }

  document.addEventListener('keydown', onKey, true)
  document.addEventListener('click', onClick, true)
})()
