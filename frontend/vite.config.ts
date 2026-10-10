import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The dev server proxies API calls to the dbx backend (`pixi run serve`).
export default defineConfig({
  plugins: [react()],
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
