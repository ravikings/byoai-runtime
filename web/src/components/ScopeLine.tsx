/**
 * The scope line every data screen carries — "what am I looking at" (§4.1).
 * A number with no stated scope is a number a user cannot act on, so this
 * strip restates the slice in full: tenant, selection, window, and the
 * included/enrolled fraction that every aggregate on the screen is over.
 *
 * The inclusion fraction is rendered here even when it is complete: an
 * operator should not have to infer from the absence of a warning that a
 * figure covers everything.
 */
import type { Inclusion } from '../api/schemas'
import { type ScopeSearch, scopeSubject, windowLabel } from '../app/scope'

export interface ScopeLineProps {
  readonly tenant: string
  readonly search: ScopeSearch
  readonly inclusion?: Inclusion | undefined
  /** Anything screen-specific: "entries_checked 12,904", "seq 1204–8391". */
  readonly extra?: readonly string[] | undefined
}

export function ScopeLine({ tenant, search, inclusion, extra }: ScopeLineProps) {
  const excluded = inclusion
    ? inclusion.devices_enrolled - inclusion.devices_included
    : 0
  return (
    <div className="scope" aria-label="Scope of the figures on this screen">
      <span>
        tenant <b>{tenant}</b>
      </span>
      <span>
        scope <b>{scopeSubject(search)}</b>
        {/* Only say "no filter" when nothing at all is restricting the
            figures. A trajectory or mandate scope narrows every number on
            screen, so printing this beside one reads as a claim that the view
            is unfiltered when it is not. */}
        {search.devices || search.agents || search.trajectory || search.mandate
          ? null
          : ' (no device_ids filter)'}
      </span>
      <span>
        window <b>{windowLabel(search)}</b>
      </span>
      {inclusion ? (
        <span>
          devices_included <b>{inclusion.devices_included}</b> /
          devices_enrolled <b>{inclusion.devices_enrolled}</b>
          {excluded > 0
            ? ` — ${excluded} enrolled ${
                excluded === 1 ? 'device is' : 'devices are'
              } not counted here`
            : null}
        </span>
      ) : (
        <span>
          devices_included <b>unknown</b> — no coverage figure has loaded
        </span>
      )}
      {extra?.map((e) => <span key={e}>{e}</span>)}
    </div>
  )
}
