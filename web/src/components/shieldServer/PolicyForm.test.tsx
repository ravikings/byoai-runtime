import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { PolicyForm, defaultPolicyDoc } from './PolicyForm'

describe('PolicyForm', () => {
  it('serializes a lock checkbox toggle into the `locked` array', () => {
    const onChange = vi.fn()
    render(
      <PolicyForm value={{ policy: defaultPolicyDoc(), locked: [] }} onChange={onChange} />,
    )
    fireEvent.click(screen.getByLabelText(/lock mode for devices/i))
    expect(onChange).toHaveBeenCalledWith({ policy: defaultPolicyDoc(), locked: ['mode'] })
  })

  it('unlocking removes the key from `locked`', () => {
    const onChange = vi.fn()
    render(
      <PolicyForm value={{ policy: defaultPolicyDoc(), locked: ['mode', 'notice'] }} onChange={onChange} />,
    )
    fireEvent.click(screen.getByLabelText(/lock mode for devices/i))
    expect(onChange).toHaveBeenCalledWith({
      policy: defaultPolicyDoc(),
      locked: ['notice'],
    })
  })

  it('shows a validation error for an out-of-range retention', () => {
    render(
      <PolicyForm
        value={{
          policy: { ...defaultPolicyDoc(), retention_days: 14 as never },
          locked: [],
        }}
        onChange={() => {}}
      />,
    )
    expect(screen.getByRole('alert').textContent ?? '').toMatch(/retention must be/i)
  })

  it('disables a fieldset whose key is in disabledKeys', () => {
    render(
      <PolicyForm
        value={{ policy: defaultPolicyDoc(), locked: [] }}
        onChange={() => {}}
        disabledKeys={new Set(['mode'])}
      />,
    )
    const radios = screen.getAllByRole('radio') as HTMLInputElement[]
    for (const r of radios) expect(r.disabled).toBe(true)
  })
})
