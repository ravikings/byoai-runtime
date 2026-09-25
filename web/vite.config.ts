import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { TanStackRouterVite } from '@tanstack/router-plugin/vite'
import path from 'node:path'
import type { Plugin } from 'vite'

/**
 * `/shield` is a client route outside the `/console/` base (it is not part of
 * the admin console). Vite only serves the app's page under the base, so a
 * reload or bookmark of /shield would 404 in dev; hand those requests the
 * same page. `byoai-shield` does the equivalent for the built app.
 */
function shieldPageInDev(): Plugin {
  return {
    name: 'shield-page-in-dev',
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        if (req.url && /^\/shield(\/|\?|$)/.test(req.url)) req.url = '/console/'
        next()
      })
    },
  }
}

// The console is served by the Python proxy app under /console in production,
// so the built asset paths must be absolute under that prefix. In dev, Vite
// proxies /v1 to the Python process — no CORS handling ever lands in the
// FastAPI app.
// defineConfig comes from vitest/config, not vite: the `test` block below is
// vitest's and is not part of vite's UserConfig, so importing from 'vite'
// type-errors the moment anything type-checks this file.
export default defineConfig({
  base: '/console/',
  plugins: [shieldPageInDev(), TanStackRouterVite({ routesDirectory: 'src/routes', generatedRouteTree: 'src/routeTree.gen.ts' }), react()],
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  server: {
    // 5173 belongs to the Coriqo app's own frontend container. Sharing it
    // meant localhost:5173 reached whichever server won the IPv4/IPv6 race,
    // so one app's changes seemed to vanish. strictPort: fail loudly instead.
    port: 5174,
    strictPort: true,
    proxy: {
      '/v1': { target: process.env.BYOAI_PROXY_URL ?? 'http://127.0.0.1:8787', changeOrigin: true },
      // Coriqo shield (live capture ledger) — dev-only sidecar; the prod build
      // keeps serving the console build with the Python app. The proxy
      // rewrites Origin to the target so the shield's same-origin write rule
      // (Settings POSTs must come from the service's own page) holds in dev
      // too — Vite's changeOrigin only fixes Host, not Origin.
      '/shield-api': {
        target: process.env.SHIELD_URL ?? 'http://127.0.0.1:8300',
        changeOrigin: true,
        rewrite: p => p.replace(/^\/shield-api/, '/api'),
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            const origin = proxyReq.getHeader('origin')
            if (origin && String(origin).startsWith('http://localhost:')) {
              // The request path is already rewritten too, so Shield sees a
              // request that looks exactly like one from its own page.
              proxyReq.setHeader('origin', process.env.SHIELD_URL ?? 'http://127.0.0.1:8300')
            }
          })
        },
      },
    },
  },
  // Build straight into the Python package: hatchling ships whatever is in
  // src/byoai/console_static/ inside the wheel, so `npm run build` is the only
  // step between a source checkout and a working /console. The directory is
  // git-ignored — build output is never committed.
  build: {
    outDir: path.resolve(__dirname, '../src/byoai/console_static'),
    emptyOutDir: true,
    sourcemap: true,
  },
  test: { environment: 'jsdom', globals: true },
})
