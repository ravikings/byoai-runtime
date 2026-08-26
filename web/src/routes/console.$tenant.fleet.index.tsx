/**
 * Fleet overview — §6.0.1, design frame 8.
 *
 * One screen answering four questions: is everything reporting, do the chains
 * hold, is evidence still leaving the devices, and what is being refused.
 * Every aggregate on it carries devices_included / devices_enrolled, because
 * a fleet number that quietly drops the devices it could not reach is a lie
 * with a denominator missing.
 */
import { useEffect, useMemo } from 'react'
import { useHref } from '@/app/hrefContext'
import type { CSSProperties } from 'react'
import { createFileRoute } from '@tanstack/react-router'

import { isSchemaMismatch } from '../api/client'
import { useDevices, useFindings, useFleetSummary } from '../api/queries'
import type { HealthRollup, ShellStatus } from '../app/scope'
import { toScope, usePublishShellStatus } from '../app/scope'
import { ScopeLine } from '../components/ScopeLine'
import { CoveragePanel } from '../components/fleet/CoveragePanel'
import { DenialPanel } from '../components/fleet/DenialPanel'
import { FindingsPanel } from '../components/fleet/FindingsPanel'
import { IngestPanel } from '../components/fleet/IngestPanel'
import { IntegrityPanel } from '../components/fleet/IntegrityPanel'
import { PanelError, PanelLoading } from '../components/fleet/PanelState'
import { duration, href, n } from '../components/fleet/format'

export const Route = createFileRoute('/console/$tenant/fleet/')({
  component: FleetOverview,
})

const GRID: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr) minmax(0,1.15fr)',
  gap: 'var(--s4)',
  alignItems: 'start',
}
const COL: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 'var(--s4)' }

