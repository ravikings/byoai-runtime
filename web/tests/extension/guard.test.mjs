// @vitest-environment jsdom
/**
 * Shield Lite Guard in the extension: actions, the warn bar (page capture +
 * relay wired through one window), numbered placeholders, the canary and
 * attachment facts. window.__shieldRules is stubbed with the contract shape,
 * so this holds whatever the generated shield-rules.js currently contains.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { JSDOM } from 'jsdom'
import { connectPage, fakeChannel, postPort } from './port.mjs'
import { REPO } from './env.mjs'

const EXT = path.join(REPO, 'src', 'byoai', 'browser_extension')
const read = (f) => readFileSync(path.join(EXT, f), 'utf8')
const SITES_JS = readFileSync(path.join(EXT, 'shield-sites.js'), 'utf8')
const CORE_JS = readFileSync(path.join(EXT, 'shield-core.js'), 'utf8')
const CONTENT_JS = read('content.js')
const RELAY_JS = read('content-relay.js')
const CONSENT_JS = read('consent.js')

const AWS = 'AKIAIOSFODNN7EXAMPLE'
const EMAIL = 'ann@example.com'
const EMAIL2 = 'bob@example.com'
const CARD = '4111 1111 1111 1111'
const BAD_CARD = '4111 1111 1111 1112'
const IBAN = 'GB82WEST12345698765432'
const FIXTURES = [AWS, EMAIL, EMAIL2, CARD, IBAN, 'hunter2']

const RULES = {
  secret: [
    ['aws_access_key', '\\b(AKIA|ASIA)[0-9A-Z]{16}\\b', ''],
    ['credential_assign', '\\b(password|api[_ ]?key)\\s*[:=]\\s*\\S+', 'i'],
  ],
  pii: [
    ['emails', '[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}', 'i'],
    ['cards', '\\b(?:\\d[ -]?){12,18}\\d\\b', ''],
    ['iban', '\\b[A-Z]{2}\\d{2}[A-Z0-9]{11,30}\\b', ''],
  ],
  flag: [['tool_intent', '\\bdeploy\\b', 'i']],
  validators: { cards: 'luhn', iban: 'iban' },
  placeholder: { emails: 'EMAIL', cards: 'CARD', iban: 'IBAN', aws_access_key: 'SECRET', credential_assign: 'SECRET' },
  rule_actions: { credential_assign: 'warn' },
  default_actions: { secret: 'block', pii: 'redact', flag: 'log' },
  covered_apps: ['claude', 'chatgpt'],
}

const PATHS = { 'claude.ai': '/api/organizations/o/chat_conversations/c/completion', 'chatgpt.com': '/backend-api/conversation' }

/** A fresh page (its own jsdom window) with the capture, and optionally the relay, loaded. */
function world({ host = 'claude.ai', config, relay = false, reply = () => new Response('{}') } = {}) {
  const dom = new JSDOM('<!doctype html><body></body>', { url: `https://${host}/` })
  const win = dom.window
  const doc = win.document
  const sent = []
  const captured = []
  const rows = []
  const w = { shadow: null, channel: null }
  const attach = win.Element.prototype.attachShadow
  win.Element.prototype.attachShadow = function (init) { w.shadow = attach.call(this, { ...init, mode: 'open' }); return w.shadow }
  win.__shieldRules = RULES
  win.fetch = async (input, init) => { sent.push(init?.body ?? null); return reply() }
  win.addEventListener('shield-agent-capture', (ev) => captured.push(ev.detail))
  const actions = config?.actions ?? null
  const policy = relay ? { mode: config?.mode ?? 'redact', apps: null, actions } : null
  const ctx = vm.createContext({ window: win, document: doc, location: { host }, CustomEvent: win.CustomEvent,
    Request, Response, TextDecoder, TextEncoder, crypto, FormData, Blob, JSON, URL, ArrayBuffer, Uint8Array,
    MessageChannel: function () { w.channel = fakeChannel(); return w.channel },
    setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (t) => clearTimeout(t), Date: { now: () => Date.now() },
    chrome: { runtime: { lastError: undefined, sendMessage: (m, cb) => { if (m.type === 'agent.capture') rows.push(m.row); cb?.() } },
      storage: { local: { get: (_k, cb) => cb({ consent: { version: 2 }, shield_policy: policy }) }, onChanged: { addListener() {} } } },
  })
  win.postMessage = (data, _origin, transfer) => postPort(win, win.Event, transfer[0], data)
  vm.runInContext(CONSENT_JS, ctx)
  vm.runInContext(SITES_JS, ctx)
  vm.runInContext(CORE_JS, ctx)
  vm.runInContext(CONTENT_JS, ctx) // listed first in the manifest, so it is listening when the relay posts
  if (relay) vm.runInContext(RELAY_JS, ctx)
  else connectPage(win, win.Event, { consented: true, mode: 'redact', apps: null, ...config })
  const post = (raw, p = PATHS[host], init = {}) =>
    win.fetch(`https://${host}${p}`, { method: 'POST', body: raw, ...init })
  return { win, w, sent, captured, rows, post }
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })
let current = null
const make = (o) => { const x = world(o); current = x; return x }

