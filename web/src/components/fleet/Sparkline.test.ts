import { describe, expect, it } from 'vitest'
import { trailingFlatRun } from './Sparkline'

/**
 * The flat-run logic in Sparkline has been wrong four separate times — once
 * silently dropping the marker, once fabricating a crash from a live data
 * point, once printing "these buckets are not equal" over identical buckets,
 * and once drawing a healthy opening segment on a series that was flat
 * throughout. Every one of those was an off-by-one or a clamp, and every one
 * was found by a reviewer rather than by the code. These pin the boundaries.
 */
describe('trailingFlatRun', () => {
  it('reports no run for a series that is still moving', () => {
    expect(trailingFlatRun([1, 2, 3, 4])).toBe(1)
  })

  it('counts only the trailing equal buckets, not equal ones earlier', () => {
    expect(trailingFlatRun([5, 5, 9, 3, 3, 3])).toBe(3)
  })

  it('spans the whole series when every bucket is identical', () => {
    // The case that produced a false "buckets are not equal" caveat: the run
    // may legitimately equal the series length, so no clamp may cut it short.
    expect(trailingFlatRun([0, 0, 0, 0])).toBe(4)
  })

  it('spans both buckets of a two-point flat series', () => {
    // The shortest series that can be flat, and the one most likely right
    // after a stall begins.
    expect(trailingFlatRun([7, 7])).toBe(2)
  })

  it('reports nothing for a series too short to have a run', () => {
    expect(trailingFlatRun([7])).toBe(0)
    expect(trailingFlatRun([])).toBe(0)
  })
})
