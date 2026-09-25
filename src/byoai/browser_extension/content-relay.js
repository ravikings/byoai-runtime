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
  function disable() {
    disabled = true
    window.removeEventListener('shield-agent-capture', onCapture)
    document.removeEventListener('keydown', onKey, true)
    document.removeEventListener('click', onClick, true)
  }

  window.addEventListener('shield-agent-capture', (ev) => {
    const row = ev.detail
    if (row && row.kind && !disabled) {
      send(row)
    }
  })

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
    if (disabled) return
    const now = Date.now()
    if (now - lastSend < 1500) return // the fetch patch already recorded it
    lastSend = now
    send({ kind: 'browser.chat.request', app: null, chars: null, wire: 'input' })
  }

  document.addEventListener('keydown', onKey, true)
  document.addEventListener('click', onClick, true)
})()
