import { formatCount } from '../../lib/number'
import { duration } from '../../lib/duration'
export { duration }
/**
 * Formatting helpers for the fleet overview.
 *
 * Every one of these has a defined answer for the missing case, because a
 * blank where a duration should be reads as "fine" and it never is.
 */


export function n(value: number): string {
  return formatCount(value)
}

export function rate(value: number): string {
  return value.toFixed(1)
}


/** Seconds elapsed since an ISO timestamp, or null when the stamp is absent. */
export function secondsSince(iso: string | null, now: number = Date.now()): number | null {
  if (iso === null) return null
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  return (now - t) / 1000
}

/** "41m ago" — or the honest word when there is no timestamp at all. */
export function ago(iso: string | null, now: number = Date.now()): string {
  const s = secondsSince(iso, now)
  if (s === null) return 'never'
  // duration() floors negatives to zero, so a server clock ahead of the browser
  // would render a future stamp as "0s ago" — the freshest reading possible,
  // which is precisely backwards. Name the skew instead of hiding it.
  if (s < 0) return `${duration(-s)} in the future (clock skew)`
  return `${duration(s)} ago`
}

export function clock(iso: string): string {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  return new Date(t).toISOString().slice(11, 16)
}

export function stamp(iso: string): string {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  return `${new Date(t).toISOString().slice(0, 16).replace('T', ' ')}`
}

/** Route hrefs. Plain strings so this screen does not depend on the generated
 *  route tree for links to screens that may not be built yet. */
export const href = {
  coverage: (tenant: string) => `/console/${tenant}/fleet/coverage`,
  devices: (tenant: string) => `/console/${tenant}/fleet/devices`,
  device: (tenant: string, deviceId: string) =>
    `/console/${tenant}/fleet/devices/${encodeURIComponent(deviceId)}`,
  entry: (tenant: string, deviceId: string, seq: number) =>
    `/console/${tenant}/entries/${encodeURIComponent(deviceId)}/${seq}`,
  session: (tenant: string, deviceId: string, sessionId: string) =>
    `/console/${tenant}/sessions/${encodeURIComponent(deviceId)}/${encodeURIComponent(sessionId)}`,
  verdicts: (tenant: string) => `/console/${tenant}/mandate/verdicts`,
  findings: (tenant: string) => `/console/${tenant}/evidence/findings`,
  verifyUnverified: (tenant: string) => `/console/${tenant}/evidence/verify?scope=unverified`,
  enrollment: (tenant: string) => `/console/${tenant}/settings/enrollment`,
}
