/**
 * Coriqo Shield — browser capture, page world.
 *
 * Runs with "world": "MAIN", so this IS the same JavaScript world the page
 * uses: wrapping window.fetch here sees what the app sends without injection
 * into the DOM, which the surfaces' CSP blocks. It emits length-only facts
 * through a CustomEvent that the isolated-world relay (content-relay.js)
 * forwards to the service worker.
 *
 * Same trust stance as the desktop proxy and the MCP gateway: what happened,
 * not what was said. Message text is read to count characters and discarded.
 */
(function () {
  'use strict'

  if (window.__shieldCapturePatched) return
  window.__shieldCapturePatched = true

  const APP_FOR_HOST = {
    'claude.ai': 'claude',
    'chatgpt.com': 'chatgpt',
    'chat.openai.com': 'chatgpt',
    'gemini.google.com': 'gemini',
    'copilot.microsoft.com': 'copilot',
  }
  const app = APP_FOR_HOST[location.host]
  if (!app) return

  const CHAT_PATHS = [
    /\/chat_conversations\//, // claude.ai message send
    /\/backend-(api|anon)\/(f\/)?conversation(\?|$)/, // chatgpt.com message send (plain and /f/ paths)
    /\/v1\/messages/, // Messages-API agent wire
    /\/v1\/chat\/completions/,
  ]
  const orig = window.fetch
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || ''
    const path = url.replace(/^https?:\/\/[^/]+/, '')
    try {
      const isRequestObject = typeof Request !== 'undefined' && input instanceof Request
      if (((init && init.body && typeof init.body === 'string') || isRequestObject) &&
          CHAT_PATHS.some((p) => p.test(path))) {
        // A Request object carries its body as a stream; counting it would mean
        // consuming or cloning it, so those sends are noted without a length.
        let chars = isRequestObject && !(init && init.body) ? null : 0
        try {
          if (chars === null) throw new Error('length unknown')
          const body = JSON.parse(init.body)
          const last = (body.messages || [])[body.messages.length - 1]
          const text = (last && typeof last.content === 'string' && last.content) ||
            (last && last.content?.[0]?.text) || ''
          chars = typeof text === 'string' ? text.length : (init.body || '').length
        } catch { chars = chars === null ? null : (init.body || '').length }
        window.dispatchEvent(new CustomEvent('shield-agent-capture', {
          detail: { kind: 'browser.chat.request', app, chars, wire: path.slice(0, 60) },
        }))
        return orig.apply(this, arguments).then((res) => {
          window.dispatchEvent(new CustomEvent('shield-agent-capture', {
            detail: {
              kind: 'browser.chat.status', app, chars,
              ok: res.ok, status: res.status,
            },
          }))
          return res
        })
      }
    } catch { /* capture must never break the chat */ }
    return orig.apply(this, arguments)
  }
})()
