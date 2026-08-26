/**
 * Total blind area — the five classes of absence as one strip, then the whole
 * enrolment split into what we have and have not heard from.
 *
 * The rollup deliberately has four segments, not three. A device we have never
 * heard from is not "healthy" and not "broken": it is `unknown`, and folding it
 * into either would be the exact lie this screen exists to prevent.
 */
import type { CSSProperties } from 'react'
import type { CoverageReport } from '@/api/schemas'
import { compact, num } from './format'

const stripStyle: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(5, 1fr)',
  gap: 'var(--s5)',
  alignItems: 'start',
}
const cellStyle: CSSProperties = {}
const dividedCellStyle: CSSProperties = {
  borderLeft: 'var(--hairline-soft)',
  paddingLeft: 'var(--s5)',
}

function Cell(props: { first?: boolean; value: string; of: string; provenance: string }) {
  return (
    <div style={props.first ? cellStyle : dividedCellStyle}>
      <div className="fraction">
        <span className="num">{props.value}</span>
        <span className="of">{props.of}</span>
      </div>
      <div className="stat">
        <div className="provenance">{props.provenance}</div>
      </div>
    </div>
  )
}

export function BlindArea(props: { report: CoverageReport }) {
  const r = props.report
  const neverSeen = r.never_seen.length
  const silent = r.silent.length
  const seqs = r.unverified_ranges.reduce((acc, range) => acc + range.seqs, 0)
  const rangeDevices = new Set(r.unverified_ranges.map((range) => range.device_id)).size

  // Liveness is a four-state enum, so the rollup is read off it directly
  // rather than inferred from a threshold. Anything the API did not classify
  // as late or silent, and that we have heard from, is within cadence.
  const late = r.silent.filter((d) => d.liveness === 'late').length
  const farPast = r.silent.filter((d) => d.liveness === 'silent').length
  const withinCadence = Math.max(0, r.enrolled - neverSeen - late - farPast)
  const pct = (n: number): string => `${r.enrolled > 0 ? (n / r.enrolled) * 100 : 0}%`

  return (
    <section className="panel" style={{ marginTop: 'var(--s4)' }}>
      <h3>Total blind area</h3>
      <div style={stripStyle}>
        <Cell
          first
          value={num(silent)}
          of={silent === 1 ? 'device quiet' : 'devices quiet'}
          // The rollup below splits these into "late" (warn) and "far past
          // cadence" (bad). A headline that silently merges them into one
          // "silent" figure leaves two totals on one screen that never
          // reconcile, so the split is stated here instead.
          provenance={`${num(late)} late · ${num(farPast)} far past cadence · of ${num(
            r.enrolled - neverSeen,
          )} that ever reported`}
        />
        <Cell
          value={num(neverSeen)}
          of="never seen"
          provenance="enrolled, key registered, 0 batches received"
        />
        <Cell
          value={compact(seqs)}
          of="seqs unverified"
          provenance={`${num(seqs)} accepted · no verify walk ever run · ${num(rangeDevices)} devices`}
        />
        <Cell
          value={num(r.checkpoint_gaps.sessions_without_checkpoint)}
          of="sessions uncheckpointed"
          provenance={`+ ${num(r.checkpoint_gaps.checkpoints_never_countersigned)} checkpoints never counter-signed`}
        />
        <Cell
          value={num(r.ungoverned_agents.length)}
          of="agents ungoverned"
          provenance="ran with mandate_version_id: null"
        />
      </div>

      <div style={{ marginTop: 'var(--s5)' }}>
        <div className="label">
          {num(r.enrolled)} enrolled devices, by what we have heard from them
        </div>
        <div className="rollup">
          <div className="seg ok" style={{ width: pct(withinCadence) }} />
          <div className="seg warn" style={{ width: pct(late) }} />
          <div className="seg bad" style={{ width: pct(farPast) }} />
          <div className="seg unknown" style={{ width: pct(neverSeen) }} />
        </div>
        <div className="rollup-key">
          <span><span className="dot ok" /> {num(withinCadence)} within cadence</span>
          <span><span className="dot warn" /> {num(late)} late</span>
          <span><span className="dot bad" /> {num(farPast)} far past cadence</span>
          <span>
            <span className="dot unknown" /> {num(neverSeen)} never seen — not
            &ldquo;healthy&rdquo;, not &ldquo;broken&rdquo;: <i>unheard</i>
          </span>
        </div>
        <p className="methodology">
          Counted from <span className="mono">device_enrolments</span> joined to the ingest
          store&rsquo;s <span className="mono">last_batch_at</span>. A device with zero rows on the
          right side of that join is <b>unknown</b>, never <b>ok</b> — the console has no evidence
          either way, and silence is not consent. Figures carry{' '}
          <span className="mono">devices_included {num(r.enrolled)} / devices_enrolled {num(r.enrolled)}</span>:
          nothing here is a sample. The one thing this arithmetic cannot include is named at the
          bottom of this screen.
        </p>
      </div>
    </section>
  )
}
