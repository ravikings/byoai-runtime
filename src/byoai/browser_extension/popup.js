/**
 * Popup: is Shield running, is it YOUR Shield, is this tab covered, one button
 * to open Shield. State is asked of the service worker and the local server;
 * the popup renders those facts, it never invents them. It checks once and
 * must never be able to hang on "Checking Shield".
 */
const el = (id) => document.getElementById(id)

const FALLBACK_ENDPOINT = 'http://127.0.0.1:17831/api/browser'
const APPS = {
  'claude.ai': 'Claude',
  'chatgpt.com': 'ChatGPT',
  'chat.openai.com': 'ChatGPT',
  'gemini.google.com': 'Gemini',
  'copilot.microsoft.com': 'Copilot',
}
const STALE_AFTER_MS = 7 * 24 * 3600 * 1000

document.addEventListener('DOMContentLoaded', async () => {
  el('ext-id').textContent = chrome.runtime.id
  wire()
  await refresh()
})

async function refresh() {
  const [probed, app] = await Promise.all([probe(), currentApp()])
  render(probed, app)
}

async function probe() {
  const check = await withTimeout(send({ type: 'agent.checkServer' }), 2500).catch(() => undefined)
  const endpoint = check?.endpoint || FALLBACK_ENDPOINT
  const state = check?.state || 'unreachable'
  if (state !== 'ok' && state !== 'paired') return { state, endpoint }
  try {
    const res = await withTimeout(fetch(baseOf(endpoint) + '/api/verify', { cache: 'no-store' }), 1500)
    if (!res.ok) throw new Error(String(res.status))
    const verify = await res.json()
    let apps = {}
    try {
      const policy = await withTimeout(fetch(baseOf(endpoint) + '/api/policy', { cache: 'no-store' }), 1500)
      apps = (await policy.json()).apps || {}
    } catch { /* an older Shield: say nothing rather than guess */ }
    return { state, endpoint, verify, apps }
  } catch {
    return { state: 'unreachable', endpoint }
  }
}

async function currentApp() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true })
    const host = tab?.url ? new URL(tab.url).host : ''
    const seen = (await chrome.storage.local.get('last_capture')).last_capture || {}
    return { host, name: APPS[host] || null, lastSeen: seen[host] || null }
  } catch {
    return { host: '', name: null, lastSeen: null }
  }
}

const baseOf = (endpoint) => endpoint.replace(/\/api\/browser$/, '')

function withTimeout(promise, ms) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms)),
  ])
}

function ago(ms) {
  const mins = Math.max(1, Math.round((Date.now() - ms) / 60000))
  if (mins < 60) return `${mins} min ago`
  const hours = Math.round(mins / 60)
  if (hours < 48) return `${hours} h ago`
  return `${Math.round(hours / 24)} days ago`
}

function render({ state, endpoint, verify, apps }, app) {
  el('endpoint').value = endpoint
  const set = (kind, icon, title, detail) => {
    el('status').className = `status ${kind}`
    el('icon').textContent = icon
    el('state-title').textContent = title
    el('state-detail').textContent = detail
  }
  const primary = el('open-shield')
  el('help').hidden = true
  el('trust').hidden = true
  primary.hidden = false
  primary.textContent = 'Open Shield'
  el('tab').textContent = ''

  if (state === 'mismatch') {
    set('bad', '!', "That isn't your Shield",
      "Something else on this Mac is answering at Shield's address, so nothing is being sent to it.")
    primary.hidden = true
    el('trust').hidden = false
    return
  }
  if (state === 'unverifiable') {
    set('bad', '!', "Shield can't prove who it is",
      'This Shield is too old to pair with the extension. Update it, then reopen this window.')
    primary.hidden = true
    return
  }
  if (state === 'unreachable') {
    set('bad', '!', "Shield isn't running",
      'Messages you send now are held here for a short while, up to 50, then dropped.')
    el('help').hidden = false
    primary.textContent = 'Check again'
    primary.dataset.retry = '1'
    return
  }
  delete primary.dataset.retry
  const v = verify || {}
  const n = v.entries
  if (v.tamper_evident === false) {
    set('bad', '!', 'The record does not check out', 'Open Shield to see which entry changed.')
  } else {
    set('ok', '✓', state === 'paired' ? 'Paired with your Shield' : 'Shield is on',
      typeof n === 'number'
        ? `${n} sealed ${n === 1 ? 'entry' : 'entries'}, all verified.`
        : 'The record is intact.')
  }
  const tab = el('tab')
  const appKey = { Claude: 'claude', ChatGPT: 'chatgpt', Gemini: 'gemini', Copilot: 'copilot' }[app.name]
  if (app.name && apps && apps[appKey] === false) {
    tab.textContent = `Shield is set not to record ${app.name}. Turn it on in Shield's settings and messages here will be noted.`
  } else if (app.name) {
    const b = Object.assign(document.createElement('b'), { textContent: app.name })
    if (app.lastSeen && Date.now() - app.lastSeen < STALE_AFTER_MS) {
      tab.append('Shield watches ', b, ` on this tab. Last message noted ${ago(app.lastSeen)}.`)
    } else {
      tab.append('Shield watches ', b, app.lastSeen
        ? ` on this tab, but has not noted a message in over a week. Send one to check it still works.`
        : ' on this tab. No message noted yet. Send one to check it works.')
    }
  } else if (app.host) {
    tab.textContent = `This tab (${app.host}) is not one Shield covers.`
  }
}

function wire() {
  el('open-shield').addEventListener('click', async (ev) => {
    if (ev.currentTarget.dataset.retry) { await refresh(); return }
    const endpoint = el('endpoint').value || FALLBACK_ENDPOINT
    chrome.tabs.create({ url: baseOf(endpoint) + '/shield' })
    window.close()
  })
  el('trust').addEventListener('click', async () => {
    await send({ type: 'agent.checkServer', trustCurrent: true })
    await refresh()
  })
  el('open-privacy').addEventListener('click', (ev) => {
    ev.preventDefault()
    chrome.tabs.create({ url: chrome.runtime.getURL('PRIVACY.md') })
    window.close()
  })
  el('copy-id').addEventListener('click', () => {
    navigator.clipboard.writeText(chrome.runtime.id)
    el('copy-id').textContent = 'Copied'
    setTimeout(() => { el('copy-id').textContent = 'Copy id' }, 1200)
  })
  el('save').addEventListener('click', async () => {
    const msg = el('msg')
    const r = await send({ type: 'agent.setEndpoint', endpoint: el('endpoint').value.trim() })
    msg.className = `msg ${r && r.ok ? 'ok' : 'bad'}`
    msg.textContent = r && r.ok ? 'Saved.' : (r?.error || 'That is not a Shield address on this Mac.')
    if (r && r.ok) await refresh()
  })
}

function send(msg) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (r) => {
        void chrome.runtime.lastError
        resolve(r)
      })
    } catch { resolve(undefined) }
  })
}
