import { defineConfig, devices } from '@playwright/test';

// Both servers are disposable fixtures. One uses API mocks; the other uses the
// actual isolated controller and native executors with fake Oracle commands.
// Neither accepts a configurable production URL or reuses live port 8765.
export default defineConfig({
  testDir: './tests/browser',
  testMatch: '**/*.spec.mjs',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  timeout: 30_000,
  reporter: [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }], ['json', { outputFile: 'test-results/browser-results.json' }]],
  use: {
    baseURL: 'http://127.0.0.1:18765',
    actionTimeout: 10_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    serviceWorkers: 'block',
  },
  projects: [
    { name: 'fixture-chromium', testIgnore: '**/*.integration.spec.mjs', use: { ...devices['Desktop Chrome'] } },
    { name: 'backend-chromium', testMatch: '**/*.integration.spec.mjs', use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:18766' } },
  ],
  webServer: [{
    command: 'node tests/browser/fixture_server.mjs',
    url: 'http://127.0.0.1:18765/__fixture_health',
    reuseExistingServer: false,
    timeout: 10_000,
  }, {
    command: 'python3 -B tests/browser/integration_server.py',
    url: 'http://127.0.0.1:18766/api/health',
    reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5000 },
    timeout: 20_000,
  }],
});
