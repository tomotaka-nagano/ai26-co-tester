import { expect, test } from '@playwright/test'

const url = 'http://127.0.0.1:8000/ui/'

async function expectNoHorizontalOverflow(page: import('@playwright/test').Page) {
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
}

test('verification workspace renders on desktop and mobile', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(url)
  await expect(page.getByRole('heading', { name: '結合検証を開始' })).toBeVisible()
  await expect(page.getByLabel('要求仕様書')).toBeVisible()
  await expect(page.getByRole('button', { name: '検証を開始' })).toBeVisible()
  await expectNoHorizontalOverflow(page)
  await page.screenshot({ path: 'test-results/ui-desktop.png', fullPage: true })

  await page.setViewportSize({ width: 390, height: 844 })
  await page.reload()
  await expect(page.getByRole('heading', { name: '結合検証を開始' })).toBeVisible()
  await expect(page.getByRole('button', { name: '検証を開始' })).toBeVisible()
  await expectNoHorizontalOverflow(page)
  await page.screenshot({ path: 'test-results/ui-mobile.png', fullPage: true })
})
