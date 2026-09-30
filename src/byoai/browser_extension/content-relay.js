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
  let toPage = null
  function tellPage() {
    if (!toPage) return
    toPage.postMessage({ t: 'config', consented: consented === true, mode: policy?.mode,
      apps: policy?.apps ?? null, actions: policy?.actions ?? null, files: policy?.files ?? null })
  }
  /*
   * One private MessageChannel to the page-world capture, transferred in a
   * single message now, at document_start, before page scripts run (content.js
   * is listed first and is already listening). A page can't send config or
   * warn answers on it: it never holds this end.
   */
  try {
    const channel = new MessageChannel()
    toPage = channel.port1
    toPage.onmessage = (ev) => {
      if (ev.data && ev.data.t === 'warn') onWarn(ev.data)
      else if (ev.data && ev.data.t === 'notice') onNotice(ev.data)
    }
    window.postMessage('shield-agent-port', '*', [channel.port2])
  } catch { toPage = null }

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
    // Tell the page first, so sends it is holding go redacted rather than wait.
    try { toPage && toPage.postMessage({ t: 'relay-closed' }) } catch { /* already closed */ }
    const closing = toPage
    toPage = null
    for (const w of bars.values()) w.finish('cancelled')
    try { closing && closing.close() } catch { /* already closed */ }
    clearTimeout(pendingFallback)
    document.removeEventListener('keydown', onKey, true)
    document.removeEventListener('click', onClick, true)
  }

  window.addEventListener('shield-agent-capture', onCapture)

  /*
   * The warn bar. The page capture holds a send and asks for it here; the
   * bar lives in a closed Shadow DOM (the page's CSS and scripts can't
   * restyle or click it) and shows human names for the rules, never the
   * matched text: it may be screenshotted. Labels arrive from the page, which
   * can be forged, so only names in RULE_LABEL become text; anything else
   * reads as "sensitive data".
   */
  const RULE_LABEL = {
    anthropic_key: 'an Anthropic API key', openai_key: 'an OpenAI API key',
    aws_access_key: 'an AWS access key', github_token: 'a GitHub token',
    slack_token: 'a Slack token', google_api_key: 'a Google API key',
    stripe_key: 'a Stripe key', private_key_block: 'a private key',
    jwt: 'a JSON web token', conn_string: 'a database URL with a password',
    bearer: 'a bearer token', credential_assign: 'a password or key assignment',
    emails: 'an email address', cards: 'a card number', phone_numbers: 'a phone number',
    ssn_like: 'a social security number', wallets: 'a crypto wallet address', iban: 'an IBAN',
  }
  const WARN_MS = 60_000
  const bars = new Map()
  let barHost = null
  let barBox = null

  function answer(sendId, choice) {
    try { toPage && toPage.postMessage({ t: 'warn-result', send_id: sendId, choice }) } catch { /* page gone */ }
  }

  function onWarnHost() {
    barHost = document.createElement('div')
    barHost.style.cssText = 'all:initial;position:fixed;top:0;left:0;right:0;z-index:2147483647'
    const root = barHost.attachShadow({ mode: 'closed' })
    const style = document.createElement('style')
    style.textContent = '.bar{font:14px system-ui,sans-serif;background:#1f2937;color:#fff;padding:10px 16px;' +
      'display:flex;gap:10px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #4b5563}' +
      '.msg{flex:1 1 240px}button{font:inherit;border:1px solid #6b7280;border-radius:6px;padding:5px 12px;' +
      'background:#374151;color:#fff;cursor:pointer}button.main{background:#2563eb;border-color:#2563eb}'
    barBox = document.createElement('div')
    root.append(style, barBox)
    ;(document.documentElement || document.body).append(barHost)
  }

  function onWarn(req) {
    if (!req || typeof req.send_id !== 'string' || req.send_id.length > 64 || bars.has(req.send_id)) return
    const sendId = req.send_id
    // A file variant: the file's name is shown here, in the bar only, and
    // never goes into an event. Shown as text, cut short.
    const isFile = req.file === true
    const fileName = isFile && typeof req.name === 'string' ? req.name.slice(0, 80) : ''
    const asked = Array.isArray(req.labels) ? req.labels : []
    const unreadable = isFile && asked.includes('unreadable')
    const names = [...new Set(asked.filter((n) => n !== 'unreadable')
      .map((n) => RULE_LABEL[n] ?? 'sensitive data'))].slice(0, 5)
    let row
    let timer
    const finish = (choice) => {
      if (!bars.delete(sendId)) return
      clearTimeout(timer)
      try { row.remove(); if (!barBox.children.length) { barHost.remove(); barHost = null; barBox = null } } catch { /* page gone */ }
      answer(sendId, choice)
    }
    try {
      if (!barHost) onWarnHost()
      row = document.createElement('div')
      row.className = 'bar'
      row.setAttribute('role', 'alertdialog')
      const msg = document.createElement('span')
      msg.className = 'msg'
      msg.textContent = isFile
        ? `Shield asks before this file goes out${fileName ? ` (${fileName})` : ''}` +
          `${names.length ? `: it contains ${names.join(', ')}` : ''}.` +
          `${unreadable ? " Shield couldn't check this file." : ''}`
        : `This message contains ${names.join(', ') || 'sensitive data'}.`
      const mk = (label, choice, main) => {
        const b = document.createElement('button')
        b.textContent = label
        if (main) b.className = 'main'
        b.addEventListener('click', () => finish(choice))
        return b
      }
      const def = isFile ? mk('Remove file', 'removed', true) : mk('Send redacted', 'redacted', true)
      row.append(msg, def, mk(isFile ? 'Upload anyway' : 'Send anyway', 'sent'), mk('Cancel', 'cancelled'))
      row.addEventListener('keydown', (e) => { if (e.key === 'Escape') finish('cancelled') })
      barBox.append(row)
      def.focus?.() // Enter picks the default
    } catch { answer(sendId, 'cancelled'); return }
    timer = setTimeout(() => finish('cancelled'), WARN_MS)
    bars.set(sendId, { finish })
  }

  /*
   * After Shield stops or removes a file, the app may still show it as
   * attached (its upload just failed). A short notice says it isn't. Same
   * bar area and the same forgery rule: labels outside RULE_LABEL read as
   * "sensitive data", names are shown as text and cut short.
   */
  const NOTICE_MS = 20_000
  function onNotice(req) {
    if (!req) return
    const isMessage = req.message === true
    if (!isMessage && !Array.isArray(req.names)) return
    const names = isMessage ? ['this message'] : req.names.filter((n) => typeof n === 'string').map((n) => n.slice(0, 80)).slice(0, 5)
    if (!names.length) return
    const why = [...new Set((Array.isArray(req.labels) ? req.labels : [])
      .map((n) => RULE_LABEL[n] ?? 'sensitive data'))].slice(0, 5)
    const verb = req.verdict === 'blocked' ? 'stopped' : req.verdict === 'removed' ? 'removed' : 'cancelled'
    try {
      if (!barHost) onWarnHost()
      const row = document.createElement('div')
      row.className = 'bar'
      row.setAttribute('role', 'status')
      const msg = document.createElement('span')
      msg.className = 'msg'
      const list = names.join(', ')
      msg.textContent = isMessage
        ? `Shield stopped this message${why.length ? `: it contains ${why.join(', ')}` : ''}. It wasn't sent; ` +
          'remove that part and send again.'
        : `Shield ${verb} ${list}${why.length ? ` (it contains ${why.join(', ')})` : ''}. ` +
        `${names.length > 1 ? "They weren't" : "It wasn't"} uploaded; remove ${names.length > 1 ? 'them' : 'it'} ` +
        'from your message before sending.'
      const ok = document.createElement('button')
      ok.textContent = 'OK'
      let timer
      const close = () => {
        clearTimeout(timer)
        try { row.remove(); if (barBox && !barBox.children.length) { barHost.remove(); barHost = null; barBox = null } } catch { /* page gone */ }
      }
      ok.addEventListener('click', close)
      row.append(msg, ok)
      barBox.append(row)
      timer = setTimeout(close, NOTICE_MS)
    } catch { /* the record still says what happened */ }
  }

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
