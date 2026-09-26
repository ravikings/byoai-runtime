import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { setupServer } from 'msw/node'
import { http, HttpResponse } from 'msw'

import { AdminAuthProvider, AdminTokenGate } from './AdminAuth'
import { DevicesPanel } from './DevicesPanel'
import { renderWithClient } from './testUtils'
import { shieldServerHandlers, resetShieldServerMocks } from '@/mocks/shieldServerHandlers'
import { ADMIN_TOKEN } from '@/mocks/shieldServerFixtures'
import { clearStoredAdminToken, getStoredAdminToken } from '@/api/shieldServer'

const BASE = '/api/v1/shield'
const server = setupServer(...shieldServerHandlers)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetShieldServerMocks()
  clearStoredAdminToken()
  cleanup()
})
afterAll(() => server.close())

function App() {
  return renderWithClient(
    <AdminAuthProvider>
      <AdminTokenGate>
        <DevicesPanel />
      </AdminTokenGate>
    </AdminAuthProvider>,
  )
}

describe('admin token gate', () => {
  it('prompts for the admin token on a 401', async () => {
    render(<App />)
    expect(await screen.findByRole('dialog', { name: /admin token required/i })).toBeTruthy()
  })

  it('attaches the submitted token as an Authorization header on subsequent calls', async () => {
    let sawAuth: string | null = null
    server.use(
      http.get(`${BASE}/devices`, ({ request }) => {
        sawAuth = request.headers.get('authorization')
        if (sawAuth !== `Bearer ${ADMIN_TOKEN}`) return new HttpResponse(null, { status: 401 })
        return HttpResponse.json({ devices: [] })
      }),
    )

    render(<App />)
    await screen.findByRole('dialog', { name: /admin token required/i })

    fireEvent.change(screen.getByTestId('admin-token-input'), { target: { value: ADMIN_TOKEN } })
    fireEvent.click(screen.getByRole('button', { name: /continue/i }))

    await waitFor(() => expect(sawAuth).toBe(`Bearer ${ADMIN_TOKEN}`))
    expect(getStoredAdminToken()).toBe(ADMIN_TOKEN)
  })

  it('sign out clears the stored token', async () => {
    render(<App />)
    await screen.findByRole('dialog', { name: /admin token required/i })
    fireEvent.change(screen.getByTestId('admin-token-input'), { target: { value: ADMIN_TOKEN } })
    fireEvent.click(screen.getByRole('button', { name: /continue/i }))

    await waitFor(() => expect(getStoredAdminToken()).toBe(ADMIN_TOKEN))

    fireEvent.click(await screen.findByRole('button', { name: /sign out/i }))
    expect(getStoredAdminToken()).toBeNull()
    expect(await screen.findByRole('dialog', { name: /admin token required/i })).toBeTruthy()
  })
})
