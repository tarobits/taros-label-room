import { test, expect } from './fixtures'

test('drag artwork stays live without server renders until release, and recovers from failure', async ({
  page,
  request,
}) => {
  const response = await request.post('/api/templates', {
    data: {
      name: 'Live dragging',
      document: {
        length_mm: 60,
        elements: [
          { id: 'box', kind: 'rectangle', x: 8, y: 5, width: 8, height: 4, filled: true },
          { id: 'overlap', kind: 'rectangle', x: 12, y: 6, width: 8, height: 4 },
          {
            id: 'text',
            kind: 'text',
            x: 25,
            y: 5,
            width: 25,
            height: 8,
            content: 'Äg 42',
            font_weight: 550.5,
          },
        ],
      },
    },
  })
  expect(response.ok()).toBeTruthy()
  const template = await response.json()
  await page.goto(`/#/templates/${template.id}/edit?element=box`)
  await expect(page.getByRole('button', { name: 'Save template', exact: true })).toBeEnabled()
  let renders = 0
  let fail = true
  await page.route('**/api/preview', async (route) => {
    renders++
    if (fail) await route.fulfill({ status: 500, json: { detail: 'Render unavailable' } })
    else await route.continue()
  })
  const boundary = page.locator('.object-boundary').first()
  await boundary.scrollIntoViewIfNeeded()
  const box = (await boundary.boundingBox())!
  const origin = { x: box.x + box.width / 4, y: box.y + box.height / 2 }
  await page.mouse.move(origin.x, origin.y)
  await page.mouse.down()
  await page.mouse.move(origin.x + 24, origin.y + 8)
  // A stationary pointer during a held gesture must not fire the debounce timer.
  await page.waitForTimeout(650)
  expect(renders).toBe(0)
  const live = page.locator('.editor-drag-preview')
  await expect(live).toBeVisible()
  const source = await live.getAttribute('href')
  const transform = await live.evaluate((node) => {
    const xml = decodeURIComponent(node.getAttribute('href')!.split(',').slice(1).join(','))
    const svg = new DOMParser().parseFromString(xml, 'image/svg+xml')
    return svg.querySelector('[data-element-id="box"]')!.parentElement!.getAttribute('transform')
  })
  expect(transform).toBe('translate(11,6) scale(1,1) translate(-8,-5)')
  await page.mouse.move(origin.x + 32, origin.y + 8)
  await expect(live).not.toHaveAttribute('href', source!)
  await page.locator('.editor-board').screenshot({ path: 'test-results/live-drag.png' })
  await page.mouse.up()
  await expect(page.getByText('Render unavailable', { exact: true })).toBeVisible()
  // Keep the last good artwork available when rendering fails.
  await expect(page.locator('.editor-canvas > image').first()).toBeVisible()
  fail = false
  const next = (await boundary.boundingBox())!
  await page.mouse.move(next.x + 8, next.y + 4)
  await page.mouse.down()
  await page.mouse.move(next.x + 24, next.y + 4)
  await page.mouse.up()
  await expect(page.getByRole('button', { name: 'Save template', exact: true })).toBeEnabled()
  await expect(page.getByText('Render unavailable', { exact: true })).toBeHidden()
  await expect(live).toHaveCount(0)
  expect(renders).toBe(2)
})
