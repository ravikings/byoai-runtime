import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { TanStackRouterVite } from '@tanstack/router-plugin/vite'
import path from 'node:path'

// The console is served by the Python proxy app under /console in production,
// so the built asset paths must be absolute under that prefix. In dev, Vite
// proxies /v1 to the Python process — no CORS handling ever lands in the
// FastAPI app.
// defineConfig comes from vitest/config, not vite: the `test` block below is
// vitest's and is not part of vite's UserConfig, so importing from 'vite'
// type-errors the moment anything type-checks this file.
export default defineConfig({
  base: '/console/',
  plugins: [TanStackRouterVite({ routesDirectory: 'src/routes', generatedRouteTree: 'src/routeTree.gen.ts' }), react()],
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  server: {
    port: 5173,
    proxy: {
      '/v1': { target: process.env.BYOAI_PROXY_URL ?? 'http://127.0.0.1:8787', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
  test: { environment: 'jsdom', globals: true },
})
