/**
 * The runtime client's two decisions that are not visual: classifying a
 * stats failure by what it says about the HOST (a 404 is knowledge — this
 * process is not a proxy — while a network drop is ignorance), and parsing
 * the real proxy payloads. Fixtures here are verbatim shapes from
 * `agent_context_cache.main`.
 */
import { describe, expect, it } from 'vitest'
import { EstimatedStats, StatsHistory, PermanentStats } from './schemas'
import { HttpError, NetworkError } from './client'
import { runtimeAvailability } from './runtime'

function http(status: number): HttpError {
  return new HttpError('/v1/stats', status, 'x', '{}')
}

describe('runtimeAvailability', () => {
  it('reads a 404 as a fact about the host, not a failure', () => {
    expect(runtimeAvailability(http(404))).toBe('not-on-this-host')
  })
  it('reads 401/403 as a gated proxy, distinct from an absent one', () => {
    expect(runtimeAvailability(http(401))).toBe('auth-required')
    expect(runtimeAvailability(http(403))).toBe('auth-required')
  })
  it('keeps transport trouble as ignorance', () => {
    expect(runtimeAvailability(new NetworkError('/v1/stats', 'down'))).toBe('unknown')
    expect(runtimeAvailability(http(500))).toBe('unknown')
    expect(runtimeAvailability(null)).toBe('served')
  })
})

describe('proxy payloads parse', () => {
  it('the live estimate', () => {
    const parsed = EstimatedStats.parse({
      optimizer_enabled: true,
      tokens_saved: 0,
      tokens_original: 0,
      tokens_sent: 0,
      savings_percentage: '0.00%',
      methodology: 'ESTIMATE ONLY — len(json.dumps(body)) // 4',
    })
    expect(parsed.optimizer_enabled).toBe(true)
  })

  it('the durable summary: null retention is all-time, not zero', () => {
    const parsed = PermanentStats.parse({
      storage: '/home/op/.byoai/byoai_runtime.db',
      benchmark: {
        sample_count: 8,
        real_tokens_original: 353330,
        real_tokens_sent: 169350,
        real_tokens_saved: 183980,
        real_savings_percentage: '52.07%',
      },
      usage_totals: { request_count: 24, total_input_tokens: 216276 },
      retention_days: null,
      methodology: 'Identical measurement methodology',
    })
    expect(parsed.retention_days).toBeNull()
  })

  it('history samples, including the null model the column allows', () => {
    const parsed = StatsHistory.parse({
      count: 2,
      samples: [
        { ts: 1.5, session_id: 's', model: null, real_tokens_original: 10, real_tokens_sent: 4, real_tokens_saved: 6 },
        { ts: 2.5, session_id: 's', model: 'claude-sonnet-4-5', real_tokens_original: 10, real_tokens_sent: 8, real_tokens_saved: 2 },
      ],
    })
    expect(parsed.samples[0]!.model).toBeNull()
  })
})
