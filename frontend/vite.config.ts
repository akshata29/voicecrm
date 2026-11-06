import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The API runs on :8010 by default; set VITE_API_PROXY to point the dev/preview proxy elsewhere.
// 127.0.0.1 rather than "localhost": on Windows localhost can resolve to ::1 first, where another program (a WSL/Docker relay) may be listening on the same port.
const apiProxy = {
  '/api': {
    target: process.env.VITE_API_PROXY ?? 'http://127.0.0.1:8010',
    changeOrigin: true,
  },
};

// `npm run dev:mock` runs with --mode mock, which loads .env.mock (VITE_USE_MOCK=1).
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: apiProxy,
  },
  preview: {
    port: 4173,
    proxy: apiProxy,
  },
  build: {
    chunkSizeWarningLimit: 900,
  },
});
