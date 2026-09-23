import { expect, test } from '@playwright/test'

test('employee completes a recommendation and sees live readiness diff',async({page})=>{
  await page.goto('/login')
  await page.getByLabel('Find a synthetic profile').fill('E0028')
  await page.getByLabel('Continue as').selectOption('E0028')
  await page.getByRole('button',{name:/Open Career Quest/}).click()
  await expect(page.getByText('Your next step')).toBeVisible()
  await page.getByRole('button',{name:'Why this?'}).first().click()
  await expect(page.getByRole('dialog')).toContainText('critical')
  await page.getByRole('button',{name:'Close'}).click()
  await page.getByRole('button',{name:'Mark completed'}).first().click()
  await expect(page.getByRole('dialog',{name:'What changed'})).toBeVisible()
  await expect(page.getByRole('dialog',{name:'What changed'})).toContainText('Career readiness')
})
