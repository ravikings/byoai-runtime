/**
 * The console's HTTP client.
 *
 * One rule drives the whole file: a response that does not match the contract
 * in `schemas.ts` is a *distinct kind of failure* from a network drop or a 500,
 * and it must reach the screen as its own visible state (spec §2.3 — a verdict
 * is never rendered without its scope, and a number is never rendered without
 * knowing it is the number the backend promised). So `SchemaMismatchError` is
 * thrown, never logged-and-defaulted, never `catch {}`-ed into a fallback, and
 * never widened with a cast. The product's failure mode is a green tick over
 * data nobody validated; this is the only place that can prevent it.
 */
import type { z } from 'zod'
import { Scope } from './schemas'

/* ------------------------------------------------------------------ *
 * Errors — three kinds, deliberately not one
 * ------------------------------------------------------------------ */

/** The request never produced an HTTP response (offline, DNS, abort, CORS). */
export class NetworkError extends Error {
  readonly kind = 'network'
  constructor(readonly url: string, override readonly cause: unknown) {
    super(`Request to ${url} failed before a response arrived`)
    this.name = 'NetworkError'
  }
}

/** The server answered, and said no. */
export class HttpError extends Error {
  readonly kind = 'http'
  constructor(
    readonly url: string,
    readonly status: number,
    readonly statusText: string,
    readonly body: string,
  ) {
    super(`${status} ${statusText} from ${url}`)
    this.name = 'HttpError'
  }
}

/**
 * The server answered 200 with something that is not what it promised.
 *
 * This is the important one. It is *not* a subclass of HttpError, because
 * callers must be unable to handle it by accident while handling transport
 * failures — the UI has a separate "unexpected response" state for it and the
 * distinction is the point.
 */
export class SchemaMismatchError extends Error {
  readonly kind = 'schema'
  constructor(
    readonly url: string,
    /** zod's own issue list, kept structured so the UI can show the paths. */
    readonly issues: readonly z.ZodIssue[],
    /** The body as received, so an operator can see what actually arrived. */
    readonly received: unknown,
  ) {
    super(
      `Response from ${url} did not match the console API contract ` +
        `(${issues.length} issue${issues.length === 1 ? '' : 's'}): ` +
        issues.map((i) => `${i.path.join('.') || '<root>'}: ${i.message}`).join('; '),
    )
    this.name = 'SchemaMismatchError'
  }
}

/** The body was not JSON at all — an HTML error page, a proxy interstitial. */
export class MalformedResponseError extends Error {
  readonly kind = 'malformed'
  constructor(readonly url: string, readonly body: string, override readonly cause: unknown) {
    super(`Response from ${url} was not valid JSON`)
    this.name = 'MalformedResponseError'
  }
}

export type ApiError =
  | NetworkError
  | HttpError
  | SchemaMismatchError
  | MalformedResponseError

/** Narrowing helper for screens that render the "unexpected response" state. */
export function isSchemaMismatch(e: unknown): e is SchemaMismatchError {
  return e instanceof SchemaMismatchError
}

/* ------------------------------------------------------------------ *
 * Base URL
 * ------------------------------------------------------------------ */

/**
 * Vite replaces `import.meta.env.VITE_API_BASE` at build time. In dev the
 * default is proxied to the Python process by vite.config.ts, so no CORS
 * handling ever lands in the FastAPI app.
 */
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? '/v1/console'

/* ------------------------------------------------------------------ *
 * Scope serialisation — §2.3, exactly one implementation
 * ------------------------------------------------------------------ */

/**
 * The single place a scope becomes a query string. No caller hand-builds one:
 * the scope is mirrored in the URL (§4.5) and shown in the scope chip, and
 * three encodings of "the same" scope would make those three disagree.
 *
 * Arrays are emitted as repeated keys (`device_ids=a&device_ids=b`) rather
 * than a joined string, because a device_id is opaque and could itself contain
 * a separator.
 */
