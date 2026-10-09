import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The dev server proxies API calls to the dbx backend (`pixi run serve`).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
