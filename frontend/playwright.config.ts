import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir:'./e2e', timeout:30_000, use:{baseURL:'http://127.0.0.1:5173',trace:'retain-on-failure',launchOptions:{executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'}},
  projects:[{name:'mobile-chromium',use:{...devices['iPhone 13'],viewport:{width:390,height:844}}}],
})
