import { describe, expect, it } from 'vitest'
import { ShieldFile, ShieldPolicy } from './shield'

describe('file evidence schemas', () => {
  it('accepts a file row and rejects a short or upper-case hash', () => {
    expect(ShieldFile.safeParse({ sha256: 'a'.repeat(64), bytes: 3, scanned: false }).success).toBe(true)
    expect(ShieldFile.safeParse({ sha256: 'A'.repeat(64) }).success).toBe(false)
    expect(ShieldFile.safeParse({ sha256: 'abc' }).success).toBe(false)
  })
  it('reads the per-app files policy', () => {
    const base = { mode: 'redact', apps: {}, keep_text: false, retention_days: 30, notice: true }
    expect(ShieldPolicy.parse({ ...base, files: { claude: 'block' } }).files?.claude).toBe('block')
    expect(ShieldPolicy.safeParse({ ...base, files: { claude: 'log' } }).success).toBe(false)
  })
})
