import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir: './e2e', timeout: 60000, workers: 1, retries: 0,
  use: {baseURL: process.env.EVIREEL_TEST_URL || 'http://127.0.0.1:8875', viewport: {width: 1440, height: 1000},
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH} : {},
    trace: 'retain-on-failure', screenshot: 'only-on-failure'},
  reporter: [['list'], ['html', {open: 'never'}]],
});
