/**
 * Class 3 — accepted but never verified.
 *
 * Stored is not verified. These ranges arrived, were written, and have never
 * had a verify walk run against them: their integrity state is `unverified`,
 * which is a third thing, not a quiet yes.
 *
 * Every range carries its `device_id` in the same row. Seqs are per-device and
 * collide across a fleet — seq 3,001 exists on four of these devices and is
 * four different events.
 */
import type { CSSProperties } from 'react'
import type { CoverageReport } from '@/api/schemas'
import { Methodology, SectionHead, leadStyle, secStyle } from './parts'
import { duration, num } from './format'

const right: CSSProperties = { textAlign: 'right' }

export function UnverifiedRangesSection(props: {
  tenant: string
  ranges: CoverageReport['unverified_ranges']
}) {
  const { tenant, ranges } = props
  // Worst-first by this class's own clock: how long the range has sat unwalked.
  const rows = [...ranges].sort((a, b) => b.unverified_for_s - a.unverified_for_s)
  const seqs = rows.reduce((acc, r) => acc + r.seqs, 0)
  const devices = new Set(rows.map((r) => r.device_id)).size

  return (
    <section className="panel" style={secStyle}>
      <SectionHead
        sev="unknown"
        title="Accepted but never verified"
        tag={{ text: `${num(seqs)} seqs · ${num(devices)} devices`, sev: 'unknown' }}
        field="coverage.unverified_ranges[]"
      />
      <p className="display" style={leadStyle}>
        Stored is not verified. These ranges arrived, were written, and have never had a verify
        walk run against them — their state is unknown, which is a third thing, not a quiet yes.
      </p>

      {rows.length === 0 ? (
        <p className="display" style={{ margin: 0 }}>
          Every accepted range has been walked at least once.
        </p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>device_id</th>
                <th>seq range</th>
                <th className="num" style={right}>seqs</th>
                <th>accepted</th>
                <th>last verify walk</th>
                <th style={right}>unverified for</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const href =
                  `/console/${encodeURIComponent(tenant)}/evidence/verify` +
                  `?device_id=${encodeURIComponent(r.device_id)}&from=${r.seq_start}&to=${r.seq_end}`
                return (
                  <tr key={`${r.device_id}:${r.seq_start}-${r.seq_end}`}>
                    <td><span className="mono">{r.device_id}</span></td>
                    <td><span className="hash">{num(r.seq_start)} – {num(r.seq_end)}</span></td>
                    <td className="num mono" style={right}>{num(r.seqs)}</td>
                    <td><span className="hash">{r.accepted_from} → {r.accepted_to}</span></td>
                    <td>
                      {r.last_verify_walk === null
                        ? <span className="tag warn">never</span>
                        : <span className="hash">{r.last_verify_walk}</span>}
                    </td>
                    <td className="mono quiet-for" style={right}>{duration(r.unverified_for_s)}</td>
                    <td style={right}>
                      <a className="link-count" href={href}>verify range</a>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <Methodology>
        Ranges are per-device because <span className="mono">seq</span> is per-device and collides
        across the fleet: seq 3,001 can exist on every device here and be a different event on each.
        Never render one of these ranges without its <span className="mono">device_id</span> attached.
      </Methodology>
    </section>
  )
}
