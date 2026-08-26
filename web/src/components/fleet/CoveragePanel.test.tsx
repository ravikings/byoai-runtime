import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { CoveragePanel } from './CoveragePanel'
import { FLEET_SUMMARY } from '@/mocks/fixtures'

/**
 * These pin the one invariant this console keeps re-breaking: an unknown or
 * unloaded state must never render as a settled, reassuring number.
 *
 * The specific regression: the "Silent devices N" heading was switched to
 * count the rows it renders — correct while the device register loads fine,
 * but the row list is also empty when that query FAILS, so the heading
 * asserted "0" directly above a banner naming devices it could not list.
 */
describe('CoveragePanel silent-devices heading', () => {
  it('does not claim zero silence when the device register failed to load', () => {
    render(
      <CoveragePanel
        summary={FLEET_SUMMARY}
        tenant="acme-prod"
        devices={undefined}
        devicesProblem="failed"
      />,
    )
    expect(screen.getByText(/the register did not load/i)).toBeDefined()
    expect(screen.queryByText(/^\s*0\s*→\s*$/)).toBeNull()
  })

  it('does not claim zero silence while the register is still loading', () => {
    render(
      <CoveragePanel
        summary={FLEET_SUMMARY}
        tenant="acme-prod"
        devices={undefined}
        devicesProblem="loading"
      />,
    )
    expect(screen.queryByText(/^\s*0\s*→\s*$/)).toBeNull()
  })

  it('counts the rows it renders once the register is present', () => {
    render(
      <CoveragePanel
        summary={FLEET_SUMMARY}
        tenant="acme-prod"
        devices={[]}
        devicesProblem={null}
      />,
    )
    // An empty register that LOADED is a real zero, and may say so.
    expect(screen.queryByText(/the register did not load/i)).toBeNull()
  })
})
