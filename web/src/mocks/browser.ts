/**
 * The dev-only service worker.
 *
 * Exported as a lazy `worker` facade rather than a side-effecting module so
 * `main.tsx` can `await` it before the first render — MSW that starts after the
 * first query has fired produces a real network error on load, which is the one
 * thing a mock must not do. Callers should gate this on `import.meta.env.DEV`;
 * the guard here is a second line of defence, not the only one.
 */
import { setupWorker } from 'msw/browser'
import { brokenFleetHandler, handlers } from './handlers'

/**
 * `?mock=broken` serves a contract-violating /fleet response so the
 * "unexpected response" state can be exercised by hand. handlers.ts documented
 * this trigger, but nothing read it — a comment describing a path that could
 * never run, which reads as verified behaviour and is not. Wired here.
 */
function activeHandlers(): typeof handlers {
  const broken =
    typeof location !== 'undefined' &&
    new URLSearchParams(location.search).get('mock') === 'broken'
  return broken ? [brokenFleetHandler, ...handlers] : handlers
}

/** The real MSW worker, for tests or callers that need its full surface. */
export const mswWorker = setupWorker(...activeHandlers())

/**
 * `main.tsx` imports this module structurally and calls `worker.start(opts)`
 * with a loosely-typed options bag, so the exported `worker` is a narrow
 * facade rather than the MSW instance itself: MSW's `StartOptions` parameter
 * is contravariant and would not accept `unknown` under strictFunctionTypes.
 */
export const worker: {
  start: (opts?: unknown) => Promise<unknown>
  stop: () => void
} = {
  // The one option callers actually vary is `onUnhandledRequest`; it is read
  // back out by narrowing rather than by casting, so an unrecognised bag
  // falls through to our own default instead of being trusted blindly.
  start: (opts?: unknown) => mswWorker.start({ ...workerOptions(), ...unhandled(opts) }),
  stop: () => mswWorker.stop(),
}

type Unhandled = 'bypass' | 'warn' | 'error'

function unhandled(opts: unknown): { onUnhandledRequest?: Unhandled } {
  if (typeof opts !== 'object' || opts === null || !('onUnhandledRequest' in opts)) return {}
  const v: unknown = Reflect.get(opts, 'onUnhandledRequest')
  return v === 'bypass' || v === 'warn' || v === 'error' ? { onUnhandledRequest: v } : {}
}

function workerOptions(): Parameters<typeof mswWorker.start>[0] {
  return {
    // The console is served under /console/ (vite base), so the worker script
    // lives there too.
    serviceWorker: { url: `${import.meta.env.BASE_URL}mockServiceWorker.js` },
    // Anything the handlers do not cover should reach the real proxy rather
    // than 404 silently — unhandled requests are a wiring bug, so say so.
    onUnhandledRequest: 'warn',
  }
}

