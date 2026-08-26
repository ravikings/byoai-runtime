/**
 * `/` — Fleet is the default landing surface (§6.0), so the bare root is a
 * redirect, never a screen of its own. A console whose home page is a chooser
 * is a console that makes you decide before it has told you anything.
 */
import { createFileRoute, redirect } from '@tanstack/react-router'
import { DEFAULT_TENANT } from '../app/scope'

export const Route = createFileRoute('/')({
  beforeLoad: ({ search }) => {
    // Carry the search through; see console.index.tsx for why.
    throw redirect({ to: '/console/$tenant/fleet', params: { tenant: DEFAULT_TENANT }, search })
  },
})
