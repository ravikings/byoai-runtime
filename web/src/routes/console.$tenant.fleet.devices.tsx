/**
 * Devices register — `/console/{tenant}/fleet/devices`.
 *
 * The overview answers "is the fleet well"; this is the list of *which* —
 * one row per enrolled device, quietest first, every seq pinned to its
 * device, every state word paired with its dot. It reads the same
 * `/v1/console/fleet/devices` contract as every other screen, so it renders
 * whatever the host honestly knows: on the local Shield that is one device
 * with integrity walked on the read; on a fleet server it is the whole
 * register. Rows this host cannot fill carry `null` and render unknown —
 * the register never renders an absent value as a small one.
 */
import { useEffect, useMemo } from 'react'
import { createFileRoute } from '@tanstack/react-router'

import { useDevices } from '@/api/queries'
import { toScope, usePublishShellStatus, type HealthRollup, type ShellStatus } from '@/app/scope'
import { ScopeLine } from '@/components/ScopeLine'
import { DeviceRegister } from '@/components/fleet/DeviceRegister'
import { PanelError, PanelLoading } from '@/components/fleet/PanelState'
import { href as fleetHref, n } from '@/components/fleet/format'
import { useHref } from '@/app/hrefContext'

export const Route = createFileRoute('/console/$tenant/fleet/devices')({
  component: DevicesPage,
})

function DevicesPage() {
  const href = useHref()
  const { tenant } = Route.useParams()
  const search = Route.useSearch()
  const scope = useMemo(() => toScope(tenant, search), [tenant, search])
  const query = useDevices(scope)
  const r = query.data

  // The shell's dots roll up what this register read: per-device integrity
  // and liveness rows are the same facts the table shows, so the top bar
  // keeps its verdict while the operator walks the register. Ingest is not
  // carried by this endpoint, so it stays un-published rather than borrowing
  // a green from a screen the user has left.
  const publish = usePublishShellStatus()
  useEffect(() => {
    publish(registerShell(tenant, r, query.error !== null))
    return () => publish(null)
  }, [publish, tenant, r, query.error])

  return (
    <>
      <div className="row" style={{ marginBottom: 'var(--s3)' }}>
        <h1>Devices</h1>
        <span className="tag info">read-only</span>
        <span className="hash">GET /v1/console/fleet/devices</span>
        <div className="spacer" style={{ flex: 1 }} />
        <a className="btn sm" href={href.coverage(tenant)}>
          Silence report →
        </a>
        <a className="btn sm" href={href.fleet(tenant)}>
          ← Fleet
        </a>
      </div>

      <ScopeLine
        tenant={tenant}
        search={search}
        inclusion={r?.inclusion}
        extra={r === undefined ? [] : [`registers ${n(r.devices.length)} device${r.devices.length === 1 ? '' : 's'}`]}
      />

      {r === undefined ? (
        query.error !== null ? (
          <PanelError title="Device register" error={query.error} />
        ) : (
          <PanelLoading title="Device register" rows={6} />
        )
      ) : (
        <>
          {r.devices.length > 1 ? (
            // The seq-collision warning earns its space only where seqs can
            // actually collide. On a single-device register the banner would
            // assert an eleven-ways ambiguity that does not exist here.
            <div className="banner warn" style={{ marginBottom: 'var(--s4)' }}>
              <span className="dot warn" />
              <span>
                <b>Seq numbers are per device and collide across this fleet.</b>{' '}
                {n(r.devices.length)} devices carry overlapping ranges; a seq is only an
                address written <span className="mono">device_id · seq</span> — which is
                why no column on this page renders a bare number.
              </span>
            </div>
          ) : null}

          <section className="panel flush">
            <DeviceRegister
              devices={r.devices}
              tenant={tenant}
              total={r.inclusion.devices_enrolled}
            />
          </section>

          <div className="blindspot" style={{ marginTop: 'var(--s4)' }}>
            <b>What this register cannot tell you.</b> A device that was never enrolled
            has shipped no batch and has no row here — absence of a row is not evidence
            of absence.{' '}
            <a className="ref" href={href.coverage(tenant)}>
              Coverage / silence report →
            </a>
          </div>
        </>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ *
 * The shell's health dots, rolled up from the register's own rows.
 * ------------------------------------------------------------------ */

const UNKNOWN = (worst: string, to: string): HealthRollup => ({
  state: 'unknown',
  state_label: 'unknown',
  worst,
  href: to,
})

function registerShell(
  tenant: string,
  r: ReturnType<typeof useDevices>['data'],
  failed: boolean,
): ShellStatus {
  if (r === undefined) {
    const why = failed
      ? 'the device register could not be loaded'
      : 'device register still loading'
    return {
      coverage: UNKNOWN(why, fleetHref.coverage(tenant)),
      integrity: UNKNOWN(why, fleetHref.findings(tenant)),
    }
  }
  const neverSeen = r.devices.filter((d) => d.liveness === 'never_seen').length
  const silent = r.devices.filter((d) => d.liveness === 'silent').length
  const broken = r.devices.filter((d) => d.integrity === 'broken').length
  const unverified = r.devices.filter((d) => d.integrity === 'unverified').length
  const intact = r.devices.filter((d) => d.integrity === 'intact').length

  const coverage: HealthRollup =
    neverSeen > 0
      ? {
          state: 'bad',
          state_label: 'unaccounted',
          worst: `${n(neverSeen)} enrolled device${neverSeen === 1 ? '' : 's'} never heard from`,
          href: fleetHref.devices(tenant),
        }
      : silent > 0
        ? {
            state: 'warn',
            state_label: 'degraded',
            worst: `${n(silent)} silent device${silent === 1 ? '' : 's'} in the register`,
            href: fleetHref.coverage(tenant),
          }
        : r.devices.length === 0
          ? UNKNOWN('the register is empty — enrolled devices unknown', fleetHref.coverage(tenant))
          : {
              state: 'ok',
              state_label: 'intact',
              worst: `all ${n(r.devices.length)} registered devices reporting`,
              href: fleetHref.devices(tenant),
            }

  const integrity: HealthRollup =
    broken > 0
      ? {
          state: 'bad',
          state_label: 'broken',
          worst: `${n(broken)} device${broken === 1 ? '' : 's'} with a broken chain`,
          href: fleetHref.findings(tenant),
        }
      : unverified > 0
        ? {
            state: 'unknown',
            state_label: 'unknown',
            worst: `${n(unverified)} unverified — not a pass`,
            href: fleetHref.verifyUnverified(tenant),
          }
        : intact === 0
          ? UNKNOWN('no device in the register carries a verdict', fleetHref.findings(tenant))
          : {
              state: 'ok',
              state_label: 'intact',
              worst: `${n(intact)} device${intact === 1 ? '' : 's'} verify intact`,
              href: fleetHref.devices(tenant),
            }

  return { inclusion: r.inclusion, coverage, integrity }
}
