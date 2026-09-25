/**
 * Popup: is Shield running, is this tab covered, one button to open Shield.
 * State is asked of the running service worker and the local server; the
 * popup renders those facts, it never invents them. It probes once and must
 * never be able to hang on "Checking Shield".
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

document.addEventListener('DOMContentLoaded', async () => {
  el('ext-id').textContent = chrome.runtime.id
  wire()
  const [probed, app] = await Promise.all([probe(), currentApp()])
  render(probed, app)
})

async function probe() {
  const config = await send({ type: 'agent.getEndpoint' })
  const endpoint = config?.endpoint || FALLBACK_ENDPOINT
  try {
    const base = baseOf(endpoint)
    const res = await withTimeout(fetch(base + '/api/verify', { cache: 'no-store' }), 1500)
    if (!res.ok) throw new Error(String(res.status))
    return { up: true, endpoint, verify: await res.json() }
  } catch {
    return { up: false, endpoint }
  }
}

async function currentApp() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true })
    const host = tab?.url ? new URL(tab.url).hostname : ''
    return { host, name: APPS[host] || null }
  } catch {
    return { host: '', name: null }
  }
}

const baseOf = (endpoint) => endpoint.replace(/\/api\/browser$/, '')

function withTimeout(promise, ms) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms)),
  ])
}

function render({ up, endpoint, verify }, app) {
  el('endpoint').value = endpoint
  const box = el('status')
  const set = (kind, icon, title, detail) => {
    box.className = `status ${kind}`
    el('icon').textContent = icon
    el('state-title').textContent = title
    el('state-detail').textContent = detail
  }
  if (!up) {
    set('bad', '!', "Shield isn't running",
      'Messages you send now are held here for a short while, up to 50, then dropped.')
    el('help').hidden = false
    el('open-shield').textContent = 'Try Shield again'
    el('tab').textContent = ''
    return
  }
  const v = verify || {}
  const n = v.entries
  if (v.tamper_evident === false) {
    set('bad', '!', 'The record does not check out',
      'Open Shield to see which entry changed.')
  } else {
    set('ok', '✓', 'Shield is on',
      typeof n === 'number'
        ? `${n} sealed ${n === 1 ? 'entry' : 'entries'}, all verified.`
        : 'The record is intact.')
  }
  el('tab').innerHTML = ''
  if (app.name) {
    el('tab').append('This tab: ', Object.assign(document.createElement('b'), { textContent: app.name }),
      ' is covered.')
  } else if (app.host) {
    el('tab').textContent = `This tab (${app.host}) is not one Shield covers.`
  }
}

function wire() {
  el('open-shield').addEventListener('click', async () => {
    const endpoint = el('endpoint').value || FALLBACK_ENDPOINT
    if (!el('help').hidden) {
      // Server was down: re-probe instead of opening a dead page.
      const again = await probe()
      if (again.up) { render(again, await currentApp()); el('help').hidden = true; el('open-shield').textContent = 'Open Shield' }
      return
    }
    chrome.tabs.create({ url: baseOf(endpoint) + '/shield' })
    window.close()
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
