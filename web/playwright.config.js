import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  outputDir: './node_modules/.cache/intake-test-results',
  use: { baseURL: 'http://127.0.0.1:5190', browserName: 'chromium', launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } },
  webServer: {
    command: 'npm run dev -- --port 5190 --mode intake-test',
    url: 'http://127.0.0.1:5190',
    reuseExistingServer: false,
  },
});
