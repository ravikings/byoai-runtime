/**
 * One duration formatter for the whole console.
 *
 * This existed twice, once per screen, with a quietly different sub-minute
 * branch: one returned "45s", the other "0m" — which the very docstring above
 * it promised never to produce. Two formatters for one concept drift, and on
 * this product they drift into telling an operator a device has been quiet for
 * zero minutes when it has been quiet for forty-five seconds.
 */

/** Largest two units that still carry information: "85d 06h", "4h 51m", "41m", "45s". */
export function duration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  const days = Math.floor(s / 86_400)
  const hours = Math.floor((s % 86_400) / 3_600)
  const minutes = Math.floor((s % 3_600) / 60)
  if (days > 0) return `${days}d ${String(hours).padStart(2, '0')}h`
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`
  if (minutes > 0) return `${minutes}m`
  // Never "0m": a sub-minute silence is still a silence, and rounding it to
  // zero reads as "nothing to see here".
  return `${s}s`
}
