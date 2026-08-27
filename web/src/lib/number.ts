/**
 * One thousands-separator formatter for the whole console.
 *
 * This existed twice — `n()` on the fleet screens via `Intl.NumberFormat`,
 * `num()` on the coverage screens via `toLocaleString` — which is the same
 * split that already produced two `duration()`s that disagreed. Counts on
 * these screens are read against each other; they cannot be allowed to drift
 * into different formatting.
 */
const NUM = new Intl.NumberFormat('en-US')

export function formatCount(value: number): string {
  return NUM.format(value)
}
