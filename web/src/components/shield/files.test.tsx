// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Sheet } from './Sheet'
import type { Item } from './shared'

const policy = {
  mode: 'redact', apps: { claude: true }, keep_text: false, retention_days: 30, notice: true,
  actions: { secret: 'block', pii: 'redact', flag: 'log' }, files: { claude: 'block' }, covered_apps: ['claude'],
}
const savePolicy = vi.fn(async (next: object) => ({ ...policy, ...next }))
vi.mock('@/api/shield', async (orig) => ({
  ...(await orig<object>()),
  fetchPolicy: async () => policy,
  fetchInstalledApps: async () => ({}),
  fetchPrivacy: async () => ({}),
  fetchCoriqo: async () => ({}),
  savePolicy: (n: object) => savePolicy(n),
}))
import { Settings } from './Settings'

afterEach(cleanup)

const fileItem = {
  id: 'i1', tier: 'bad', status: 'blocked', surface: 'Claude', source: 'browser', date: 'today', ts: '10:00',
  verb: 'Uploaded a file', flags: [{ tier: 'high', rule: 'aws_access_key' }], seal: 'abc',
  file: { mime: 'text/plain', bytes: 40, sha256: 'a'.repeat(64), scanned: true },
} as unknown as Item

describe('Sheet for a file row', () => {
  it('shows the rule once and has no literal backticks', () => {
    const qc = new QueryClient()
    const { container } = render(<QueryClientProvider client={qc}><Sheet item={fileItem} onClose={() => {}} /></QueryClientProvider>)
    expect(screen.getAllByText('AWS access key')).toHaveLength(1)
    const prose = container.querySelector('.sheet')!.textContent!.replace(/The record as stored[\s\S]*$/, '')
    expect(prose).not.toContain('`')
    expect(container.querySelector('code')?.textContent).toBe('shasum -a 256')
  })
})

describe('Settings: less strict asks once and sends the acknowledgement', () => {
  it('a lowered file setting opens one dialog and confirm sends acknowledge', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><Settings /></QueryClientProvider>)
    const sel = await screen.findByLabelText('Claude: files')
    fireEvent.change(sel, { target: { value: 'allow' } })
    expect(await screen.findByText('Make Shield less strict?')).toBeTruthy()
    expect(savePolicy).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Make less strict'))
    await waitFor(() => expect(savePolicy).toHaveBeenCalledWith(
      { files: { claude: 'allow' }, acknowledge: 'less_private' }))
  })
})
