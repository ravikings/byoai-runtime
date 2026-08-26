/**
 * Loading, failure and empty states for the silence report.
 *
 * The failure states matter more here than on any other screen. This page
 * renders absence, so a page that renders nothing is indistinguishable from a
 * page that renders good news unless it says which one it is. Nothing in this
 * file is allowed to fail quietly.
 */
import { isSchemaMismatch } from '@/api/client'
import { num } from './format'

export function CoverageLoading() {
  return (
    <div className="banner info" style={{ marginTop: 'var(--s4)' }}>
      <span className="dot unknown pulse" />
      <span>
        <b>Counting what is missing.</b> Until this finishes, the absence of rows below means the
        query has not returned — not that there is nothing to report.
      </span>
    </div>
  )
}

/**
 * The response was 200 but did not match the contract. This gets its own
 * state, loudly, because the alternative is a calm empty screen over data the
 * client could not read — the exact failure this product exists to prevent.
 */
export function CoverageError(props: { error: unknown }) {
  const { error } = props
  const schema = isSchemaMismatch(error)
  const message = error instanceof Error ? error.message : String(error)

  return (
    <section className="panel" style={{ marginTop: 'var(--s4)' }}>
      <div className="banner bad" style={{ marginTop: 0 }}>
        <span className="dot bad pulse" />
        <span>
          <b>{schema ? 'Unexpected response.' : 'Coverage could not be computed.'}</b>{' '}
          {schema
            ? 'The server answered, but the body does not match the console API contract. ' +
              'No count on this page can be trusted, and the empty page below is not good news — ' +
              'it is a page that failed to load.'
            : 'This page is showing you nothing because it could not ask, not because there is ' +
              'nothing to show. Treat coverage as unknown until this succeeds.'}
        </span>
      </div>
      <p className="hash" style={{ marginTop: 'var(--s3)', whiteSpace: 'pre-wrap' }}>{message}</p>
      {schema && isSchemaMismatch(error) ? (
        <>
          <div className="label">contract mismatches ({num(error.issues.length)})</div>
          <div className="table-scroll">
            <table>
              <thead><tr><th>path</th><th>problem</th></tr></thead>
              <tbody>
                {error.issues.map((issue, i) => (
                  <tr key={`${issue.path.join('.')}:${i}`}>
                    <td><span className="mono">{issue.path.join('.') || '<root>'}</span></td>
                    <td>{issue.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="methodology">
            Requested <span className="mono">{error.url}</span>. The body as received is kept on
            the error object so an operator can compare it against the contract in
            <span className="mono"> src/api/schemas.ts</span> rather than guessing which side drifted.
          </p>
        </>
      ) : null}
    </section>
  )
}

/**
 * Every class came back empty. This is the good outcome and it is still not a
 * clean bill of health — the blind spot below is unaffected by it, and this
 * banner says so rather than letting a blank page imply completeness.
 */
export function CoverageEmpty(props: { enrolled: number }) {
  return (
    <div className="banner info" style={{ marginTop: 'var(--s4)' }}>
      <span className="dot ok" />
      <span>
        <b>Nothing is missing across {num(props.enrolled)} enrolled devices.</b> All five classes
        of absence are empty: every device has reported inside its cadence, every accepted range
        has been walked, every checkpoint is counter-signed, every agent ran under a mandate.
        This is the good outcome — and it still says nothing about a device that was never
        enrolled. See the limit of this screen below.
      </span>
    </div>
  )
}
