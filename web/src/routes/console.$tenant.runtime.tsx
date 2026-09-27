/**
 * Runtime — `/console/{tenant}/runtime` (spec §6.6). The savings screen,
 * built around the one number the product is allowed to cite and the one it
 * never is: *Estimated* (`/v1/stats`, a character heuristic whose own API
 * response tells you not to cite it — so the warning lives on the block
 * permanently, not in a tooltip) and *Tokenizer-verified*
 * (`/v1/stats/permanent`, Anthropic's real count_tokens measured on sampled
 * traffic and persisted to disk). History renders the per-sample spread
 * before the aggregate earns trust, `null` retention renders as "all time —
 * no prune has run", and the optimizer toggle is optimistic with rollback.
 *
 * This screen reads the proxy's root `/v1` API, not the fleet contract, and
 * not every host that serves this SPA runs a proxy. When the host answers 404
 * the screen says so as a fact about the host — no zeros, no spinner, no red
 * alarm.
 */
import { useMemo } from 'react'
import { createFileRoute } from '@tanstack/react-router'
import { useHref } from '@/app/hrefContext'
import {
  runtimeAvailability,
  useOptimizerToggle,
  usePermanentStats,
  useStatsEstimate,
  useStatsHistory,
} from '@/api/runtime'
import type { BenchmarkSample } from '@/api/schemas'
import { PanelError, PanelEmpty, PanelLoading } from '@/components/fleet/PanelState'
import { n } from '@/components/fleet/format'
import { Sparkline } from '@/components/fleet/Sparkline'
import { formatCount } from '@/lib/number'

export const Route = createFileRoute('/console/$tenant/runtime')({
  component: RuntimePage,
})

/** Below this many verified samples, the aggregate percentage is shown
 *  muted with the caveat inline — the API's own wording is "directional
 *  until dozens". A dozen samples of one payload shape is not a rate. */
const DIRECTIONAL_UNDER = 24

function fmtTokens(v: unknown): string {
  return typeof v === 'number' ? formatCount(v) : String(v)
}

