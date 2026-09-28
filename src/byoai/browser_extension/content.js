/**
 * Coriqo Shield — browser capture, page world.
 *
 * Runs with "world": "MAIN", so this IS the same JavaScript world the page
 * uses: wrapping window.fetch here sees what the app sends without injection
 * into the DOM, which the surfaces' CSP blocks. It emits facts through a
 * CustomEvent that the isolated-world relay (content-relay.js) forwards to
 * the service worker.
 *
 * Same trust stance as the desktop proxy and the MCP gateway: what happened,
 * not what was said. The message text is read here, in the page, for three
 * things only: its length, which of Shield's rules it matches (rule names,
 * never the matched text), and — when Shield's policy says redact or block —
 * replacing personal details before the message leaves the browser. The
 * text itself is never put in an event, stored or sent to Shield.
 *
 * The rules come from shield-rules.js (generated from the Python ones, so
 * the proxy and the extension can't disagree about what an email is).
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

  const RULES = window.__shieldRules || { pii: [], high: [], agent: [], redact: {}, covered_apps: [] }
  const compile = (list) => list.map(([name, source, flags]) =>
    ({ name, test: new RegExp(source, flags), all: new RegExp(source, flags + 'g') }))
  const PII = compile(RULES.pii)
  const HIGH = compile(RULES.high)
  const AGENT = compile(RULES.agent)
  // Gemini's web client sends form-encoded batch RPC and Copilot a websocket,
  // so only these apps' messages can be read and rewritten.
  const covered = RULES.covered_apps.includes(app)

  /*
   * What the relay tells us: whether the user agreed to capture, and Shield's
   * mode and per-app toggles (verified by the service worker against the
   * paired Shield). Until it arrives nothing is rewritten; with no policy
   * heard from Shield yet, the privacy-first default applies: redact.
   */
  let config = { consented: false, mode: 'redact', apps: null }
  window.addEventListener('shield-agent-config', (ev) => {
    try {
      const c = JSON.parse(ev.detail)
      config = {
        consented: c.consented === true,
        mode: ['observe', 'redact', 'block'].includes(c.mode) ? c.mode : 'redact',
        apps: c.apps && typeof c.apps === 'object' ? c.apps : null,
      }
    } catch { /* keep what we had */ }
  })
  window.dispatchEvent(new CustomEvent('shield-agent-config-request'))

  // Rewriting applies only where the user agreed, the app can be read, and
  // Shield governs it (an app turned off in Shield is left alone).
  const governing = () => config.consented && covered &&
    (config.apps === null || config.apps[app] === true)

  // --- the same logic as byoai.integrations.shield, in the same order ---

  function contentText(content) {
    if (typeof content === 'string') return content
    if (content && !Array.isArray(content) && typeof content === 'object' && Array.isArray(content.parts)) {
      return content.parts.filter((x) => typeof x === 'string').join('\n')
    }
    if (Array.isArray(content)) {
      return content
        .filter((b) => b && typeof b === 'object' && (b.type === 'text' || b.type === 'input_text'))
        .map((b) => String(b.text ?? '')).join('\n')
    }
    return ''
  }

  function messageText(body) {
    if (typeof body.prompt === 'string') return body.prompt
    const messages = body.messages
    if (Array.isArray(messages) && messages.length && messages[messages.length - 1] &&
        typeof messages[messages.length - 1] === 'object') {
      return contentText(messages[messages.length - 1].content)
    }
    const inp = body.input
    if (typeof inp === 'string') return inp
    if (Array.isArray(inp) && inp.length && inp[inp.length - 1] && typeof inp[inp.length - 1] === 'object') {
      return contentText(inp[inp.length - 1].content)
    }
    return ''
  }

  // Rule names a message matches, as "tier:rule" (request rules only).
  function flagsFor(text) {
    const out = []
    for (const [tier, rules] of [['high', HIGH], ['pii', PII], ['agent', AGENT]]) {
      for (const r of rules) if (r.test.test(text)) out.push(`${tier}:${r.name}`)
    }
    return out
  }

  function redactWithRules(text, hit) {
    for (const r of PII) {
      const token = RULES.redact[r.name]
      if (token && r.test.test(text)) {
        text = text.replace(r.all, () => token)
        hit.add(r.name)
      }
    }
    return text
  }

  // Only message fields are rewritten, as the proxy does: running the rules
  // over the whole wire body would also hit ids and timestamps. Returns the
  // body unchanged (same string) when nothing matched.
  function redactJsonBody(raw) {
    let body
    try { body = JSON.parse(raw) } catch { return { raw, rules: [] } }
    if (!body || typeof body !== 'object' || Array.isArray(body)) return { raw, rules: [] }
    const hit = new Set()
    const fix = (s) => redactWithRules(s, hit)
    const fixContent = (content) => {
      if (typeof content === 'string') return fix(content)
      if (content && !Array.isArray(content) && typeof content === 'object' && Array.isArray(content.parts)) {
        content.parts = content.parts.map((x) => (typeof x === 'string' ? fix(x) : x))
      } else if (Array.isArray(content)) {
        for (const block of content) {
          if (block && typeof block === 'object' && typeof block.text === 'string') block.text = fix(block.text)
        }
      }
      return content
    }
    for (const key of ['prompt', 'input']) {
      if (typeof body[key] === 'string') body[key] = fix(body[key])
    }
    const turns = [...(Array.isArray(body.messages) ? body.messages : []),
      ...(Array.isArray(body.input) ? body.input : [])]
    for (const msg of turns) {
      if (msg && typeof msg === 'object' && 'content' in msg) msg.content = fixContent(msg.content)
    }
    if (!hit.size) return { raw, rules: [] }
    return { raw: JSON.stringify(body), rules: [...hit].sort() }
  }

  // --- the send ---

  /*
   * Everything decided about one outgoing body: its length, the rules it
   * matches, the (possibly rewritten) body, and the verdict. `blocked` holds
   * the high-risk rules when block mode stops the send.
   */
  function inspect(raw) {
    let body = null
    try { body = JSON.parse(raw) } catch { /* not JSON: length only */ }
    const text = body && typeof body === 'object' && !Array.isArray(body) ? messageText(body) : ''
    const facts = { chars: text ? text.length : raw.length }
    if (!config.consented) return { facts, raw }
    facts.flags = text ? flagsFor(text) : []
    if (!governing()) return { facts, raw }
    const mode = config.mode
    const high = facts.flags.filter((f) => f.startsWith('high:')).map((f) => f.slice(5))
    if (high.length && mode === 'block') {
      return { facts: { ...facts, verdict: 'blocked' }, raw, blocked: high }
    }
    let out = raw
    let redactions = []
    if (mode === 'redact' || mode === 'block') ({ raw: out, rules: redactions } = redactJsonBody(raw))
    facts.redactions = redactions
    facts.verdict = redactions.length ? `redacted(${redactions.length})` : mode
    return { facts, raw: out }
  }

  function emit(detail) {
    window.dispatchEvent(new CustomEvent('shield-agent-capture', { detail }))
  }

  // What the app gets instead of a reply when block mode stops a send; the
  // same answer the desktop proxy gives.
  function refusal(rules) {
    return new Response(JSON.stringify({
      error: { type: 'coriqo_shield_blocked', message: 'Stopped on this Mac: ' + rules.join(', ') },
    }), { status: 403, headers: { 'content-type': 'application/json', 'x-coriqo-shield': 'blocked' } })
  }

  const CHAT_PATHS = [
    /\/chat_conversations\//, // claude.ai message send
    /\/backend-(api|anon)\/(f\/)?conversation(\?|$)/, // chatgpt.com message send (plain and /f/ paths)
    /\/v1\/messages/, // Messages-API agent wire
    /\/v1\/chat\/completions/,
  ]
  const orig = window.fetch

  // One send whose body is in hand as a string: inspect, maybe rewrite or
  // stop, record, then send.
  function send(self, input, init, raw, wire) {
    const { facts, raw: out, blocked } = inspect(raw)
    const base = { app, wire: wire.slice(0, 60) }
    emit({ kind: 'browser.chat.request', ...base, ...facts })
    if (blocked) return Promise.resolve(refusal(blocked))
    let args = [input, init]
    if (out !== raw) {
      args = init && typeof init.body === 'string'
        ? [input, { ...init, body: out }]
        : [new Request(input, { body: out }), init]
    }
    return orig.apply(self, args).then((res) => {
      emit({ kind: 'browser.chat.status', app, chars: facts.chars, ok: res.ok, status: res.status })
      return res
    })
  }

  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || ''
    const path = url.replace(/^https?:\/\/[^/]+/, '')
    try {
      const isRequestObject = typeof Request !== 'undefined' && input instanceof Request
      // Only sends: chat wires take a POST. A GET (a read), or a PUT/PATCH/DELETE
      // to a matching path (a rename, a delete), is not a message.
      const method = String((init && init.method) || (isRequestObject ? input.method : 'POST')).toUpperCase()
      if (method === 'POST' && CHAT_PATHS.some((p) => p.test(path))) {
        if (init && typeof init.body === 'string') return send(this, input, init, init.body, path)
        if (isRequestObject && !(init && init.body)) {
          // A Request carries its body as a stream. Read a copy, so the send
          // can still be inspected and, when Shield says so, rewritten.
          const self = this
          return input.clone().text().then(
            (raw) => send(self, input, init, raw, path),
            () => {
              emit({ kind: 'browser.chat.request', app, chars: null, wire: path.slice(0, 60) })
              return orig.call(self, input, init)
            })
        }
        // Any other body (FormData, Blob, a stream) can't be read without
        // consuming it: noted, not inspected or rewritten.
        emit({ kind: 'browser.chat.request', app, chars: null, wire: path.slice(0, 60) })
      }
    } catch { /* capture must never break the chat */ }
    return orig.apply(this, arguments)
  }
})()
