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

  const RULES = window.__shieldRules ||
    { secret: [], pii: [], flag: [], validators: {}, placeholder: {}, rule_actions: {}, default_actions: {}, covered_apps: [] }
  const compile = (list) => (list || []).map(([name, source, flags]) =>
    ({ name, all: new RegExp(source, flags.replace('g', '') + 'g') }))
  const TIERS = [['secret', compile(RULES.secret)], ['pii', compile(RULES.pii)], ['flag', compile(RULES.flag)]]
  // Gemini's web client sends form-encoded batch RPC and Copilot a websocket,
  // so only these apps' messages can be read and rewritten.
  const covered = (RULES.covered_apps || []).includes(app)

  /*
   * What the relay tells us: whether the user agreed to capture, and Shield's
   * mode and per-app toggles (verified by the service worker against the
   * paired Shield). Until it arrives nothing is rewritten; with no policy
   * heard from Shield yet, the privacy-first default applies: redact.
   */
  const SCAN_MAX = 2_000_000
  const ORDER = { log: 0, redact: 1, warn: 2, block: 3 }
  const isAction = (a) => typeof a === 'string' && Object.prototype.hasOwnProperty.call(ORDER, a)
  // Only valid actions survive; a missing or invalid one falls back to the default.
  const cleanActions = (a) => {
    const out = {}
    for (const tier of ['secret', 'pii', 'flag']) if (a && isAction(a[tier])) out[tier] = a[tier]
    return out
  }
  let config = { consented: false, mode: 'redact', apps: null, actions: {} }
  /*
   * Everything from the relay (config, warn answers) arrives on one
   * MessageChannel port that the relay transfers in a single event at
   * document_start. This script is listed first, so its listener is in place
   * before the relay posts and before any page script runs. The first port
   * wins and the event is hidden from page listeners; no window CustomEvent
   * can set the policy or answer a warn prompt, so a page can't forge either.
   */
  const PORT_MARK = 'shield-agent-port'
  let port = null
  function onPortMessage(ev) {
    const c = ev && ev.data
    if (!c || typeof c !== 'object') return
    if (c.t === 'config') {
      config = {
        consented: c.consented === true,
        mode: ['observe', 'redact', 'block'].includes(c.mode) ? c.mode : 'redact',
        apps: c.apps && typeof c.apps === 'object' ? c.apps : null,
        actions: cleanActions(c.actions),
      }
    } else if (c.t === 'warn-result') {
      answered(c.send_id, c.choice)
    } else if (c.t === 'relay-closed') {
      relayGone()
    }
  }
  window.addEventListener('message', (ev) => {
    if (!ev || ev.source !== window || ev.data !== PORT_MARK) return
    ev.stopImmediatePropagation() // page listeners never see the handover, or a later imitation
    if (port || !ev.ports || ev.ports.length !== 1) return
    port = ev.ports[0]
    port.onmessage = onPortMessage
  }, true)

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

  // A regex hit counts only if the rule's named validator passes on the match.
  function luhn(m) {
    const d = m.replace(/\D/g, '')
    if (d.length < 13 || d.length > 19) return false
    let sum = 0
    for (let i = 0; i < d.length; i++) {
      let n = d.charCodeAt(d.length - 1 - i) - 48
      if (i % 2 === 1) { n *= 2; if (n > 9) n -= 9 }
      sum += n
    }
    return sum % 10 === 0
  }
  function iban(m) {
    const s = m.replace(/\s/g, '').toUpperCase()
    if (!/^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$/.test(s)) return false
    let rem = 0
    for (const ch of s.slice(4) + s.slice(0, 4)) {
      const v = /[A-Z]/.test(ch) ? String(ch.charCodeAt(0) - 55) : ch
      for (const dg of v) rem = (rem * 10 + (dg.charCodeAt(0) - 48)) % 97
    }
    return rem === 1
  }
  const VALIDATORS = { luhn, iban }
  const passes = (name, m) => {
    const v = RULES.validators && RULES.validators[name]
    if (!v) return true
    const fn = VALIDATORS[v]
    return fn ? fn(m) : false // an unknown validator never vouches for a hit
  }
  const matchesOf = (rule, text) => {
    const out = []
    for (const m of text.matchAll(rule.all)) if (passes(rule.name, m[0])) out.push(m[0])
    return out
  }

  // What Shield does about one hit: a rule's own action first, else the
  // policy's action for its tier. "observe" mode makes everything a log.
  function actionFor(tier, name) {
    if (config.mode === 'observe') return 'log'
    const own = RULES.rule_actions && RULES.rule_actions[name]
    if (isAction(own)) return own
    const t = config.actions[tier]
    if (isAction(t)) return t
    const d = RULES.default_actions && RULES.default_actions[tier]
    return isAction(d) ? d : 'log'
  }

  // Rules a message matches: [{tier, name, action}] (rule names, never text).
  // The secret tier always reads the full text; pii and flag read the head and
  // tail of a text over SCAN_MAX (and the caller flags it oversize).
  const capped = (text) => (text.length > SCAN_MAX ? text.slice(0, SCAN_MAX / 2) + '\n' + text.slice(-SCAN_MAX / 2) : text)
  function scan(text) {
    const out = []
    const short = capped(text)
    for (const [tier, rules] of TIERS) {
      const t = tier === 'secret' ? text : short
      for (const r of rules) if (matchesOf(r, t).length) out.push({ tier, name: r.name, action: actionFor(tier, r.name) })
    }
    return out
  }

  // [EMAIL_1]: numbered per body by distinct value, so a model can keep
  // references straight. Lives only for one call; nothing is kept.
  function redactWithRules(text, names, state, hit) {
    for (const [tier, rules] of TIERS) {
      if (tier === 'flag') continue
      for (const r of rules) {
        if (!names.has(r.name)) continue
        text = text.replace(r.all, (m) => {
          if (!passes(r.name, m)) return m
          const label = (RULES.placeholder && RULES.placeholder[r.name]) || 'REDACTED'
          const seen = state[label] || (state[label] = new Map())
          if (!seen.has(m)) seen.set(m, seen.size + 1)
          hit.add(r.name)
          return `[${label}_${seen.get(m)}]`
        })
      }
    }
    return text
  }

  /*
   * Every string leaf of the body, at any depth, except the values of id-ish
   * keys (ids, model, signature, media type) and inline base64 payloads: system prompts, tool
   * results, documents and any future field are covered without listing them,
   * while conversation ids and timestamps can't trip the rules.
   */
  const SKIP_KEY = /^(id|uuid|model|signature|media_type|mime_?type|.*_(id|uuid))$/i
  // Inline payloads: a base64 blob or data: URI under `data`/`url`/`image_url`, never prose.
  const PAYLOAD_KEY = /^(data|url|image_url|source)$/i
  const isPayload = (k, v) => PAYLOAD_KEY.test(k) &&
    (/^data:[\w.+-]+\/[\w.+-]+;base64,/i.test(v) || /^[A-Za-z0-9+/=\s]{200,}$/.test(v))
  const MAX_DEPTH = 64
  function mapLeaves(node, fix, depth = 0) {
    if (!node || typeof node !== 'object' || depth > MAX_DEPTH) return
    for (const k of Array.isArray(node) ? node.keys() : Object.keys(node)) {
      const v = node[k]
      if (typeof v === 'string') {
        if (Array.isArray(node) || !(SKIP_KEY.test(k) || isPayload(k, v))) node[k] = fix(v)
      } else mapLeaves(v, fix, depth + 1)
    }
  }
  const mapTexts = (body, fix) => mapLeaves(body, fix)

  // What a block/warn/redact decision looks at: every leaf, joined.
  function allText(body) {
    const out = []
    mapLeaves(body, (s) => { out.push(s); return s })
    return out.join('\n')
  }

  // Returns the body unchanged (same string) when nothing matched.
  function redactJsonBody(raw, names) {
    let body
    try { body = JSON.parse(raw) } catch { return { raw, rules: [] } }
    if (!body || typeof body !== 'object' || Array.isArray(body)) return { raw, rules: [] }
    const hit = new Set()
    const state = {}
    mapTexts(body, (s) => redactWithRules(s, names, state, hit))
    if (!hit.size) return { raw, rules: [] }
    return { raw: JSON.stringify(body), rules: [...hit].sort() }
  }

  // --- the send ---

  /*
   * Everything decided about one outgoing body: its length, the rules it
   * matches, and what to do. `blocked` holds rule names when a hit's action is
   * block; `warn` holds the rule names to ask the user about (the strictest
   * action is warn). Otherwise the body is redacted per the hits' actions.
   */
  const redactable = (h) => h.tier !== 'flag' && (h.action === 'redact' || h.action === 'warn')
  function inspect(raw) {
    let body = null
    try { body = JSON.parse(raw) } catch { /* not JSON: length only */ }
    const isObj = body && typeof body === 'object' && !Array.isArray(body)
    const text = isObj ? messageText(body) : ''
    const facts = { chars: text ? text.length : raw.length }
    let decide = isObj ? allText(body) : ''
    let oversize = false
    if (decide.length > SCAN_MAX) {
      oversize = true // pii and flag rules scan head and tail; say so
    }
    if (!config.consented) return { facts, raw }
    const hits = decide ? scan(decide) : []
    const lastFlags = (text ? scan(text) : [])
    facts.flags = lastFlags.map((h) => `${h.tier}:${h.name}`) // last message, as Python
    if (oversize) facts.flags.push('flag:oversize')
    if (!governing()) return { facts, raw }
    const top = hits.reduce((m, h) => Math.max(m, ORDER[h.action]), 0)
    const named = (a) => hits.filter((h) => h.action === a).map((h) => h.name)
    if (top === ORDER.block) {
      return { facts: { ...facts, verdict: 'blocked' }, raw, blocked: named('block') }
    }
    const names = new Set(hits.filter(redactable).map((h) => h.name))
    if (top === ORDER.warn) {
      const keep = new Set(hits.filter((h) => h.tier !== 'flag' && h.action === 'redact').map((h) => h.name))
      return { facts, raw, warn: { labels: named('warn'), names, keep } }
    }
    let out = raw
    let redactions = []
    if (names.size) ({ raw: out, rules: redactions } = redactJsonBody(raw, names))
    facts.redactions = redactions
    facts.verdict = redactions.length ? `redacted(${redactions.length})` : config.mode
    return { facts, raw: out }
  }

  /*
   * warn: the send waits on the user, who answers through the relay's bar
   * (isolated world; this page can't be trusted to draw it). Results are
   * matched by send_id; one for an id that isn't waiting is ignored. Timeout
   * cancels: the original is never sent without an explicit choice.
   */
  const WARN_MS = 60_000
  const CHOICES = ['redacted', 'sent', 'cancelled']
  const waiting = new Map()
  function askUser(sendId, labels) {
    // No live relay to show the bar: don't hang, redact (never send the original).
    if (!port) return Promise.resolve('no_relay')
    return new Promise((resolve) => {
      const timer = setTimeout(() => { waiting.delete(sendId); resolve('cancelled') }, WARN_MS)
      waiting.set(sendId, { resolve, timer })
      try { port.postMessage({ t: 'warn', send_id: sendId, labels }) } catch { relayGone() }
    })
  }
  // The relay closed its end: nothing can answer, so every held send goes redacted.
  function relayGone() {
    port = null
    for (const [id, w] of [...waiting]) { clearTimeout(w.timer); waiting.delete(id); w.resolve('no_relay') }
  }
  // Only reached from the relay's port; an id that isn't waiting is ignored.
  function answered(sendId, choice) {
    const w = typeof sendId === 'string' && waiting.get(sendId)
    if (!w || !CHOICES.includes(choice)) return
    clearTimeout(w.timer)
    waiting.delete(sendId)
    w.resolve(choice)
  }

  // A random id for one send, carrying nothing about it.
  const newId = () => (crypto.randomUUID?.() ??
    Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join(''))

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

  // One send whose body is in hand as a string: inspect, maybe rewrite,
  // ask or stop, record, then send.
  function send(self, input, init, raw, wire) {
    lastInspected = Date.now()
    const { facts, raw: out, blocked, warn } = inspect(raw)
    // Ties this send's reply row to it, however many sends overlap.
    const sendId = newId()
    const base = { app, wire: wire.slice(0, 60), send_id: sendId }
    if (warn) {
      return askUser(sendId, warn.labels).then((choice) => {
        if (choice === 'cancelled') {
          emit({ kind: 'browser.chat.request', ...base, ...facts, verdict: 'warned\u2192cancelled' })
          return refusal(warn.labels)
        }
        // Send anyway skips only the warned hits: redact-tier ones still go.
        const extra = choice === 'no_relay' ? { reason: 'no_relay' } : {}
        if (choice === 'sent') {
          const { raw: red, rules } = redactJsonBody(raw, warn.keep)
          emit({ kind: 'browser.chat.request', ...base, ...facts, redactions: rules, verdict: 'warned\u2192sent' })
          return go(self, input, init, raw, red, facts, sendId)
        }
        const { raw: red, rules } = redactJsonBody(raw, warn.names)
        emit({ kind: 'browser.chat.request', ...base, ...facts, redactions: rules, verdict: 'warned\u2192redacted', ...extra })
        return go(self, input, init, raw, red, facts, sendId)
      })
    }
    emit({ kind: 'browser.chat.request', ...base, ...facts })
    if (blocked) return Promise.resolve(refusal(blocked))
    return go(self, input, init, raw, out, facts, sendId)
  }

  function go(self, input, init, raw, out, facts, sendId) {
    let args = [input, init]
    if (out !== raw) {
      args = init && init.body != null
        ? [input, { ...init, body: out }]
        : [new Request(input, { body: out }), init]
    }
    return orig.apply(self, args).then((res) => {
      emit({ kind: 'browser.chat.status', app, chars: facts.chars, ok: res.ok, status: res.status })
      if (config.consented && covered && res.ok &&
          /event-stream/.test(res.headers.get('content-type') || '')) {
        try { watchReply(res.clone(), sendId) } catch { /* the reply is the app's, whatever happens here */ }
      }
      return res
    })
  }

  /*
   * Canary: chat POSTs that look like a send but that CHAT_PATHS didn't
   * recognise. Three in ten minutes with no send inspected means the site
   * probably changed; say so (path prefix only, no body) instead of failing
   * silently open.
   */
  const LOOSE_SEND = /conversation|completion|chat|messages/i
  const UPLOAD_PATH = /files|upload|attachments/i
  const CANARY_MS = 10 * 60_000
  let unmatched = []
  let lastInspected = 0
  function canary(pathOnly) {
    const now = Date.now()
    unmatched = unmatched.filter((t) => now - t < CANARY_MS)
    unmatched.push(now)
    if (unmatched.length >= 3 && now - lastInspected >= CANARY_MS) {
      unmatched = []
      emit({ kind: 'browser.health.unmatched', app, path: pathOnly.slice(0, 60) })
    }
  }

  // Attachments are recorded, never read: type and size only.
  function noteAttachment(input, init) {
    let mime = ''
    let bytes = null
    const body = init && init.body
    const size = (b) => (typeof Blob !== 'undefined' && b instanceof Blob ? b.size : 0)
    if (typeof FormData !== 'undefined' && body instanceof FormData) {
      bytes = 0
      mime = 'multipart/form-data'
      for (const [, v] of body.entries()) {
        if (typeof v === 'string') bytes += v.length
        else { bytes += size(v); if (v.type && mime === 'multipart/form-data') mime = v.type }
      }
    } else if (typeof Blob !== 'undefined' && body instanceof Blob) {
      mime = body.type
      bytes = body.size
    } else {
      const h = (init && init.headers) || (typeof Request !== 'undefined' && input instanceof Request ? input.headers : null)
      const get = (k) => (h && typeof h.get === 'function' ? h.get(k) : h && (h[k] ?? h[k.toLowerCase()])) || ''
      mime = String(get('Content-Type')).split(';')[0].trim()
      const n = Number(get('Content-Length'))
      bytes = Number.isFinite(n) && n > 0 ? n : (typeof body === 'string' ? body.length : null)
    }
    mime = /^[\w.+-]{1,60}\/[\w.+-]{1,80}$/.test(mime) ? mime.toLowerCase() : ''
    emit({ kind: 'browser.chat.attachment', app, mime, bytes })
  }

  /*
   * Which tools the AI ran for this reply, read from a copy of the reply
   * stream as it arrives: tool names and a count of the sources a search
   * returned. The reply's text passes through here only to be parsed as
   * JSON events and is dropped; nothing of it is kept or sent.
   *
   * ChatGPT streams each message as a whole object ({o: "add", v: {message}}):
   * a tool's own message carries author {role: "tool", name: "web.run"}, and
   * the assistant's call to it names it as the recipient. Anthropic-style
   * streams (claude.ai) announce a tool with a content_block_start whose
   * block is a tool_use or server_tool_use.
   */
  const TOOL_NAME = /^[A-Za-z0-9_.:-]{1,48}$/
  const MAX_TOOLS = 12
  const MAX_STREAM = 8 * 1024 * 1024 // stop reading a copy past this; the app keeps its own

  function toolsIn(ev, found) {
    const msg = ev?.v?.message ?? ev?.message
    if (msg && typeof msg === 'object') {
      const role = msg.author?.role
      if (role === 'tool' && typeof msg.author.name === 'string') found.tools.add(msg.author.name)
      else if (role === 'assistant' && typeof msg.recipient === 'string' &&
               msg.recipient !== 'all' && msg.recipient !== 'assistant') found.calls.add(msg.recipient)
      for (const g of msg.metadata?.search_result_groups ?? []) {
        if (g && typeof g.domain === 'string') found.domains.add(g.domain)
      }
    }
    const block = ev?.content_block
    if (ev?.type === 'content_block_start' && block &&
        (block.type === 'tool_use' || block.type === 'server_tool_use') && typeof block.name === 'string') {
      found.tools.add(block.name)
    }
  }

  function summarise(found) {
    // A call is only listed when no tool answered under that name or a
    // longer one (ChatGPT addresses "web", then "web.run" answers).
    const names = [...found.tools]
    for (const call of found.calls) {
      if (!names.some((n) => n === call || n.startsWith(call + '.'))) names.push(call)
    }
    return names.filter((n) => TOOL_NAME.test(n)).slice(0, MAX_TOOLS)
  }

  async function watchReply(copy, sendId) {
    const reader = copy.body?.getReader()
    if (!reader) return
    const decoder = new TextDecoder()
    const found = { tools: new Set(), calls: new Set(), domains: new Set() }
    let pending = ''
    let seen = 0
    const take = (line) => {
      if (!line.startsWith('data:')) return
      const data = line.slice(5).trim()
      if (!data || data === '[DONE]') return
      try { toolsIn(JSON.parse(data), found) } catch { /* a partial or non-JSON line */ }
    }
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        seen += value.byteLength
        if (seen > MAX_STREAM) { reader.cancel().catch(() => {}); break }
        pending += decoder.decode(value, { stream: true })
        const lines = pending.split('\n')
        pending = lines.pop()
        lines.forEach(take)
      }
      take(pending)
    } catch { /* the stream was cut: report what was seen */ }
    const tools = summarise(found)
    if (!tools.length || !config.consented) return
    emit({ kind: 'browser.chat.reply', app, send_id: sendId, tools, sources: found.domains.size })
  }

  // A body that is text however it was wrapped: a string, a String object, an
  // ArrayBuffer or a typed array. Anything else returns null.
  function bodyText(b) {
    if (typeof b === 'string') return b
    const tag = Object.prototype.toString.call(b)
    if (tag === '[object String]') return String(b)
    if (tag === '[object ArrayBuffer]' || ArrayBuffer.isView(b)) {
      try { return new TextDecoder().decode(b) } catch { return null }
    }
    return null
  }

  window.fetch = function (input, init) {
    let url = ''
    try { url = typeof Request !== 'undefined' && input instanceof Request ? input.url : String(input) } catch { /* unreadable */ }
    const path = url.replace(/^https?:\/\/[^/]+/, '')
    const pathOnly = path.split('?')[0]
    try {
      const isRequestObject = typeof Request !== 'undefined' && input instanceof Request
      // Only sends: chat wires take a POST. A GET (a read), or a PUT/PATCH/DELETE
      // to a matching path (a rename, a delete), is not a message.
      const method = String((init && init.method) || (isRequestObject ? input.method : 'POST')).toUpperCase()
      const isChat = CHAT_PATHS.some((p) => p.test(path))
      const body = init && init.body
      if (method === 'POST' && ((typeof FormData !== 'undefined' && body instanceof FormData) ||
          (typeof Blob !== 'undefined' && body instanceof Blob && !isChat) || UPLOAD_PATH.test(pathOnly))) {
        noteAttachment(input, init)
      } else if (method === 'POST' && !isChat && covered && LOOSE_SEND.test(pathOnly)) {
        canary(pathOnly)
      }
      if (method === 'POST' && isChat) {
        const text = bodyText(body)
        if (text !== null) return send(this, input, init, text, path)
        if (typeof Blob !== 'undefined' && body instanceof Blob) {
          // A Blob on a chat path is the message itself: read it to inspect it.
          const self = this
          return body.text().then(
            (raw) => send(self, input, init, raw, path),
            () => {
              emit({ kind: 'browser.chat.request', app, chars: null, wire: path.slice(0, 60) })
              return orig.call(self, input, init)
            })
        }
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
