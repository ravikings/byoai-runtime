/**
 * Appends the active scope to a console path.
 *
 * Every cross-screen link and rail item built a bare path, so navigating away
 * from a scoped view silently reset the scope the operator had just set — they
 * would narrow to three devices over seven days, click through to Coverage,
 * and be shown the whole fleet over 24h with nothing saying it had changed.
 * That directly contradicts the URL-as-addressing design (spec §4.5): a
 * navigation must preserve the slice unless the user asked to widen it.
 *
 * Applied centrally rather than at each call site, because there are ~20 links
 * and the ones that got missed would be exactly the silent-reset bug again.
 */
export function withScope(path: string, params: URLSearchParams | string): string {
  const qs = typeof params === 'string' ? params : params.toString()
  if (qs === '') return path
  return path.includes('?') ? `${path}&${qs}` : `${path}?${qs}`
}
