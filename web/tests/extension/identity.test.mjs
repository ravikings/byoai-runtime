/**
 * Real-browser test for the pairing check: the extension must send rows only
 * to the Shield it paired with, not to whatever else answers on the port.
 *
 * Runs the real extension in Chrome and files rows the way the content relay
 * does (sendMessage from an extension page), so they go through the worker's
 * own queue → identity check → flush path. Phase 1: a real Shield pairs and
 * receives the row. Phase 2: an impostor takes over the same port; it must
 * receive nothing, and the popup must say so.
 */
import { describe, expect, it, afterAll } from 'vitest'
import puppeteer from 'puppeteer-core'
import { spawn } from 'node:child_process'
import { readFileSync, mkdtempSync, rmSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import os from 'node:os'

const EXT = path.resolve('../src/byoai/browser_extension')
const EXPECTED_ID = 'jbbongpiablbbmfflcaafjbeejeododc' // pinned by manifest.json "key"
const PORT = 18300 + Math.floor(Math.random() * 400)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

let shield, impostor, browser

describe('extension pairing (real browser)', () => {
  afterAll(async () => {
    await browser?.close?.()
    shield?.kill?.()
    impostor?.close()
  })

  it('pairs with the real Shield and refuses an impostor on the same port', { timeout: 90_000 }, async () => {
    const dataDir = mkdtempSync(path.join(os.tmpdir(), 'id-data-')) // ledger + seal chain live together
    const ledger = path.join(dataDir, 'captures.jsonl')
    const startShield = async () => {
      shield = spawn(path.resolve('../.venv/bin/python'),
        ['-m', 'byoai.integrations.shield', ledger, '--port', String(PORT)],
        { cwd: path.resolve('..'), stdio: 'ignore' })
      await sleep(2000)
    }
    await startShield()

    browser = await puppeteer.launch({
      executablePath: process.env.CHROMIUM_PATH ?? '/tmp/pptr/chromium/mac_arm-1704762/chrome-mac/Chromium.app/Contents/MacOS/Chromium',
      headless: false,
      args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`,
        '--user-data-dir=' + path.join(os.tmpdir(), `id-profile-${Date.now()}`), '--no-first-run'],
    })
    const worker = await (await browser.waitForTarget((t) => t.type() === 'service_worker', { timeout: 15_000 })).worker()
    expect(new URL(worker.url()).host).toBe(EXPECTED_ID) // the ID does not depend on the install path
    await worker.evaluate((p) => chrome.storage.local.set({ endpoint: `http://127.0.0.1:${p}/api/browser` }), PORT)

    const popup = await browser.newPage()
    const open = async () => {
      await popup.goto(`chrome-extension://${EXPECTED_ID}/popup.html`)
      await popup.waitForFunction(() => document.getElementById('state-title').textContent !== 'Checking Shield', { timeout: 8000 })
      return popup.$eval('#state-title', (n) => n.textContent)
    }
    const file = (rowId) => popup.evaluate((id) => chrome.runtime.sendMessage({
      type: 'agent.capture',
      row: { kind: 'browser.chat.request', app: 'claude', chars: 12, wire: 'input', row_id: id } }), rowId)

    // Phase 1: real Shield.
    expect(await open()).toBe('Paired with your Shield')
    await file('real-1')
    await sleep(4500)
    expect(readFileSync(ledger, 'utf8')).toContain('browser.chat.request')

    // Phase 1b: the record is wiped (ledger and chain both) and Shield comes
    // back. The extension remembers it had 1 sealed entry, so this must show.
    shield.kill(); await sleep(800)
    for (const f of ['captures.jsonl', 'sealchain.json', 'sealchain.log.jsonl']) rmSync(path.join(dataDir, f), { force: true })
    await startShield()
    expect(await open()).toBe("Shield's record is shorter than it was")
    await popup.click('#accept')
    await popup.waitForFunction(() => document.getElementById('state-title').textContent === 'Shield is on', { timeout: 8000 })

    // Phase 2: an impostor takes the port. Its signature comes from a key the
    // extension never paired with.
    shield.kill(); await sleep(800)
    const { generateKeyPairSync, sign } = await import('node:crypto')
    const { privateKey, publicKey } = generateKeyPairSync('ed25519')
    const pub = publicKey.export({ format: 'der', type: 'spki' }).subarray(-32).toString('base64')
    const seen = []
    impostor = http.createServer((req, res) => {
      seen.push(`${req.method} ${req.url.split('?')[0]}`)
      res.setHeader('content-type', 'application/json')
      if (req.url.startsWith('/api/identity')) {
        const nonce = new URL(req.url, 'http://x').searchParams.get('nonce')
        const sig = sign(null, Buffer.from('byoai-shield-identity:' + nonce), privateKey).toString('base64')
        return res.end(JSON.stringify({ device_id: 'fake', public_key: pub, sig: 'ed25519:' + sig }))
      }
      res.end(JSON.stringify({ tamper_evident: true, entries: 1, accepted: 1 }))
    })
    await new Promise((r) => impostor.listen(PORT, '127.0.0.1', r))

    expect(await open()).toBe("That isn't your Shield")
    await file('imp-1')
    await sleep(4500)
    expect(seen.filter((s) => s.startsWith('POST'))).toEqual([]) // nothing reached the impostor
  })
})
