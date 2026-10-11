import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// Node globals (no @types/node in this project).
declare const process: { env: Record<string, string | undefined> };

const apiTarget = process.env.MOTAHAI_API_URL ?? 'http://127.0.0.1:8000';

/**
 * Dev/preview proxy for the Stats API. The operator key (when OPERATOR_API_KEY is set in the
 * server's environment) is injected here, server-side, and never reaches the browser bundle:
 * it is not VITE_-prefixed and not passed to `define`.
 */
const apiProxy = {
  '/v1': {
    target: apiTarget,
    changeOrigin: true,
    configure: (proxy: {
      on(
        event: 'proxyReq',
        cb: (req: { getHeader(k: string): unknown; setHeader(k: string, v: string): void }) => void,
      ): void;
    }) => {
      proxy.on('proxyReq', (proxyReq) => {
        const key = process.env.OPERATOR_API_KEY;
        // A browser-supplied tenant key (Authorization header) always wins over the operator key.
        if (key && !proxyReq.getHeader('authorization')) proxyReq.setHeader('Authorization', `Bearer ${key}`);
      });
    },
  },
};

// The portal is served by the FastAPI backend under /app.
export default defineConfig({
  base: '/app/',
  plugins: [react()],
  server: { proxy: apiProxy },
  preview: { proxy: apiProxy },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  test: {
    environment: 'jsdom',
  },
});
