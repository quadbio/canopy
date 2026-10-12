import { execSync } from 'node:child_process';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The commit the frontend was built from, sent with telemetry (it may differ from the server's).
function commit(): string {
  try {
    return execSync('git rev-parse --short=10 HEAD', { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
  } catch {
    return 'unknown';
  }
}

// The dev server proxies API calls to the canopy backend (`pixi run serve`).
export default defineConfig({
  plugins: [react()],
  define: { __CANOPY_BUILD__: JSON.stringify(commit()) },
  // deck.gl + luma.gl + Viv make one ~1.1 MB bundle (330 KB gzipped); it is cached by the browser
  // and served from the same machine. Code splitting can come with later views if load time shows.
  build: { chunkSizeWarningLimit: 1600 },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
