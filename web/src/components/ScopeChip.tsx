/**
 * The scope chip — the most important control in the product (spec §6.1).
 *
 * It states what every number on screen is counted over. Two rules, both
 * structural rather than stylistic:
 *
 *  1. It is never empty. An unfiltered scope still says "all devices"; a
 *     scope with no loaded data still says what it selects and admits it does
 *     not yet know the denominator.
 *  2. When the current scope excludes enrolled devices, it says so IN THE
 *     CHIP — `.scope-chip.partial` plus the fraction in words — never in a
 *     tooltip. The figure beside it is wrong by exactly that much, and a
 *     caveat you have to hover for is a caveat nobody reads.
 */
import { useEffect, useId, useRef, useState } from 'react'
import type { Inclusion } from '../api/schemas'
import {
  DEFAULT_WINDOW,
  WINDOWS,
  WINDOW_LABEL,
  type ScopeSearch,
  type Window,
  scopeSubject,
  windowLabel, csvList } from '../app/scope'

export interface ScopeChipProps {
  readonly search: ScopeSearch
  /** Undefined means no page has reported a denominator yet — say so. */
  readonly inclusion?: Inclusion | undefined
  readonly onChange: (next: ScopeSearch) => void
}

/** The chip's own sentence, and whether it must wear the partial styling. */
export function scopeChipText(
  search: ScopeSearch,
  inclusion?: Inclusion,
): { text: string; partial: boolean; reading: string } {
  const subject = scopeSubject(search)
  if (!inclusion) {
    return {
      text: `${subject} · coverage unknown`,
      partial: false,
      reading:
        'No screen has reported a device count yet, so the denominator for these numbers is unknown.',
    }
  }
  const { devices_included, devices_enrolled } = inclusion
  const excluded = devices_enrolled - devices_included
  const text = `${subject} · ${devices_included} of ${devices_enrolled} reporting`
  if (excluded > 0) {
    return {
      text,
      partial: true,
      reading: `${excluded} enrolled ${
        excluded === 1 ? 'device is' : 'devices are'
      } excluded from every number on this screen.`,
    }
  }
  return {
    text,
    partial: false,
    reading: 'Every enrolled device is included in these numbers.',
  }
}

export function ScopeChip({ search, inclusion, onChange }: ScopeChipProps) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const chipRef = useRef<HTMLButtonElement | null>(null)
  const popId = useId()
  const { text, partial, reading } = scopeChipText(search, inclusion)

  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setOpen(false)
        chipRef.current?.focus()
      }
    }
    function onDown(e: MouseEvent) {
      const t = e.target
      if (t instanceof Node && wrapRef.current && !wrapRef.current.contains(t)) {
        setOpen(false)
      }
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
    }
  }, [open])

  return (
    <div className="scope-pop-wrap" ref={wrapRef}>
      <button
        ref={chipRef}
        type="button"
        className={partial ? 'scope-chip partial' : 'scope-chip'}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls={open ? popId : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        {/* The excluded count is stated, not signalled by colour alone. */}
        {text}
        <span className="mono dim">· {windowLabel(search)}</span>
        <span className="caret" aria-hidden="true">
          ▾
        </span>
      </button>
      {/* Read out on focus by screen readers, and visible in the picker. */}
      <span className="sr-only">{reading}</span>
      {open ? (
        <ScopePicker
          id={popId}
          search={search}
          reading={reading}
          onChange={(next) => {
            onChange(next)
            setOpen(false)
            chipRef.current?.focus()
          }}
          onClose={() => {
            setOpen(false)
            chipRef.current?.focus()
          }}
        />
      ) : null}
    </div>
  )
}

interface PickerProps {
  readonly id: string
  readonly search: ScopeSearch
  readonly reading: string
  readonly onChange: (next: ScopeSearch) => void
  readonly onClose: () => void
}