const body = (text) => JSON.stringify({ prompt: text })
const promptOf = (raw) => JSON.parse(raw).prompt
const buttons = () => Object.fromEntries([...current.w.shadow.querySelectorAll('button')].map((b) => [b.textContent, b]))
const flush = async () => { await vi.advanceTimersByTimeAsync(0) }

describe('actions', () => {
  it('blocks a secret by default and never sends it', async () => {
    const w = make({})
    const res = await w.post(body(`key ${AWS}`))
    expect(res.status).toBe(403)
    expect(w.sent).toEqual([])
    expect(w.captured[0]).toMatchObject({ verdict: 'blocked', flags: ['secret:aws_access_key'] })
  })
  it('redacts pii by default', async () => {
    const w = make({})
    await w.post(body(`mail ${EMAIL}`))
    expect(promptOf(w.sent[0])).toBe('mail [EMAIL_1]')
    expect(w.captured[0]).toMatchObject({ redactions: ['emails'], verdict: 'redacted(1)' })
  })
  it('a card counts only when Luhn passes', async () => {
    const w = make({})
    await w.post(body(`c ${BAD_CARD}`))
    expect(promptOf(w.sent[0])).toBe(`c ${BAD_CARD}`)
    await w.post(body(`c ${CARD}`))
    expect(promptOf(w.sent[1])).toBe('c [CARD_1]')
  })
  it('an IBAN counts only with a valid checksum', async () => {
    const w = make({})
    await w.post(body(`i GB82WEST12345698765433`))
    expect(promptOf(w.sent[0])).toContain('GB82WEST12345698765433')
    await w.post(body(`i ${IBAN}`))
    expect(promptOf(w.sent[1])).toBe('i [IBAN_1]')
  })
  it('flag rules only log; observe logs everything', async () => {
    const w = make({})
    await w.post(body('please deploy'))
    expect(w.sent).toHaveLength(1)
    expect(w.captured[0].flags).toEqual(['flag:tool_intent'])
    const o = make({ config: { mode: 'observe' } })
    await o.post(body(`k ${AWS} ${EMAIL}`))
    expect(promptOf(o.sent[0])).toContain(AWS)
    expect(o.captured[0].verdict).toBe('observe')
  })
  it('config actions override defaults; invalid ones fall back', async () => {
    const w = make({ config: { actions: { secret: 'redact', pii: 'nonsense' } } })
    await w.post(body(`k ${AWS} m ${EMAIL}`))
    expect(promptOf(w.sent[0])).toBe('k [SECRET_1] m [EMAIL_1]')
  })
  it('the strictest action wins', async () => {
    const w = make({})
    const res = await w.post(body(`${AWS} ${EMAIL}`))
    expect(res.status).toBe(403)
  })
})

describe('every turn is decided on', () => {
  it('blocks a secret that is only in an earlier turn', async () => {
    const w = make({})
    const raw = JSON.stringify({ messages: [{ role: 'user', content: `key ${AWS}` }, { role: 'user', content: 'thanks' }] })
    const res = await w.post(raw)
    expect(res.status).toBe(403)
    expect(w.sent).toEqual([])
    expect(w.captured[0].chars).toBe('thanks'.length)
  })
  it('redacts a card in an earlier turn and in Gemini contents', async () => {
    const w = make({})
    await w.post(JSON.stringify({ messages: [{ content: `c ${CARD}` }, { content: 'ok' }] }))
    expect(JSON.parse(w.sent[0]).messages[0].content).toBe('c [CARD_1]')
    await w.post(JSON.stringify({ prompt: 'x', contents: [{ parts: [{ text: `m ${EMAIL}` }] }] }))
    expect(JSON.parse(w.sent[1]).contents[0].parts[0].text).toBe('m [EMAIL_1]')
  })
})

