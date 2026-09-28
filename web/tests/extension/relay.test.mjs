// @vitest-environment node
/**
 * The isolated-world relay (content-relay.js) in a bare context with a
 * stand-in chrome.* and page. It must forward one row per send: the Enter/
 * click fallback exists for sends that don't go over fetch, and must not
 * add a second row for a send the page capture already recorded.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { REPO } from './env.mjs'

const EXT = path.join(REPO, 'src', 'byoai', 'browser_extension')
const CONSENT_JS = readFileSync(path.join(EXT, 'consent.js'), 'utf8')
const RELAY_JS = readFileSync(path.join(EXT, 'content-relay.js'), 'utf8')

function relay() {
  const win = new EventTarget()
  const doc = new EventTarget()
  const rows = []
  const chrome = {
    runtime: {
      lastError: undefined,
      sendMessage(msg, cb) { if (msg.type === 'agent.capture') rows.push(msg.row); cb?.() },
    },
    storage: {
      local: { get: (_keys, cb) => cb({ consent: { version: 2 }, shield_policy: null }) },
      onChanged: { addListener() {} },
    },
  }
  const ctx = vm.createContext({
    window: win, document: doc, chrome, location: { host: 'chatgpt.com' },
    CustomEvent, setTimeout, clearTimeout, Date, JSON,
  })
  vm.runInContext(CONSENT_JS, ctx)
  vm.runInContext(RELAY_JS, ctx)
  const enter = () => {
    const ev = new Event('keydown')
    Object.assign(ev, { key: 'Enter' })
    Object.defineProperty(ev, 'target', { value: { matches: () => true } })
    doc.dispatchEvent(ev)
  }
  const pageCapture = () => win.dispatchEvent(new CustomEvent('shield-agent-capture', {
    detail: { kind: 'browser.chat.request', app: 'chatgpt', chars: 47, wire: '/backend-api/f/conversation' },
  }))
  return { rows, enter, pageCapture }
}

describe('relay: one row per send', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('drops the Enter fallback when the page capture records the send', () => {
    const r = relay()
    r.enter()
    r.pageCapture()
    vi.advanceTimersByTime(5000)
    expect(r.rows.map((x) => x.wire)).toEqual(['/backend-api/f/conversation'])
  })

  it('drops it too when the page capture came first', () => {
    const r = relay()
    r.pageCapture()
    r.enter()
    vi.advanceTimersByTime(5000)
    expect(r.rows).toHaveLength(1)
  })

  it('drops it when a slow page starts its fetch seconds later', () => {
    const r = relay()
    r.enter()
    vi.advanceTimersByTime(3000)
    r.pageCapture()
    vi.advanceTimersByTime(5000)
    expect(r.rows).toHaveLength(1)
  })

  it('keeps the fallback for a send the page capture never saw', () => {
    const r = relay()
    r.enter()
    vi.advanceTimersByTime(4999)
    expect(r.rows).toEqual([])                      // held while a fetch may follow
    vi.advanceTimersByTime(1)
    expect(r.rows).toEqual([{ kind: 'browser.chat.request', app: 'chatgpt.com', chars: null, wire: 'input' }])
  })
})