function ScopePicker({ id, search, reading, onChange, onClose }: PickerProps) {
  const [devices, setDevices] = useState(search.devices?.join(', ') ?? '')
  const [agents, setAgents] = useState(search.agents?.join(', ') ?? '')
  const firstRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    firstRef.current?.focus()
  }, [])

  /**
   * `window` set means the user clicked a window pill, which is a
   * change-the-window gesture, not an Apply. Reading the id inputs there
   * committed whatever was half-typed — 'd1-ops-run' became a real filter, the
   * fleet narrowed to a device that does not exist, and every figure went to
   * zero without the user ever pressing Apply.
   */
  function commit(window?: Window) {
    const pillOnly = window !== undefined
    const next: {
      devices?: string[]
      agents?: string[]
      trajectory?: string
      window?: Window
      from?: string
      to?: string
      mandate?: string
    } = {}
    const d = pillOnly ? search.devices : csvList(devices)
    if (d) next.devices = d
    const a = pillOnly ? search.agents : csvList(agents)
    if (a) next.agents = a
    if (search.trajectory) next.trajectory = search.trajectory
    if (search.mandate) next.mandate = search.mandate
    const w = window ?? search.window
    if (w) next.window = w
    // Choosing a named window drops any pinned absolute bounds — keeping both
    // would show a window the URL does not actually mean.
    if (!window && search.from) next.from = search.from
    if (!window && search.to) next.to = search.to
    onChange(next)
  }

  return (
    <div
      id={id}
      className="panel scope-pop"
      role="dialog"
      aria-label="Scope picker"
    >
      <h3 className="label">Scope</h3>
      <p className="muted scope-pop-reading">{reading}</p>

      <h3 className="label">Window</h3>
      <div className="row scope-pop-row">
        {WINDOWS.map((w, i) => {
          // Either explicit bound means an absolute range governs the data, so
          // no named window is in force. Checking only `from` left the pill
          // pressed while a `to`-only URL actually decided the slice.
          const absolute = search.from !== undefined || search.to !== undefined
          const active = (search.window ?? DEFAULT_WINDOW) === w && !absolute
          return (
            <button
              key={w}
              ref={i === 0 ? firstRef : undefined}
              type="button"
              className={active ? 'btn sm on' : 'btn sm'}
              aria-pressed={active}
              onClick={() => commit(w)}
            >
              {WINDOW_LABEL[w]}
            </button>
          )
        })}
      </div>

      <h3 className="label">Devices</h3>
      <input
        className="scope-pop-input mono"
        value={devices}
        placeholder="all devices — or d1-ops-runner-02, d1-batch-worker-07"
        onChange={(e) => setDevices(e.target.value)}
        aria-label="Device ids, comma separated"
      />

      <h3 className="label">Agents</h3>
      <input
        className="scope-pop-input mono"
        value={agents}
        placeholder="all agents — or agt_4d19b7c2"
        onChange={(e) => setAgents(e.target.value)}
        aria-label="Agent ids, comma separated"
      />

      <div className="row scope-pop-row">
        <button type="button" className="btn sm primary" onClick={() => commit()}>
          Apply
        </button>
        <button
          type="button"
          className="btn sm"
          // Clears the device/agent filter ONLY. Dropping trajectory or
          // mandate here would silently widen which trajectory or mandate
          // version every figure on screen refers to — the one thing this
          // control exists to make impossible.
          onClick={() =>
            onChange({
              window: search.window ?? DEFAULT_WINDOW,
              ...(search.from === undefined ? {} : { from: search.from }),
              ...(search.to === undefined ? {} : { to: search.to }),
              ...(search.trajectory === undefined ? {} : { trajectory: search.trajectory }),
              ...(search.mandate === undefined ? {} : { mandate: search.mandate }),
            })
          }
        >
          Reset to all devices
        </button>
        <div className="spacer" />
        <button type="button" className="btn sm ghost" onClick={onClose}>
          Close <kbd>esc</kbd>
        </button>
      </div>
      <p className="muted scope-pop-reading">
        The scope filters within this tenant and can never widen past it.
      </p>
    </div>
  )
}