describe('placeholders', () => {
  it('numbers by distinct value; the same value keeps its number across fields', async () => {
    const w = make({ host: 'chatgpt.com' })
    const raw = JSON.stringify({ messages: [
      { content: { parts: [`${EMAIL} then ${EMAIL2}`] } },
      { content: { parts: [`again ${EMAIL} and ${EMAIL2} and ${EMAIL}`] } },
    ] })
    await w.post(raw)
    const m = JSON.parse(w.sent[0]).messages
    expect(m[0].content.parts[0]).toBe('[EMAIL_1] then [EMAIL_2]')
    expect(m[1].content.parts[0]).toBe('again [EMAIL_1] and [EMAIL_2] and [EMAIL_1]')
  })
  it('restarts at 1 for the next message', async () => {
    const w = make({})
    await w.post(body(`${EMAIL2}`))
    await w.post(body(`${EMAIL}`))
    expect(promptOf(w.sent[1])).toBe('[EMAIL_1]')
  })
})

describe('warn', () => {
  const warnConfig = { config: { actions: { secret: 'warn' } }, relay: true }
  const secretBody = () => body(`use ${AWS} and ${EMAIL}`)

  it('holds the send and shows a bar with labels only', async () => {
    const w = make(warnConfig)
    const p = w.post(secretBody())
    await flush()
    expect(w.sent).toEqual([])
    expect(current.w.shadow.textContent).toContain('an AWS access key')
    expect(current.w.shadow.textContent).not.toMatch(new RegExp(FIXTURES.join('|')))
    expect(Object.keys(buttons())).toEqual(['Send redacted', 'Send anyway', 'Cancel'])
    buttons()['Cancel'].click()
    await p
  })
  it('Send redacted', async () => {
    const w = make(warnConfig)
    const p = w.post(secretBody())
    await flush()
    buttons()['Send redacted'].click()
    await p
    expect(promptOf(w.sent[0])).toBe('use [SECRET_1] and [EMAIL_1]')
    expect(w.captured[0].verdict).toBe('warned→redacted')
    expect(current.w.shadow.querySelectorAll('.bar').length).toBe(0)
  })
  it('Send anyway skips only the warned hits: the email is still redacted', async () => {
    const w = make(warnConfig)
    const p = w.post(secretBody())
    await flush()
    buttons()['Send anyway'].click()
    await p
    expect(promptOf(w.sent[0])).toBe(`use ${AWS} and [EMAIL_1]`) // only the warned hit goes as typed
    expect(w.captured[0]).toMatchObject({ verdict: 'warned→sent', redactions: ['emails'] })
  })
  it('Cancel returns the refusal and sends nothing', async () => {
    const w = make(warnConfig)
    const p = w.post(secretBody())
    await flush()
    buttons()['Cancel'].click()
    const res = await p
    expect(res.status).toBe(403)
    expect(w.sent).toEqual([])
    expect(w.captured[0].verdict).toBe('warned→cancelled')
  })
  it('times out after 60s as cancel, never sending the original', async () => {
    const w = make(warnConfig)
    const p = w.post(secretBody())
    await flush()
    await vi.advanceTimersByTimeAsync(59_000)
    expect(w.sent).toEqual([])
    await vi.advanceTimersByTimeAsync(1_500)
    const res = await p
    expect(res.status).toBe(403)
    expect(w.sent).toEqual([])
    expect(w.captured[0].verdict).toBe('warned→cancelled')
  })
  it('ignores a forged result for an unknown send_id, and a bad choice', async () => {
    const w = make(warnConfig)
    let settled = false
    const p = w.post(secretBody()).then((r) => { settled = true; return r })
    await flush()
    const forge = (detail) => w.win.dispatchEvent(new w.win.CustomEvent('shield-agent-warn-result', { detail }))
    forge(JSON.stringify({ send_id: 'nope', choice: 'sent' }))
        forge('not json')
    await flush()
    expect(settled).toBe(false)
    expect(w.sent).toEqual([])
    buttons()['Cancel'].click()
    await p
  })
  it('a forged warn request with odd labels only ever shows generic text', async () => {
    const w = make({ relay: true })
    w.w.channel.port2.postMessage({ t: 'warn', send_id: 'a', labels: ['<img src=x onerror=1>'] })
    expect(current.w.shadow.querySelector('img')).toBeNull()
    expect(current.w.shadow.textContent).toContain('sensitive data')
  })
  it('a per-rule warn (credential_assign) warns even under the default secret: block', async () => {
    const w = make({ relay: true })
    const p = w.post(body('password: hunter2'))
    await flush()
    expect(current.w.shadow.textContent).toContain('password or key assignment')
    buttons()['Cancel'].click()
    await p
  })
})

