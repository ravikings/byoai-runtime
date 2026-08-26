import { formatCount } from '../../lib/number'
export { duration } from '../../lib/duration'
/**
 * Formatting for the silence report.
 *
 * Every helper here has the same bias: an absent value must never come out
 * looking like a small one. `null` returns an explicit unknown marker for the
 * caller to render loudly, never "0", never an em-dash sitting quietly in a
 * cell.
 */

/** Thousands-separated integer. Seqs and counts are read, not skimmed. */
export function num(n: number): string {
  return formatCount(n)
}

/** Compact headline figure: 1,448,135 → "1.4M". Full value goes beside it. */
export function compact(n: number): string {
  if (n < 1_000) return String(n)
  // Branch on the ROUNDED value, not the raw one: 999,999 is below the
  // megabyte cut-off but rounds to 1000k, which reads as a formatting fault on
  // a headline count.
  if (n < 1_000_000) {
    const k = n / 1_000
    const rendered = k.toFixed(n < 10_000 ? 1 : 0)
    if (Number(rendered) < 1_000) return `${rendered}k`
  }
  return `${(n / 1_000_000).toFixed(1)}M`
}


/** An observed cadence, phrased the way an operator would say it. */
export function cadence(intervalSeconds: number): string {
  if (intervalSeconds >= 82_800 && intervalSeconds <= 100_800) return 'daily'
  if (intervalSeconds >= 3_600) return `every ${Math.round(intervalSeconds / 3_600)}h`
  if (intervalSeconds >= 60) return `every ${Math.round(intervalSeconds / 60)}m`
  return `every ${Math.round(intervalSeconds)}s`
}

/** "76×", "1.3×". Below 10 the fraction is the difference between classes. */
export function multiple(x: number): string {
  return `${x < 10 ? x.toFixed(1) : Math.round(x)}×`
}

/** ISO timestamp trimmed to the console's house format, kept mono elsewhere. */
export function ts(iso: string): string {
  return iso.replace('T', ' ').replace(/\.\d+/, '').replace('+00:00', 'Z')
}

/**
 * Where the "due" marker sits on a cadence track whose full width is the
 * observed silence. At 76× overdue the marker is pinned near the left edge;
 * it never leaves the track entirely, because a marker off-screen reads as
 * no deadline at all.
 */
export function duePercent(overdueMultiple: number): number {
  if (overdueMultiple <= 1) return 100
  return Math.max(1, 100 / overdueMultiple)
}
