import { describe, expect, it } from 'vitest'
import { canonical, checkReceipt } from './receipt'
// Exported by the Python seal chain (byoai.integrations.shield.SealChain),
// so these tests pin the browser check to the code that writes receipts.
import fixture from './__fixtures__/receipt.json'

const good = JSON.stringify(fixture)

describe('checkReceipt', () => {
  it('accepts a receipt exported by the shield', async () => {
    const res = await checkReceipt(good)
    expect(res).toMatchObject({ ok: true, level: 'merkle', root: fixture.merkle.proof.root_hex })
  })

  it('rejects an edited payload', async () => {
    const edited = structuredClone(fixture)
    edited.payload.chars = 999
    const res = await checkReceipt(JSON.stringify(edited))
    expect(res.ok).toBe(false)
    if (!res.ok) expect(res.why).toMatch(/doesn't match its seal/)
  })

  it('rejects an edited proof', async () => {
    const edited = structuredClone(fixture)
    edited.merkle.proof.steps[0]!.sibling = 'f'.repeat(64)
    const res = await checkReceipt(JSON.stringify(edited))
    expect(res.ok).toBe(false)
  })

  it('explains bad input instead of throwing', async () => {
    expect(await checkReceipt('not json')).toMatchObject({ ok: false })
    expect(await checkReceipt('{"kind":"other"}')).toMatchObject({ ok: false })
  })
})

describe('canonical', () => {
  it('sorts keys and uses compact separators', () => {
    expect(canonical({ b: 1, a: [true, null, 'x'] })).toBe('{"a":[true,null,"x"],"b":1}')
  })
})

describe('checkReceipt limits', () => {
  it('declines numbers a browser cannot hold exactly, without calling it tampering', async () => {
    expect(good).toContain('"chars":42')
    const res = await checkReceipt(good.replace('"chars":42', '"chars":12345678901234567890'))
    expect(res.ok).toBe(false)
    if (!res.ok) expect(res.why).toMatch(/too large/)
  })
})
