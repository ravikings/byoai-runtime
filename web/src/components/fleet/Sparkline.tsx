const W = 300
const H = 40
const TOP = 6
const BASE = 39

/** Trailing run of identical values — the flat run. A stalled recorder ships
 *  the same bucket value over and over; that is an incident, not calm. */
export function trailingFlatRun(series: readonly number[]): number {
  if (series.length < 2) return 0
  const last = series[series.length - 1]
  if (last === undefined) return 0
  let run = 1
  for (let i = series.length - 2; i >= 0; i -= 1) {
    if (series[i] !== last) break
    run += 1
  }
  return run
}

function points(series: readonly number[]): { x: number; y: number }[] {
  if (series.length === 0) return []
  let max = 0
  for (const v of series) if (v > max) max = v
  const span = max === 0 ? 1 : max
  const step = series.length === 1 ? 0 : W / (series.length - 1)
  return series.map((v, i) => ({
    x: Number((i * step).toFixed(2)),
    y: Number((BASE - (v / span) * (BASE - TOP)).toFixed(2)),
  }))
}

function polyline(pts: readonly { x: number; y: number }[]): string {
  return pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${p.y}`).join(' ')
}

export interface SparklineProps {
  series: readonly number[]
  /** Marks the tail as flat when non-null; the flat run is drawn distinctly. */
  flatForMinutes?: number | null
  tone?: 'default' | 'warn'
  label: string
}

/**
 * A rate line where a flat run renders as a visually different mark (dashed,
 * off-scope coloured, with a stop dot) rather than as a straight bit of the
 * same line. Colour is never the only carrier — the caption and the tag beside
 * the panel head say "flat" in words.
 */
export function Sparkline({ series, flatForMinutes = null, tone = 'default', label }: SparklineProps) {
  const pts = points(series)
  if (pts.length < 2) {
    return (
      <div className="caveat" role="img" aria-label={`${label} — not enough data to draw a rate`}>
        Not enough buckets in this window to draw a rate. No line is not a flat line.
      </div>
    )
  }
  /**
   * Three distinct cases, spelled out because patching this line by line has
   * produced a wrong answer four times running:
   *
   *   1. Not flat            — `flatForMinutes` is null. Draw the whole series live.
   *   2. Partly flat         — a trailing run of equal buckets. Draw the leading
   *                            part live, then the flat marker across the tail.
   *   3. Flat for the window — the run covers every point. There is NO live part.
   *                            Forcing two points into a "live" segment here drew a
   *                            solid healthy-looking opening on a graph that was dead
   *                            the whole time, contradicting the "rate flat" tag beside it.
   *
   * `flatForMinutes` is the backend's authoritative judgement that ingest has
   * stalled; bucket equality only decides how much can honestly be DRAWN as
   * flat. When the two disagree (jitter, rounding, bucket-granularity
   * mismatch), say so in words rather than inventing a shape — an earlier
   * revision forced the mark on and drew a vertical crash from a real,
   * elevated data point.
   */
  // No clamp: `pts` is built 1:1 from `series`, so the run can never exceed
  // pts.length. A defensive Math.min here reads as load-bearing and is exactly
  // the kind of extra bound that produced four separate off-by-ones in this
  // function. The invariant is the point — state it, don't re-guard it.
  const flatRun = flatForMinutes === null ? 0 : trailingFlatRun(series)
  const disagrees = flatForMinutes !== null && flatRun < 2
  const wholeFlat = flatRun > 1 && flatRun >= pts.length
  const liveCount = wholeFlat ? 0 : flatRun > 1 ? pts.length - (flatRun - 1) : pts.length
  const live = liveCount >= 2 ? pts.slice(0, liveCount) : []
  const first = pts[0]
  const stop = live.length > 0 ? live[live.length - 1] : first
  const lastX = pts[pts.length - 1]?.x ?? W

  return (
    <>
      {disagrees ? (
        <div className="caveat" style={{ marginBottom: 'var(--s2)' }}>
          Backend reports the rate flat for {flatForMinutes}m, but these buckets are not
          equal — the line below is drawn from the values as received, not as a flat run.
        </div>
      ) : null}
      <svg
        className={tone === 'warn' ? 'spark warn' : 'spark'}
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={wholeFlat ? `${label} — flat for the whole window` : label}
      >
        {live.length >= 2 && stop !== undefined ? (
          <>
            <path
              className="area"
              d={`${polyline(live)} L${stop.x},${H} L${live[0]?.x ?? 0},${H} Z`}
            />
            <path className="line" d={polyline(live)} />
          </>
        ) : null}
        {wholeFlat && first !== undefined ? (
          // Nothing live to show: the flat mark spans the entire window at the
          // series' own constant value, so the graph reads as dead throughout
          // rather than healthy-then-stalled.
          <>
            <path className="flat" d={`M${first.x},${first.y} L${lastX},${first.y}`} />
            <circle className="mark" cx={first.x} cy={first.y} r={2.5} />
          </>
        ) : flatRun > 1 && stop !== undefined ? (
          <>
            <path
              className="flat"
              // Drawn at the run's OWN height, not the baseline. Anchoring to
              // BASE made a rate stuck at a nonzero constant — a stuck
              // recorder, arguably worse than silence — render identically to
              // one that fell to zero. The fixtures only masked this because
              // their flat tail happens to be zero.
              d={`M${stop.x},${stop.y} L${lastX},${stop.y}`}
            />
            <circle className="mark" cx={stop.x} cy={stop.y} r={2.5} />
          </>
        ) : null}
        <line className="base" x1={0} y1={BASE + 0.5} x2={W} y2={BASE + 0.5} />
      </svg>
    </>
  )
}
