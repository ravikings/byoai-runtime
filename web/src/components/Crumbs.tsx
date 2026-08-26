/**
 * Breadcrumbs — "where am I", the first of the four orientation questions
 * (§4.1). Present on every screen; every segment but the last is a link back,
 * so browser back is never the only way out.
 */
import { Fragment } from 'react'

export interface Crumb {
  readonly label: string
  /** Absent on the final segment — you are already there. */
  readonly href?: string
}

export interface CrumbsProps {
  readonly items: readonly Crumb[]
  readonly onNavigate?: ((href: string) => void) | undefined
}

export function Crumbs({ items, onNavigate }: CrumbsProps) {
  return (
    <nav className="crumbs" aria-label="Breadcrumb">
      {items.map((c, i) => {
        const last = i === items.length - 1
        return (
          <Fragment key={`${c.label}-${i}`}>
            {i > 0 ? (
              <span className="sep" aria-hidden="true">
                /
              </span>
            ) : null}
            {c.href && !last ? (
              <a
                href={c.href}
                onClick={(e) => {
                  const href = c.href
                  if (
                    !onNavigate ||
                    href === undefined ||
                    e.metaKey ||
                    e.ctrlKey ||
                    e.shiftKey ||
                    e.button !== 0
                  )
                    return
                  e.preventDefault()
                  onNavigate(href)
                }}
              >
                {c.label}
              </a>
            ) : (
              <span className="here" aria-current={last ? 'page' : undefined}>
                {c.label}
              </span>
            )}
          </Fragment>
        )
      })}
    </nav>
  )
}