export function scopeToParams(scope: Scope, extra?: Readonly<Record<string, string | number | undefined>>): URLSearchParams {
  const params = new URLSearchParams()
  params.set('tenant', scope.tenant)
  for (const id of scope.device_ids ?? []) params.append('device_ids', id)
  for (const id of scope.agent_ids ?? []) params.append('agent_ids', id)
  if (scope.trajectory_id !== undefined) params.set('trajectory_id', scope.trajectory_id)
  if (scope.from !== undefined) params.set('from', scope.from)
  if (scope.to !== undefined) params.set('to', scope.to)
  if (scope.mandate_version_id !== undefined) params.set('mandate_version_id', scope.mandate_version_id)
  for (const [k, v] of Object.entries(extra ?? {})) {
    if (v !== undefined) params.set(k, String(v))
  }
  // Sorted so the same scope always yields the same URL — this string is the
  // react-query cache key material and the shareable deep link.
  params.sort()
  return params
}

/**
 * The scope key used for query caching and for the URL. Derived from the same
 * serialiser as the request itself so a cache hit can never correspond to a
 * different scope than the one on screen.
 */
export function scopeKey(scope: Scope): string {
  return scopeToParams(scope).toString()
}

/* ------------------------------------------------------------------ *
 * The fetch wrapper
 * ------------------------------------------------------------------ */

export interface RequestOptions {
  readonly scope?: Scope
  /** Non-scope query params (cursor, limit). Merged by `scopeToParams`. */
  readonly query?: Readonly<Record<string, string | number | undefined>>
  readonly signal?: AbortSignal
  readonly method?: 'GET' | 'POST'
  readonly body?: unknown
}

function buildUrl(path: string, options: RequestOptions): string {
  const base = API_BASE.endsWith('/') ? API_BASE.slice(0, -1) : API_BASE
  const suffix = path.startsWith('/') ? path : `/${path}`
  const params = options.scope
    ? scopeToParams(options.scope, options.query)
    : (() => {
        const p = new URLSearchParams()
        for (const [k, v] of Object.entries(options.query ?? {})) {
          if (v !== undefined) p.set(k, String(v))
        }
        p.sort()
        return p
      })()
  const qs = params.toString()
  return qs ? `${base}${suffix}?${qs}` : `${base}${suffix}`
}

/**
 * Fetch `path` and validate the body against `schema`.
 *
 * Returns `z.infer<S>` — the *parsed* value, not the raw body. Callers get a
 * value the type system and the runtime agree on, and there is no code path
 * that returns unvalidated data.
 */
export async function apiFetch<S extends z.ZodTypeAny>(
  path: string,
  schema: S,
  options: RequestOptions = {},
): Promise<z.infer<S>> {
  const url = buildUrl(path, options)

  let response: Response
  try {
    response = await fetch(url, {
      method: options.method ?? 'GET',
      // The proxy app is behind `_proxy_auth_gate`; the console authenticates
      // with the same session cookie rather than a bearer token in JS.
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    })
  } catch (cause) {
    // A cancelled request is not a failure. TanStack Query aborts the previous
    // fetch whenever the scope changes, and it recognises AbortError to discard
    // that result silently. Wrapping it in NetworkError hides the name, so the
    // query settles into an error state and the UI flashes "failed before a
    // response arrived" every time someone changes a filter twice quickly.
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new NetworkError(url, cause)
  }

  // A connection dropped while the body streams rejects here, AFTER fetch()
  // resolved. Unguarded it escaped as a raw TypeError, past all four typed
  // kinds this module promises — callers branching on those saw an
  // unrecognised error and fell through to a generic failure path.
  let text: string
  try {
    text = await response.text()
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new NetworkError(url, cause)
  }

  if (!response.ok) {
    throw new HttpError(url, response.status, response.statusText, text)
  }

  let json: unknown
  try {
    json = JSON.parse(text) as unknown
  } catch (cause) {
    throw new MalformedResponseError(url, text, cause)
  }

  // safeParse, not parse: a ZodError escaping from here would be
  // indistinguishable from any other thrown error at the boundary, and the UI
  // must be able to tell "the backend broke the contract" from "the network
  // broke". This is the one behaviour in the file that must never be relaxed.
  const result = schema.safeParse(json)
  if (!result.success) {
    throw new SchemaMismatchError(url, result.error.issues, json)
  }
  return result.data as z.infer<S>
}
