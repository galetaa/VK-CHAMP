import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 50_000,
  expect: { timeout: 7_500 },
  reporter: [['list'], ['html', { open: 'never' }], ['junit', { outputFile: 'test-results/junit.xml' }]],
  use: {
    baseURL: process.env.CASECHAMP_TEST_ORIGIN ?? 'http://localhost:18081',
    browserName: 'chromium',
    viewport: { width: 1440, height: 960 },
    actionTimeout: 10_000,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
});
