/**
 * What the real-browser extension tests need from the machine they run on.
 * A missing piece skips the test with a reason; it never fails the suite,
 * because plain `vitest run` on a machine without these is a normal thing to do.
 *
 *   CHROMIUM_PATH   a Chromium build that still honours --load-extension
 *                   (branded Chrome 137+ does not); BYOAI_EXT_TEST_DOWNLOAD=1
 *                   fetches one into ~/.cache/byoai-ext-test on first use
 *   BYOAI_PYTHON    the interpreter that has byoai installed
 */
import { execSync } from 'node:child_process'
import { existsSync, readdirSync } from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Anchored to this file, not the working directory, so it holds wherever vitest runs.
export const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..')
const CACHE = path.join(os.homedir(), '.cache', 'byoai-ext-test')
const RELATIVE = {
  darwin: 'chrome-mac/Chromium.app/Contents/MacOS/Chromium',
  linux: 'chrome-linux/chrome',
  win32: 'chrome-win/chrome.exe',
}[process.platform]

function findCached(dir) {
  try {
    for (const build of readdirSync(path.join(dir, 'chromium'))) {
      for (const sub of readdirSync(path.join(dir, 'chromium', build))) {
        const exe = path.join(dir, 'chromium', build, sub, ...RELATIVE.split('/').slice(1))
        if (existsSync(exe)) return exe
      }
    }
  } catch { /* nothing cached */ }
  return null
}

let downloadFailed = false

export function chromiumPath() {
  if (!RELATIVE && !process.env.CHROMIUM_PATH) return null
  if (process.env.CHROMIUM_PATH) return existsSync(process.env.CHROMIUM_PATH) ? process.env.CHROMIUM_PATH : null
  let found = findCached(CACHE)
  if (!found && process.env.BYOAI_EXT_TEST_DOWNLOAD === '1' && !downloadFailed) {
    try {
      execSync(`npx -y @puppeteer/browsers install chromium@latest --path "${CACHE}"`, { stdio: 'inherit' })
      found = findCached(CACHE)
    } catch {
      downloadFailed = true // offline etc.: skip with a reason, never fail the suite
    }
  }
  return found
}

export function pythonPath() {
  if (process.env.BYOAI_PYTHON) return process.env.BYOAI_PYTHON
  const venv = process.platform === 'win32'
    ? path.join(REPO, '.venv', 'Scripts', 'python.exe')
    : path.join(REPO, '.venv', 'bin', 'python')
  return existsSync(venv) ? venv : (process.platform === 'win32' ? 'python' : 'python3')
}

/** Why the browser tests cannot run here, or null when they can. */
export function skipReason() {
  if (process.env.CHROMIUM_PATH && !existsSync(process.env.CHROMIUM_PATH)) {
    return `CHROMIUM_PATH is set but nothing is there: ${process.env.CHROMIUM_PATH}`
  }
  if (!RELATIVE && !process.env.CHROMIUM_PATH) {
    return `no Chromium download for ${process.platform}: set CHROMIUM_PATH`
  }
  if (!chromiumPath()) {
    if (downloadFailed) return 'the Chromium download failed (offline?): set CHROMIUM_PATH instead'
    return 'no Chromium: set CHROMIUM_PATH, or BYOAI_EXT_TEST_DOWNLOAD=1 to fetch one'
  }
  return null
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/** Wait until something answers HTTP on the port (Shield is up), or throw. */
export async function waitForServer(port, timeoutMs = 20_000) {
  const stop = Date.now() + timeoutMs
  while (Date.now() < stop) {
    const up = await new Promise((resolve) => {
      const req = http.get({ host: '127.0.0.1', port, path: '/api/identity?nonce=up', timeout: 1000 },
        (res) => { res.resume(); resolve(true) })
      req.on('error', () => resolve(false))
      req.on('timeout', () => { req.destroy(); resolve(false) })
    })
    if (up) return
    await sleep(150)
  }
  throw new Error(`nothing listening on ${port} after ${timeoutMs} ms`)
}

/** Wait until nothing listens on the port, so a restart can take it. */
export async function waitForPortFree(port, timeoutMs = 10_000) {
  await until(() => new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: '/', timeout: 500 },
      (res) => { res.resume(); resolve(false) })
    req.on('error', () => resolve(true))
    req.on('timeout', () => { req.destroy(); resolve(false) })
  }), timeoutMs, `port ${port} to free up`)
}

/** Poll until `check()` returns truthy, instead of sleeping a fixed time. */
export async function until(check, timeoutMs = 15_000, what = 'condition') {
  const stop = Date.now() + timeoutMs
  for (;;) {
    const v = await check()
    if (v) return v
    if (Date.now() > stop) throw new Error(`timed out waiting for ${what}`)
    await sleep(150)
  }
}
