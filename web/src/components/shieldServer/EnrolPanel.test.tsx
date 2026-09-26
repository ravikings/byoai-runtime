import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { setupServer } from 'msw/node'

import { EnrolPanel } from './EnrolPanel'
import { renderWithClient } from './testUtils'
import { shieldServerHandlers, resetShieldServerMocks } from '@/mocks/shieldServerHandlers'
import { ADMIN_TOKEN } from '@/mocks/shieldServerFixtures'
import { clearStoredAdminToken, setStoredAdminToken } from '@/api/shieldServer'

const server = setupServer(...shieldServerHandlers)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetShieldServerMocks()
  clearStoredAdminToken()
})
afterAll(() => server.close())

describe('EnrolPanel', () => {
  it('shows the minted token once, then it is gone after the component remounts (navigating away)', async () => {
    setStoredAdminToken(ADMIN_TOKEN)
    const { unmount } = render(renderWithClient(<EnrolPanel />))

    fireEvent.change(screen.getByPlaceholderText(/alice/i), {
      target: { value: "Front desk Mac" },
    })
    fireEvent.click(screen.getByRole('button', { name: /mint token/i }))

    const tokenBox = await screen.findByTestId('minted-token')
    // shieldtok_… is the real backend's token prefix (store.generate_token) —
    // not a guess: this must match what create_app() actually mints.
    expect(tokenBox.textContent ?? '').toMatch(/shieldtok_/)

    // Simulate navigating away and back: the route unmounts the panel, so its
    // local `minted` state — which is never persisted anywhere — is gone.
    unmount()
    render(renderWithClient(<EnrolPanel />))

    await waitFor(() => expect(screen.queryByTestId('minted-token')).toBeNull())
  })

  it('lists outstanding tokens and can revoke one', async () => {
    setStoredAdminToken(ADMIN_TOKEN)
    render(renderWithClient(<EnrolPanel />))

    const row = await screen.findByText('Front desk Mac')
    expect(row).toBeTruthy()

    const revokeButtons = screen.getAllByRole('button', { name: /revoke/i })
    expect(revokeButtons.length).toBeGreaterThan(0)
    fireEvent.click(revokeButtons[0]!)
    fireEvent.click(await screen.findByRole('button', { name: /^revoke token$/i }))

    await waitFor(() => expect(screen.getAllByText(/revoked/i).length).toBeGreaterThan(0))
  })
})
