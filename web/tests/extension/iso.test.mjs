// The isolated world loads one generated bundle (Chrome injects a file once
// per frame). It must actually run and define all three globals: a missing
// semicolon between the joined IIFEs once made it throw on its first line.
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { REPO } from './env.mjs'

describe('shield-iso.js', () => {
  it('runs and defines rules, sites and the engine', () => {
    const src = readFileSync(`${REPO}/src/byoai/browser_extension/shield-iso.js`, 'utf8')
    const window = {}
    window.window = window
    vm.runInNewContext(src, { window, location: { host: 'claude.ai' } })
    expect(window.__shieldRules).toBeTruthy()
    expect(window.__shieldSites.sites.some((s) => s.hosts.includes('claude.ai'))).toBe(true)
    expect(typeof window.__shieldCore.engine).toBe('function')
  })
})