function FleetOverview() {  const href = useHref()

  const { tenant } = Route.useParams()
  const search = Route.useSearch()
  const scope = useMemo(() => toScope(tenant, search), [tenant, search])

  const summaryQ = useFleetSummary(scope)
  const devicesQ = useDevices(scope)
  const findingsQ = useFindings(scope)

  const summary = summaryQ.data
  const summaryFailed = summaryQ.error !== null
  const summaryMismatch = isSchemaMismatch(summaryQ.error)

  const devicesProblem: 'loading' | 'failed' | null =
    devicesQ.error !== null ? 'failed' : devicesQ.data === undefined ? 'loading' : null

  /* The shell's three health dots are a rollup of what this screen loaded —
   * published upward rather than fetched twice. A failed or unrecognised
   * response publishes `unknown`, never a quiet green. */
  const publish = usePublishShellStatus()
  // A dot is a rollup over everything this screen knows. Deriving it from the
  // summary alone showed a calm green in the top bar while the panel directly
  // beneath it rendered a load failure — the header contradicting the page.
  const sideQueriesFailed = devicesQ.error !== null || findingsQ.error !== null
  useEffect(() => {
    publish(shellStatus(tenant, summary, summaryQ.error, sideQueriesFailed))
    return () => publish(null)
  }, [publish, tenant, summary, summaryQ.error, sideQueriesFailed])

  // Kept apart on purpose: "silent" means it reported and stopped, "never
  // seen" means it never reported at all. coverageDot already treats these as
  // different severities ("degraded" vs "unaccounted"); summing them into one
  // "silent" tag in the header undid that distinction in the most-read place.
  const silent = summary === undefined ? 0 : summary.coverage.silent
  const neverSeen = summary === undefined ? 0 : summary.coverage.never_seen

  return (
    <>
      <div className="row" style={{ marginBottom: 'var(--s2)' }}>
        <h1>Fleet</h1>
        {silent > 0 ? (
          <span className="tag warn">
            {n(silent)} device{silent === 1 ? '' : 's'} silent
          </span>
        ) : null}
        {neverSeen > 0 ? (
          <span className="tag bad">
            {n(neverSeen)} never seen
          </span>
        ) : null}
        {summaryMismatch ? <span className="tag unknown">unexpected response</span> : null}
        <div className="spacer" style={{ flex: 1 }} />
        <a className="btn sm" href={href.coverage(tenant)}>
          Coverage report →
        </a>
        <a className="btn sm" href={href.devices(tenant)}>
          Devices →
        </a>
      </div>

      <div style={{ marginBottom: 'var(--s4)' }}>
        <ScopeLine
          tenant={tenant}
          search={search}
          inclusion={summary?.inclusion}
          extra={
            summary === undefined
              ? ['no summary loaded — nothing below is known']
              : [`open findings ${n(summary.open_findings)}`]
          }
        />
      </div>

      {summaryFailed ? (
        <div
          className={summaryMismatch ? 'banner warn' : 'banner bad'}
          style={{ marginBottom: 'var(--s4)' }}
        >
          <span className={summaryMismatch ? 'dot unknown' : 'dot bad'} />
          <div>
            {summaryMismatch ? (
              <>
                <b>Unexpected response from the fleet endpoint.</b> The payload did not match this
                console's contract, so nothing on this screen is known — not intact, not broken,
                <b> unknown</b>. This is an outage of the evidence, not a clean fleet.
              </>
            ) : (
              <>
                <b>The fleet summary could not be loaded.</b> Coverage, integrity, ingest and
                denial are all unknown for this scope. An empty screen is not an all-clear.
              </>
            )}
          </div>
          <div className="spacer" />
          <button className="btn sm" type="button" onClick={() => void summaryQ.refetch()}>
            Retry
          </button>
        </div>
      ) : null}

      <div style={GRID}>
        <div style={COL}>
          {summary === undefined ? (
            summaryFailed ? (
              <PanelError title="Coverage" error={summaryQ.error} />
            ) : (
              <PanelLoading title="Coverage" rows={4} />
            )
          ) : (
            <CoveragePanel
              summary={summary}
              tenant={tenant}
              devices={devicesQ.data?.devices}
              devicesProblem={devicesProblem}
            />
          )}

          {summary === undefined ? (
            summaryFailed ? (
              <PanelError title="Integrity" error={summaryQ.error} />
            ) : (
              <PanelLoading title="Integrity" rows={3} />
            )
          ) : (
            <IntegrityPanel summary={summary} tenant={tenant} />
          )}
        </div>

        <div style={COL}>
          {summary === undefined ? (
            summaryFailed ? (
              <PanelError title="Ingest" error={summaryQ.error} />
            ) : (
              <PanelLoading title="Ingest" rows={5} />
            )
          ) : (
            <IngestPanel summary={summary} tenant={tenant} />
          )}

          {summary === undefined ? (
            summaryFailed ? (
              <PanelError title="Denial rate" error={summaryQ.error} />
            ) : (
              <PanelLoading title="Denial rate" rows={3} />
            )
          ) : (
            <DenialPanel summary={summary} tenant={tenant} />
          )}
        </div>

        <div style={COL}>
          {findingsQ.data === undefined ? (
            findingsQ.error !== null ? (
              <PanelError title="Open findings" error={findingsQ.error} />
            ) : (
              <PanelLoading title="Open findings" rows={5} />
            )
          ) : (
            <FindingsPanel
              findings={findingsQ.data.findings}
              total={findingsQ.data.total}
              inclusion={findingsQ.data.inclusion}
              tenant={tenant}
            />
          )}

          <div className="blindspot">
            <b>What this screen cannot see.</b> A device that was never enrolled produces no{' '}
            <span className="mono">device_id</span>, ships no batch and raises no finding — it is
            absent from every number here, including the denominator of{' '}
            {summary === undefined ? 'enrolled devices' : n(summary.inclusion.devices_enrolled)}.{' '}
            <a className="ref" href={href.enrollment(tenant)}>
              Review enrollment →
            </a>
          </div>
        </div>
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ *
 * The three health dots, computed from what this screen actually read.
 * ------------------------------------------------------------------ */

const UNKNOWN = (worst: string, to: string): HealthRollup => ({
  state: 'unknown',
  state_label: 'unknown',
  worst,
  href: to,
})

function shellStatus(
  tenant: string,
  summary: Summary | undefined,
  error: Error | null,
  sideQueriesFailed = false,
): ShellStatus {
  if (summary === undefined) {
    const why =
      error === null
        ? 'fleet summary not loaded yet'
        : isSchemaMismatch(error)
          ? 'fleet summary did not match the contract'
          : 'fleet summary could not be loaded'
    return {
      coverage: UNKNOWN(why, href.coverage(tenant)),
      integrity: UNKNOWN(why, href.findings(tenant)),
      ingest: UNKNOWN(why, href.coverage(tenant)),
    }
  }
  if (sideQueriesFailed) {
    // The summary parsed, but the device or findings query behind these panels
    // did not. Reporting the summary's verdict alone would put a settled dot
    // above a panel showing an error.
    const why = 'part of this screen failed to load — this rollup is incomplete'
    return {
      inclusion: summary.inclusion,
      coverage: UNKNOWN(why, href.coverage(tenant)),
      integrity: UNKNOWN(why, href.findings(tenant)),
      ingest: UNKNOWN(why, href.coverage(tenant)),
    }
  }
  return {
    inclusion: summary.inclusion,
    coverage: coverageDot(summary, tenant),
    integrity: integrityDot(summary, tenant),
    ingest: ingestDot(summary, tenant),
  }
}

type Summary = NonNullable<ReturnType<typeof useFleetSummary>['data']>

function coverageDot(summary: Summary, tenant: string): HealthRollup {
  const quiet = summary.coverage.silent + summary.coverage.never_seen
  if (quiet === 0) {
    return {
      state: 'ok',
      state_label: 'intact',
      worst: `all ${n(summary.coverage.enrolled)} enrolled devices reporting`,
      href: href.coverage(tenant),
    }
  }
  return {
    state: summary.coverage.never_seen > 0 ? 'bad' : 'warn',
    // The severity is right, but the WORD has to name this condition rather
    // than borrow integrity's. A device that never reported is not "broken" —
    // nothing about it is known to be wrong; it is unaccounted for. Three dots
    // all reading "broken" would collapse three different facts into one.
    state_label: summary.coverage.never_seen > 0 ? 'unaccounted' : 'degraded',
    worst: `${n(quiet)} of ${n(summary.coverage.enrolled)} devices not reporting${
      summary.coverage.never_seen > 0 ? `, ${n(summary.coverage.never_seen)} never seen` : ''
    }`,
    href: href.coverage(tenant),
  }
}

function integrityDot(summary: Summary, tenant: string): HealthRollup {
  const { intact, broken, unverified } = summary.integrity
  if (broken > 0) {
    return {
      state: 'bad',
      state_label: 'broken',
      worst: `${n(broken)} device${broken === 1 ? '' : 's'} with a broken chain`,
      href: href.findings(tenant),
    }
  }
  if (unverified > 0) {
    return {
      state: 'unknown',
      state_label: 'unknown',
      worst: `${n(unverified)} device${unverified === 1 ? '' : 's'} unverified — not a pass`,
      href: href.verifyUnverified(tenant),
    }
  }
  if (intact === 0) {
    // Zero broken and zero unverified with zero intact means nothing was
    // walked at all. Falling through to green here would render an empty or
    // unverified slice as a clean pass — absence of evidence presented as
    // evidence of absence, which is the one claim this product must never make.
    return UNKNOWN('no device in this scope has been verified', href.findings(tenant))
  }
  return {
    state: 'ok',
    state_label: 'intact',
    worst: `${n(intact)} device${intact === 1 ? '' : 's'} verify intact`,
    href: href.findings(tenant),
  }
}

function ingestDot(summary: Summary, tenant: string): HealthRollup {
  const flat = summary.ingest.rate_flat_for_minutes
  if (flat !== null) {
    return {
      state: 'bad',
      // "stalled", not "broken": the chains that did arrive are fine. What has
      // failed is arrival itself, which is a different repair.
      state_label: 'stalled',
      worst: `ingest rate flat for ${duration(flat * 60)} — recorders ship on a timer`,
      href: href.coverage(tenant),
    }
  }
  if (summary.ingest.last_batch_at === null) {
    return UNKNOWN('no batch has ever arrived in this scope', href.coverage(tenant))
  }
  if (summary.ingest.backlog_entries > 0) {
    return {
      state: 'warn',
      state_label: 'degraded',
      worst: `${n(summary.ingest.backlog_entries)} entries unsynced on ${n(
        summary.ingest.backlog_devices,
      )} device${summary.ingest.backlog_devices === 1 ? '' : 's'}`,
      href: href.devices(tenant),
    }
  }
  return {
    state: 'ok',
    state_label: 'intact',
    worst: `${n(summary.ingest.entries_received)} entries received in window`,
    href: href.devices(tenant),
  }
}
