import { defineConfig, devices } from '@playwright/test';

// End-to-end tests against a real `canopy serve` on the example datasets, serving the built
// frontend (run `pnpm build` first; `pixi run -e dev e2e` does both).
const port = Number(process.env.CANOPY_E2E_PORT ?? 8791);

export default defineConfig({
  testDir: 'e2e',
  outputDir: 'test-results',
  timeout: 60_000,
  expect: { timeout: 20_000, toHaveScreenshot: { maxDiffPixelRatio: 0.02 } },
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    viewport: { width: 1440, height: 900 },
    colorScheme: 'light',
    trace: 'retain-on-failure',
    // the server's access token (scripts/e2e-server.sh), as the link would set it
    storageState: {
      cookies: [
        {
          name: 'canopy_token',
          value: process.env.CANOPY_TOKEN ?? 'e2e-token',
          domain: '127.0.0.1',
          path: '/',
          expires: -1,
          httpOnly: true,
          secure: false,
          sameSite: 'Strict',
        },
      ],
      origins: [],
    },
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 900 },
        // Software WebGL, so the tests run without a GPU (CI, containers).
        launchOptions: { args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] },
      },
    },
    // The owner's setting: the Mac's GPU at 2x pixel density (pixi run -e dev e2e-gpu).
    ...(process.env.CANOPY_E2E_GPU === '1'
      ? [
          {
            name: 'gpu-2x',
            use: {
              ...devices['Desktop Chrome'],
              viewport: { width: 1440, height: 900 },
              deviceScaleFactor: 2,
              launchOptions: { args: ['--use-angle=metal', '--enable-gpu', '--ignore-gpu-blocklist'] },
            },
          },
        ]
      : []),
  ],
  webServer: {
    command: 'bash ../scripts/e2e-server.sh',
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: { CANOPY_E2E_PORT: String(port) },
  },
});