function RuntimePage() {
  const href = useHref()
  const { tenant } = Route.useParams()
  const estimateQ = useStatsEstimate()
  const permanentQ = usePermanentStats()
  const historyQ = useStatsHistory()
  const availability = runtimeAvailability(estimateQ.error ?? null)
  const toggle = useOptimizerToggle(estimateQ.data?.optimizer_enabled ?? false)

  const byModel = useMemo(() => {
    const groups = new Map<string, BenchmarkSample[]>()
    for (const s of historyQ.data?.samples ?? []) {
      const key = s.model ?? 'unknown model'
      const list = groups.get(key) ?? []
      list.push(s)
      groups.set(key, list)
    }
    for (const list of groups.values()) list.sort((a, b) => a.ts - b.ts)
    return groups
  }, [historyQ.data])

  return (
    <>
      <div className="row" style={{ marginBottom: 'var(--s3)' }}>
        <h1>Runtime</h1>
        <span className="tag info">proxy host</span>
        <span className="hash">GET /v1/stats · /stats/permanent · /stats/history</span>
        <div className="spacer" style={{ flex: 1 }} />
        <a className="btn sm" href={href.fleet(tenant)}>
          ← Fleet
        </a>
      </div>

      <div className="scope" style={{ marginBottom: 'var(--s4)' }}>
        <span>
          figures cover <b>this proxy process</b>, not a tenant or a fleet
        </span>
        <span>
          estimate <b>since last restart</b> (Redis counters) · verified{' '}
          <b>since the retention window</b> (durable rows)
        </span>
        {estimateQ.data ? (
          <span>
            optimizer <b>{estimateQ.data.optimizer_enabled ? 'on' : 'off'}</b>
          </span>
        ) : null}
      </div>

      {estimateQ.isPending && estimateQ.error === null ? (
        <PanelLoading title="Runtime" rows={6} />
      ) : availability === 'not-on-this-host' ? (
        // The honest degrade: this SPA is also served by hosts that capture
        // and seal but never proxy a model API. There are no savings figures
        // here because there is no cache here — a zero would claim a quiet
        // proxy, and a spinner claims we are still finding out.
        <div className="verdict unknown">
          <div className="state">
            <span className="dot unknown" /> Not the proxy
          </div>
          <p className="reading">
            This console was served by a host that does not run the caching
            proxy. Token savings are measured where the model traffic passes
            through — the proxy answers <span className="mono">/v1/stats</span>;
            this process does not, so there is nothing to show rather than a
            zero to misread.
          </p>
        </div>
      ) : availability === 'auth-required' ? (
        <div className="banner warn">
          <span className="dot warn" />
          <span>
            <b>This proxy requires its shared token.</b> The runtime API
            refused the request (401) — open the console through the proxy's
            token-authenticated address; the figures are not less real for
            being gated, and not more real for being retried.
          </span>
        </div>
      ) : estimateQ.isError ? (
        <PanelError title="Runtime" error={estimateQ.error} />
      ) : (
        estimateQ.data && (
          <>
            <div className="row" style={{ marginBottom: 'var(--s4)', gap: 'var(--s3)' }}>
              <button
                type="button"
                className={estimateQ.data.optimizer_enabled ? 'btn sm primary' : 'btn sm'}
                disabled={toggle.isPending}
                onClick={() => toggle.mutate()}
              >
                {toggle.isPending
                  ? 'toggling…'
                  : estimateQ.data.optimizer_enabled
                    ? 'Optimizer: on — turn off'
                    : 'Optimizer: off — turn on'}
              </button>
              {toggle.isError ? (
                <span className="tag bad">toggle failed — state rolled back</span>
              ) : null}
            </div>

            <div className="col2">
              {/* Estimated — permanently wearing its do-not-cite chip. */}
              <section className="panel">
                <div className="panel-head">
                  <h3 className="label">Estimated</h3>
                  <span className="caveat" title="Character-count heuristic, not a tokenizer reading">
                    estimate only — do not cite
                  </span>
                </div>
                <div className="stat">
                  <div className="pair">
                    <span className="value provisional">{estimateQ.data.savings_percentage}</span>
                    <span className="unit">saved · heuristic</span>
                  </div>
                  <div className="provenance mono">
                    {n(estimateQ.data.tokens_original)} original → {n(estimateQ.data.tokens_sent)}{' '}
                    sent · {n(estimateQ.data.tokens_saved)} saved
                  </div>
                </div>
                <p className="methodology">
                  {estimateQ.data.methodology}
                </p>
              </section>

              {/* Tokenizer-verified — the only number to put in a report. */}
              <section className="panel">
                <div className="panel-head">
                  <h3 className="label">Tokenizer-verified</h3>
                  <span className="mono dim">/stats/permanent · durable rows</span>
                </div>
                {permanentQ.data ? (
                  <>
                    <div className="stat">
                      <div className="pair">
                        <span
                          className={
                            permanentQ.data.benchmark.sample_count < DIRECTIONAL_UNDER
                              ? 'value provisional'
                              : 'value'
                          }
                        >
                          {permanentQ.data.benchmark.real_savings_percentage}
                        </span>
                        <span className="unit">saved · real tokenizer</span>
                      </div>
                      <div className="provenance mono">
                        {n(permanentQ.data.benchmark.sample_count)} sampled requests ·{' '}
                        {n(permanentQ.data.benchmark.real_tokens_original)} original →{' '}
                        {n(permanentQ.data.benchmark.real_tokens_sent)} sent
                      </div>
                    </div>
                    {permanentQ.data.benchmark.sample_count < DIRECTIONAL_UNDER ? (
                      <div className="caveat" style={{ marginTop: 'var(--s2)' }}>
                        Directional, not a rate — {n(permanentQ.data.benchmark.sample_count)}{' '}
                        sample{permanentQ.data.benchmark.sample_count === 1 ? '' : 's'} cannot
                        support a percentage. Treat it as a shape until dozens have been sampled.
                      </div>
                    ) : null}
                    <div className="kv" style={{ marginTop: 'var(--s3)' }}>
                      <span className="mono muted">retention</span>
                      <span>
                        {permanentQ.data.retention_days === null
                          ? 'all time — no prune has run in this process'
                          : permanentQ.data.retention_days === 0
                            ? 'all time — retention disabled'
                            : `last ${n(permanentQ.data.retention_days)} days — totals cover the window the last prune enforced, not everything that ever ran`}
                      </span>
                      <span className="mono muted">storage</span>
                      <span className="hash">{permanentQ.data.storage}</span>
                    </div>
                    <p className="methodology">{permanentQ.data.methodology}</p>
                  </>
                ) : permanentQ.isError ? (
                  <PanelError title="Verified savings" error={permanentQ.error} />
                ) : (
                  <PanelLoading title="Verified savings" rows={3} />
                )}
              </section>
            </div>

            {/* History: variance before the aggregate. Small multiples per
                model, one sample per point, oldest → newest. */}
            <h2 style={{ marginTop: 'var(--s5)' }}>Per-sample spread</h2>
            {historyQ.isPending ? (
              <PanelLoading title="Benchmark history" rows={4} />
            ) : historyQ.isError ? (
              <PanelError title="Benchmark history" error={historyQ.error} />
            ) : byModel.size === 0 ? (
              <section className="panel">
                <PanelEmpty headline="No sampled requests yet.">
                  <p className="mono muted" style={{ lineHeight: 1.45 }}>
                    Verified numbers exist only when{' '}
                    <span className="mono">BYOAI_BENCHMARK_SAMPLE_RATE</span> is above 0 and
                    real traffic has flowed. Until then the only figure on this screen is the
                    estimate, and the estimate is not a claim.
                  </p>
                </PanelEmpty>
              </section>
            ) : (
              <div className="sm-grid">
                {[...byModel.entries()].map(([model, samples]) => (
                  <figure className="sm" key={model}>
                    <figcaption>
                      <span className="mono">{model}</span>{' '}
                      <span className="mono dim">
                        {n(samples.length)} sample{samples.length === 1 ? '' : 's'}
                      </span>
                    </figcaption>
                    <Sparkline
                      series={samples.map(
                        (s) =>
                          s.real_tokens_original > 0
                            ? (s.real_tokens_saved / s.real_tokens_original) * 100
                            : 0,
                      )}
                      label={`% saved per sampled request, ${model}`}
                    />
                    <div className="mono dim" style={{ fontSize: 'var(--t-micro)' }}>
                      oldest → newest · each point is one real request, tokenized
                    </div>
                  </figure>
                ))}
              </div>
            )}

            {/* Usage totals from the durable log. */}
            {permanentQ.data ? (
              <section className="panel" style={{ marginTop: 'var(--s4)' }}>
                <div className="panel-head">
                  <h3 className="label">Usage totals</h3>
                  <span className="mono dim">from {permanentQ.data.storage}</span>
                </div>
                <div className="kv">
                  {Object.entries(permanentQ.data.usage_totals).map(([k, v]) => (
                    <span key={k} style={{ display: 'contents' }}>
                      <span className="mono muted">{k}</span>
                      <span className="num">{fmtTokens(v)}</span>
                    </span>
                  ))}
                </div>
              </section>
            ) : null}
          </>
        )
      )}
    </>
  )
}
