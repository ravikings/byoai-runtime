/**
 * Popup: is Shield reachable, where does the record land, open the full UI.
 * State is asked of the running service worker — the popup renders machine
 * facts, it never invents them. Rendering happens after exactly one probe;
 * it must never be able to hang on "Checking Shield…".
 */
const el = (id) => document.getElementById(id)

document.addEventListener('DOMContentLoaded', async () => {
  render(await probe())
  wire()
  el('ext-id').textContent = chrome.runtime.id
})

async function probe() {
  const config = await send({ type: 'agent.getEndpoint' })
  const endpoint = config?.endpoint
  if (!endpoint) {
    return { up: false, endpoint: 'http://127.0.0.1:8300/api/browser' }
  }
  try {
    const base = endpoint.replace(/\/api\/browser$/, '')
    const res = await withTimeout(fetch(base + '/api/verify', { cache: 'no-store' }), 1500)
    if (!res.ok) throw new Error(String(res.status))
    const verify = await res.json()
    return { up: true, endpoint, verify }
  } catch {
    return { up: false, endpoint }
  }
}

function withTimeout(promise, ms) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms)),
  ])
}

function render({ up, endpoint, verify }) {
  const dot = el('dot'); const title = el('state-title'); const detail = el('state-detail')
  el('endpoint').value = endpoint ?? ''
  if (up) {
    dot.className = 'dot on'
    const v = verify || {}
    title.textContent = 'Shield is running locally'
    const n = v.entries
    detail.textContent =
      (typeof n === 'number' ? `${n} sealed entr${n === 1 ? 'y' : 'ies'}. ` : '') +
      (v.tamper_evident === false ? 'WARNING: seal chain does not verify.' : 'Seal chain intact.')
  } else {
    dot.className = 'dot off'
    title.textContent = "Shield isn't running"
    detail.textContent = "Rows are queued locally (max 50) and dropped after that. Start it with: byoai-shield"
  }
}

function wire() {
  el('copy-id').addEventListener('click', () => {
    navigator.clipboard.writeText(chrome.runtime.id)
    el('copy-id').textContent = 'Copied'
    setTimeout(() => { el('copy-id').textContent = 'Copy id' }, 1200)
  })
  el('save').addEventListener('click', async () => {
    const msg = el('msg')
    const r = await send({ type: 'agent.setEndpoint', endpoint: el('endpoint').value.trim() })
    msg.className = `msg ${r && r.ok ? 'ok' : 'bad'}`
    msg.textContent = r && r.ok ? 'Saved.' : (r?.error || 'Not a localhost Shield endpoint.')
  })
  el('open-shield').addEventListener('click', async (ev) => {
    ev.preventDefault()
    const endpoint = el('endpoint').value
    const base = endpoint?.replace(/\/api\/browser$/, '') || 'http://127.0.0.1:8300'
    chrome.tabs.create({ url: base + '/shield' })
    window.close()
  })
  el('open-privacy').addEventListener('click', (ev) => {
    ev.preventDefault()
    chrome.tabs.create({ url: chrome.runtime.getURL('PRIVACY.md') })
    window.close()
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
