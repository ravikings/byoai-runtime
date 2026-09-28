// @vitest-environment node
/**
 * The extension's page capture (content.js + the generated shield-rules.js),
 * run in a bare JavaScript context with a stand-in page: its own fetch, its
 * own event target. No browser needed, so this always runs.
 *
 * The cases come from tests/fixtures/shield_redaction_cases.json, which the
 * Python suite checks against the desktop proxy's rules too. Both passing is
 * what makes "Replace personal details" mean the same thing in the browser.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { REPO } from './env.mjs'

const EXT = path.join(REPO, 'src', 'byoai', 'browser_extension')
const RULES_JS = readFileSync(path.join(EXT, 'shield-rules.js'), 'utf8')
const CONTENT_JS = readFileSync(path.join(EXT, 'content.js'), 'utf8')
const { cases } = JSON.parse(readFileSync(path.join(REPO, 'tests', 'fixtures', 'shield_redaction_cases.json'), 'utf8'))

const SEND_PATH = { 'claude.ai': '/api/organizations/o/chat_conversations/c/completion', 'chatgpt.com': '/backend-api/conversation' }

/** A page on `host` with the capture loaded; `config` is what the relay would say. */
function page(host, config) {
  const target = new EventTarget()
  const sent = []
  const captured = []
  const window = {
    location: { host },
    addEventListener: target.addEventListener.bind(target),
    removeEventListener: target.removeEventListener.bind(target),
    dispatchEvent: target.dispatchEvent.bind(target),
    async fetch(input, init) {
      sent.push(input instanceof Request ? await input.text() : init?.body)
      return new Response('{}', { status: 200 })
    },
  }
  window.addEventListener('shield-agent-capture', (ev) => captured.push(ev.detail))
  // The relay answers the page capture's request for its config.
  if (config) {
    window.addEventListener('shield-agent-config-request', () =>
      window.dispatchEvent(new CustomEvent('shield-agent-config', { detail: JSON.stringify(config) })))
  }
  const ctx = vm.createContext({ window, location: window.location, CustomEvent, Request, Response })
  vm.runInContext(RULES_JS, ctx)
  vm.runInContext(CONTENT_JS, ctx)
  const send = (raw, init = {}) =>
    window.fetch(`https://${host}${SEND_PATH[host] ?? '/v1/messages'}`, { method: 'POST', body: raw, ...init })
  return { window, sent, captured, send }
}

const agreed = (mode, apps = null) => ({ consented: true, mode, apps })

describe('page capture: the shared redaction cases', () => {
  for (const c of cases) {
    it(c.name, async () => {
      const host = c.name.startsWith('chatgpt') ? 'chatgpt.com' : 'claude.ai'
      const p = page(host, agreed(c.mode))
      const raw = c.raw ?? JSON.stringify(c.body)
      const res = await p.send(raw)
      const [request] = p.captured
      expect(request.kind).toBe('browser.chat.request')
      expect(request.chars).toBe(c.expect.chars)
      expect(request.flags).toEqual(c.expect.flags)
      expect(request.verdict).toBe(c.expect.verdict)
      if (c.expect.blocked) {
        expect(p.sent).toEqual([])                     // never left the page
        expect(res.status).toBe(403)
        expect((await res.json()).error.message).toBe(`Stopped on this Mac: ${c.expect.blocked.join(', ')}`)
        expect(p.captured).toHaveLength(1)             // no status row for a send that never happened
        return
      }
      expect(request.redactions).toEqual(c.expect.redactions)
      if (c.expect.body === null) expect(p.sent).toEqual([raw])   // byte for byte
      else expect(JSON.parse(p.sent[0])).toEqual(c.expect.body)
      expect(p.captured[1]).toMatchObject({ kind: 'browser.chat.status', ok: true, status: 200 })
      // What goes to Shield never holds the message.
      expect(JSON.stringify(p.captured)).not.toMatch(/coriqo\.io|415-555|sk-abc|hunter2/)
    })
  }
})

describe('page capture: when it leaves a message alone', () => {
  const email = JSON.stringify({ prompt: 'mail abdul@coriqo.io' })

  it('changes nothing before the user agreed, and names no rules', async () => {
    const p = page('claude.ai', { consented: false, mode: 'redact' })
    await p.send(email)
    expect(p.sent).toEqual([email])
    expect(p.captured[0].flags).toBeUndefined()
  })

  it('changes nothing until the relay has spoken', async () => {
    const p = page('claude.ai', null)
    await p.send(email)
    expect(p.sent).toEqual([email])
  })

  it('leaves an app alone that Shield is set not to cover', async () => {
    const p = page('claude.ai', agreed('redact', { claude: false, chatgpt: true }))
    await p.send(email)
    expect(p.sent).toEqual([email])
    expect(p.captured[0]).toMatchObject({ flags: ['pii:emails'] })
    expect(p.captured[0].verdict).toBeUndefined()
  })

  it('redacts by default when no policy has been heard from Shield', async () => {
    const p = page('claude.ai', { consented: true })
    await p.send(email)
    expect(JSON.parse(p.sent[0]).prompt).toBe('mail [redacted-email]')
  })

  it('does not rewrite Gemini, whose sends it cannot read', async () => {
    const p = page('gemini.google.com', agreed('redact'))
    const raw = JSON.stringify({ prompt: 'mail abdul@coriqo.io' })
    await p.window.fetch('https://gemini.google.com/v1/messages', { method: 'POST', body: raw })
    expect(p.sent).toEqual([raw])
    expect(p.captured[0].verdict).toBeUndefined()
  })

  it('does not touch a read, or a path that is not a send', async () => {
    const p = page('claude.ai', agreed('redact'))
    await p.window.fetch('https://claude.ai/api/account', { method: 'POST', body: email })
    await p.window.fetch('https://claude.ai' + SEND_PATH['claude.ai'], { method: 'GET' })
    expect(p.sent).toEqual([email, undefined])
    expect(p.captured).toEqual([])
  })
})

describe('page capture: a send made with a Request object', () => {
  it('reads a copy of the body and rewrites it', async () => {
    const p = page('chatgpt.com', agreed('redact'))
    const body = JSON.stringify({ messages: [{ content: { parts: ['to abdul@coriqo.io'] } }] })
    await p.window.fetch(new Request('https://chatgpt.com/backend-api/conversation', { method: 'POST', body }))
    expect(JSON.parse(p.sent[0]).messages[0].content.parts[0]).toBe('to [redacted-email]')
    expect(p.captured[0]).toMatchObject({ chars: 18, redactions: ['emails'], verdict: 'redacted(1)' })
  })
})

describe('page capture: a body it cannot read', () => {
  it('notes the send without a length, and leaves it alone', async () => {
    const p = page('claude.ai', agreed('redact'))
    const form = new FormData()
    form.set('prompt', 'mail abdul@coriqo.io')
    await p.window.fetch('https://claude.ai' + SEND_PATH['claude.ai'], { method: 'POST', body: form })
    expect(p.sent).toEqual([form])
    expect(p.captured).toEqual([{ kind: 'browser.chat.request', app: 'claude', chars: null, wire: SEND_PATH['claude.ai'] }])
  })
})
