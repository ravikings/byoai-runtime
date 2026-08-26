/**
 * The honest destination for a section this slice has not built yet.
 *
 * The alternative — leaving the link pointing at a route the router cannot
 * match — drops the user on a not-found fallback that looks like a fault in
 * the product rather than a gap in it. On a console whose entire argument is
 * that it never presents unknown state as something else, shipping dead links
 * in the primary navigation is the same mistake wearing different clothes.
 */
export function NotBuilt({ path, tenant }: { path: string; tenant: string }) {
  return (
    <main className="content">
      <h1>Not built yet</h1>
      <p className="display" style={{ marginTop: 'var(--s2)' }}>
        This section is specified but not implemented in the current slice.
      </p>
      <div className="panel" style={{ maxWidth: '62ch' }}>
        <h3 className="label">Requested</h3>
        <p className="mono" style={{ margin: '0 0 var(--s3)' }}>{path}</p>
        <p className="muted" style={{ margin: 0 }}>
          Fleet overview and the coverage report are the two screens built so far. The
          remaining sections — Ledger, Evidence, Mandate, Runtime and the devices register —
          are designed (see <span className="mono">internal_doc/console_design/</span>) and
          specified in <span className="mono">console_ui_spec.md</span> §6, and land on the
          same shell and data layer as these two.
        </p>
      </div>
      {/* Absolute, tenant-qualified. A relative href resolves against the
          browser's current directory, which varies with the splat depth and
          drops the tenant segment entirely on a one-level path. */}
      <a className="btn" href={`/console/${encodeURIComponent(tenant)}/fleet`}>
        ← Back to Fleet
      </a>
    </main>
  )
}
