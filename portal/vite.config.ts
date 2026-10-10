import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The portal is served by the FastAPI backend under /app.
export default defineConfig({
  base: '/app/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  test: {
    environment: 'jsdom',
  },
});