describe('warn without a relay', () => {
  it('relay gone: redacts, never hangs, never sends the original', async () => {
    const dom = new JSDOM('<body></body>', { url: 'https://claude.ai/' })
    const win = dom.window
    const sent = []
    const captured = []
    win.__shieldRules = RULES
    win.fetch = async (i, init) => { sent.push(init.body); return new Response('{}') }
    win.addEventListener('shield-agent-capture', (ev) => captured.push(ev.detail))
    const ctx = vm.createContext({ window: win, document: win.document, location: { host: 'claude.ai' }, CustomEvent: win.CustomEvent,
      Request, Response, TextDecoder, crypto, FormData, Blob, JSON, setTimeout, clearTimeout, Date })
    vm.runInContext(SITES_JS, ctx)
    vm.runInContext(CORE_JS, ctx)
    vm.runInContext(CONTENT_JS, ctx)
    // a relay that connects, sets warn, then goes away
    const relayEnd = connectPage(win, win.Event, { consented: true, mode: 'redact', apps: null, actions: { secret: 'warn' } })
    relayEnd.postMessage({ t: 'relay-closed' })
    await win.fetch(`https://claude.ai${PATHS['claude.ai']}`, { method: 'POST', body: body(`${AWS} ${EMAIL}`) })
    expect(promptOf(sent[0])).toBe('[SECRET_1] [EMAIL_1]')
    expect(captured[0]).toMatchObject({ verdict: 'warned\u2192redacted', reason: 'no_relay' })
  })
  it('a held send goes redacted when the relay closes', async () => {
    const w = make({ relay: true, config: { actions: { secret: 'warn' } } })
    const p = w.post(body(`${AWS}`))
    await flush()
    expect(w.sent).toEqual([])
    w.w.channel.port1.postMessage({ t: 'relay-closed' })
    await p
    expect(promptOf(w.sent[0])).toBe('[SECRET_1]')
    expect(w.captured[0]).toMatchObject({ verdict: 'warned\u2192redacted', reason: 'no_relay' })
  })
})

describe('canary', () => {
  it('fires after 3 unmatched sends in 10 min with none inspected', async () => {
    const w = make({})
    for (let i = 0; i < 3; i++) await w.post('{}', '/api/organizations/o/new_thing/chat/submit')
    const rows = w.captured.filter((r) => r.kind === 'browser.health.unmatched')
    expect(rows).toEqual([{ kind: 'browser.health.unmatched', app: 'claude', path: '/api/organizations/o/new_thing/chat/submit'.slice(0, 60) }])
  })
  it('trims the path to 60 characters and drops the query', async () => {
    const w = make({})
    const long = '/api/chat/' + 'x'.repeat(100)
    for (let i = 0; i < 3; i++) await w.post('{}', long + '?secret=1')
    const row = w.captured.find((r) => r.kind === 'browser.health.unmatched')
    expect(row.path).toHaveLength(60)
    expect(row.path).not.toContain('secret')
  })
  it('stays quiet when a send was inspected', async () => {
    const w = make({})
    await w.post(body('hi'))
    for (let i = 0; i < 3; i++) await w.post('{}', '/api/chat/other')
    expect(w.captured.some((r) => r.kind === 'browser.health.unmatched')).toBe(false)
  })
  it('stays quiet under 3, or when they are spread over more than 10 min', async () => {
    const w = make({})
    await w.post('{}', '/api/chat/a')
    await w.post('{}', '/api/chat/a')
    await vi.advanceTimersByTimeAsync(11 * 60_000)
    await w.post('{}', '/api/chat/a')
    expect(w.captured.some((r) => r.kind === 'browser.health.unmatched')).toBe(false)
  })
  it('ignores upload paths and GETs', async () => {
    const w = make({})
    for (let i = 0; i < 3; i++) await w.post('{}', '/api/chat/files')
    for (let i = 0; i < 3; i++) await w.win.fetch('https://claude.ai/api/chat/x', { method: 'GET' })
    expect(w.captured.some((r) => r.kind === 'browser.health.unmatched')).toBe(false)
  })
})

