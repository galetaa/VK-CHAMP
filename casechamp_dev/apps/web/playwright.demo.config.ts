import { defineConfig } from '@playwright/test';
import common from './playwright.config';

export default defineConfig({
  ...common,
  testMatch: '**/demo.video.ts',
  timeout: 120_000,
  reporter: 'list',
  use: { ...common.use, launchOptions: { slowMo: 350 } },
});
