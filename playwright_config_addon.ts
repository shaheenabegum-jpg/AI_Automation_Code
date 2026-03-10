/**
 * ADD THIS to your QA_Automation_Banorte playwright.config.ts
 * (merge these projects into your existing config)
 *
 * Covers all devices available in the RunTab dropdowns.
 */
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  // ... your existing config ...
  projects: [
    // ── Desktop ────────────────────────────────────────────────────────────────
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
    },

    // ── Mobile — confirmed: devices['iPhone 13'] ───────────────────────────────
    {
      name: 'mobile-safari',
      use: { ...devices['iPhone 13'] },
    },
    {
      name: 'mobile-chrome',
      use: { ...devices['Pixel 5'] },   // covers Pixel 7 / Galaxy S23 emulation
    },

    // ── Tablet ─────────────────────────────────────────────────────────────────
    {
      name: 'tablet-safari',
      use: { ...devices['iPad Pro 11'] },
    },
  ],

  // Allure reporter (required for report generation)
  reporter: [
    ['line'],
    ['allure-playwright', {
      detail: true,
      outputFolder: 'allure-results',
      suiteTitle: false,
    }],
  ],
});