describe('no message text in any event', () => {
  it('emits none of the fixture strings, across every path', async () => {
    const w = make({ relay: true, config: { actions: { secret: 'warn' } } })
    const all = `key ${AWS} ${EMAIL} ${EMAIL2} ${CARD} ${IBAN} password: hunter2`
    const p1 = w.post(body(all))
    await flush()
    buttons()['Send redacted'].click()
    await p1
    const p2 = w.post(body(all))
    await flush()
    buttons()['Cancel'].click()
    await p2
    await w.post(body(`plain ${EMAIL}`))
    const form = new FormData(); form.set('note', all)
    await w.post(form, '/upload')
    await w.post(new Blob([all]), '/api/v2/rum')
    for (let i = 0; i < 3; i++) await w.post(all, '/api/chat/new?x=' + EMAIL)
    const blob = JSON.stringify([w.captured, w.rows])
    for (const f of FIXTURES) expect(blob).not.toContain(f)
    expect(w.rows.length).toBeGreaterThan(0)
  })
})

describe('forged page events', () => {
  const AWSBODY = () => body(AWS)
  it('a window config event cannot lower the policy', async () => {
    const w = make({})
    w.win.dispatchEvent(new w.win.CustomEvent('shield-agent-config', {
      detail: JSON.stringify({ consented: true, mode: 'redact', apps: null, actions: { secret: 'log', pii: 'log' } }),
    }))
    const res = await w.post(AWSBODY())
    expect(res.status).toBe(403)
    expect(w.sent).toEqual([])
  })
  it('a second port from the page is ignored: first port wins', async () => {
    const w = make({})
    const rogue = fakeChannel()
    postPort(w.win, w.win.Event, rogue.port2)
    rogue.port1.postMessage({ t: 'config', consented: true, mode: 'redact', apps: null, actions: { secret: 'log' } })
    const res = await w.post(AWSBODY())
    expect(res.status).toBe(403)
  })
  it('a warn-result on a rogue port or a window event never answers a warn', async () => {
    const w = make({ relay: true, config: { actions: { secret: 'warn' } } })
    let settled = false
    const p = w.post(AWSBODY()).then((r) => { settled = true; return r })
    await flush()
    const id = w.captured.length ? w.captured[0].send_id : null
    const rogue = fakeChannel()
    postPort(w.win, w.win.Event, rogue.port2)
    rogue.port1.postMessage({ t: 'warn-result', send_id: id, choice: 'sent' })
    w.win.dispatchEvent(new w.win.CustomEvent('shield-agent-warn-result', { detail: JSON.stringify({ send_id: id, choice: 'sent' }) }))
    await flush()
    expect(settled).toBe(false)
    expect(w.sent).toEqual([])
    buttons()['Cancel'].click()
    expect((await p).status).toBe(403)
  })
  it('the port event is hidden from page listeners', async () => {
    const seen = []
    const w = make({})
    // A page listener added after the capture loaded never sees a port event.
    w.win.addEventListener('message', (e) => seen.push(e))
    const relay2 = fakeChannel()
    postPort(w.win, w.win.Event, relay2.port2)
    expect(seen).toEqual([])
  })
})

