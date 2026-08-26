/**
 * The three health dots: coverage, integrity, ingest (spec §6.1).
 *
 * Three rules this component exists to enforce:
 *
 *  1. Four states, not two. "recorder disabled", "verify never run" and
 *     "verify failed" are three different facts; `unknown` is a first-class
 *     state and never renders as either a pass or a failure.
 *  2. A dot is a ROLLUP, so it names its worst member — "degraded — 3 of 40
 *     devices silent > 24h" — rather than reporting a mood.
 *  3. Colour is never the only carrier. Every dot ships the state as a word
 *     and the worst member as a sentence, both in the DOM and in the
 *     accessible name.
 *
 * And a dot is a jump link, not status decoration (§4.2 J2): clicking it goes
 * to the degraded thing.
 */
import type { DotState, HealthRollup } from '../app/scope'

export type { HealthRollup }

/** What a dot says before any page has loaded anything. Not a green tick. */
export function unknownRollup(what: string): HealthRollup {
  return {
    state: 'unknown',
    state_label: 'unknown',
    worst: `no ${what} data has been loaded yet`,
  }
}

const STATE_WORD: Record<DotState, string> = {
  ok: 'intact',
  warn: 'degraded',
  bad: 'broken',
  unknown: 'unknown',
}

export interface HealthDotsProps {
  readonly coverage?: HealthRollup | undefined
  readonly integrity?: HealthRollup | undefined
  readonly ingest?: HealthRollup | undefined
  /** Router navigation, so a dot is a jump link without a full page load. */
  readonly onJump?: ((href: string) => void) | undefined
}

export function HealthDots({
  coverage,
  integrity,
  ingest,
  onJump,
}: HealthDotsProps) {
  return (
    <div className="health-dots" role="group" aria-label="Fleet health">
      <HealthDot
        name="coverage"
        rollup={coverage ?? unknownRollup('coverage')}
        onJump={onJump}
      />
      <HealthDot
        name="integrity"
        rollup={integrity ?? unknownRollup('integrity')}
        onJump={onJump}
      />
      <HealthDot
        name="ingest"
        rollup={ingest ?? unknownRollup('ingest')}
        onJump={onJump}
      />
    </div>
  )
}

interface HealthDotProps {
  readonly name: string
  readonly rollup: HealthRollup
  readonly onJump?: ((href: string) => void) | undefined
}

export function HealthDot({ name, rollup, onJump }: HealthDotProps) {
  const word = rollup.state_label || STATE_WORD[rollup.state]
  const label = `${name} ${word} — ${rollup.worst}`
  const body = (
    <>
      <span className={`dot ${rollup.state}`} aria-hidden="true" />
      <span className="what">
        {name} <span className="muted">{word}</span>
      </span>
      {/* The worst member, inline. Clamped, and repeated in full in the card
          below — never hidden behind a hover. */}
      <span className="worst mono dim">{rollup.worst}</span>
    </>
  )

  const card = (
    <span className="health-card" role="note">
      <span className="label">{name}</span>
      <span className="health-card-state">
        <span className={`dot ${rollup.state}`} aria-hidden="true" /> {word}
      </span>
      <span className="muted">{rollup.worst}</span>
      {rollup.href ? <span className="mono dim">{rollup.href}</span> : null}
    </span>
  )

  if (rollup.href) {
    const href = rollup.href
    return (
      <span className="health-dot">
        <a
          className="status"
          href={href}
          aria-label={label}
          onClick={(e) => {
            if (!onJump || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0)
              return
            e.preventDefault()
            onJump(href)
          }}
        >
          {body}
        </a>
        {card}
      </span>
    )
  }

  return (
    <span className="health-dot">
      <span className="status" tabIndex={0} aria-label={label}>
        {body}
      </span>
      {card}
    </span>
  )
}