describe('body shapes', () => {
  const U = (w) => `https://claude.ai${PATHS['claude.ai']}`
  const j = (o) => JSON.stringify(o)
  const blocked = async (w, call) => { const r = await call(); expect(r.status).toBe(403); expect(w.sent).toEqual([]) }
  it('a URL object as input', async () => {
    const w = make({})
    await blocked(w, () => w.win.fetch(new URL(U()), { method: 'POST', body: body(AWS) }))
  })
  it('a Uint8Array, an ArrayBuffer and a String object body', async () => {
    const w = make({})
    const enc = new TextEncoder().encode(body(AWS))
    await blocked(w, () => w.win.fetch(U(), { method: 'POST', body: enc }))
    await blocked(w, () => w.win.fetch(U(), { method: 'POST', body: enc.buffer }))
    await blocked(w, () => w.win.fetch(U(), { method: 'POST', body: new String(body(AWS)) }))
  })
  it('a typed-array body is rewritten as a string when redacting', async () => {
    const w = make({})
    await w.win.fetch(U(), { method: 'POST', body: new TextEncoder().encode(body(`m ${EMAIL}`)) })
    expect(typeof w.sent[0]).toBe('string')
    expect(promptOf(w.sent[0])).toBe('m [EMAIL_1]')
  })
  it('a Blob body on a chat path is read and inspected', async () => {
    const w = make({})
    const blob = new Blob([body(AWS)])
    blob.text = async () => body(AWS) // this test environment's Blob has no text()
    await blocked(w, () => w.win.fetch(U(), { method: 'POST', body: blob }))
  })
  it('system, tool_result and nested text leaves are decided on and redacted', async () => {
    const w = make({})
    await blocked(w, () => w.post(j({ system: AWS, messages: [{ role: 'user', content: 'hi' }] })))
    await blocked(w, () => w.post(j({ messages: [{ role: 'user', content: [{ type: 'tool_result', content: AWS }] }] })))
    await blocked(w, () => w.post(j({ messages: [{ content: [{ type: 'tool_result', content: [{ type: 'text', text: AWS }] }] }] })))
    await w.post(j({ system: `mail ${EMAIL}`, messages: [{ content: [{ type: 'tool_use', input: { to: EMAIL } }] }] }))
    const out = JSON.parse(w.sent[0])
    expect(out.system).toBe('mail [EMAIL_1]')
    expect(out.messages[0].content[0].input.to).toBe('[EMAIL_1]')
  })
  it('id-ish keys are left alone', async () => {
    const w = make({})
    await w.post(j({ prompt: 'hi', conversation_id: EMAIL, parent_uuid: EMAIL, model: EMAIL, signature: EMAIL }))
    expect(w.sent[0]).toContain(EMAIL)
    expect(w.captured[0].verdict).toBe('redact')
  })
  it('name, type, role, time and *_at values are scanned', async () => {
    const w = make({})
    await blocked(w, () => w.post(j({ prompt: 'hi', name: AWS })))
    await blocked(w, () => w.post(j({ prompt: 'hi', created_at: AWS })))
    await blocked(w, () => w.post(j({ prompt: 'hi', role: AWS })))
  })
  it('a base64 payload under data is left alone', async () => {
    const w = make({})
    await w.post(j({ prompt: 'hi', source: { type: 'base64', data: 'QUJD'.repeat(100) } }))
    expect(w.captured[0].flags).toEqual([])
  })
  it('a secret in the middle of a 3MB body is still blocked', async () => {
    const w = make({})
    const filler = 'a '.repeat(750_000)
    await blocked(w, () => w.post(j({ prompt: `${filler}${filler} ${AWS} ${filler}${filler}` })))
    expect(w.captured[0].flags).toContain('flag:oversize')
  })
  it('an oversize body is scanned head and tail and flagged', async () => {
    const w = make({})
    const big = 'a '.repeat(1_100_000)
    const res = await w.post(j({ prompt: `${AWS} ${big} tail` }))
    expect(res.status).toBe(403)
    const res2 = await w.post(j({ prompt: `${big} ${big} ${AWS}` }))
    expect(res2.status).toBe(403)
    expect(w.captured[1].flags).toContain('flag:oversize')
    const res3 = await w.post(j({ prompt: `${big.slice(0, 1_200_000)}${AWS}${big}` }))
    expect(w.captured[2].flags).toContain('flag:oversize') // the middle is not scanned, but it is said
    void res3
  })
})
