import { test, expect } from './fixtures'
import { chooseTapeProfile } from './tape-profile-picker-helpers'

test('portrait orientation saves per template, preserves elements, and supports undo and reload', async ({
  page,
  request,
}) => {
  const response = await request.post('/api/templates', {
    data: {
      name: 'Portrait shelf label',
      document: {
        length_mm: 60,
        elements: [
          { id: 'caption', kind: 'text', x: 4, y: 4, width: 16, height: 8, content: 'Shelf 42' },
        ],
      },
    },
  })
  expect(response.ok()).toBeTruthy()
  const template = await response.json()
  await page.goto(`/#/templates/${template.id}/edit`)
  const orientation = page.getByRole('combobox', { name: 'Orientation', exact: true })
  const canvas = page.locator('.editor-canvas')
  await expect(orientation).toHaveValue('horizontal')
  await orientation.selectOption('vertical')
  await expect(canvas).toHaveAttribute('viewBox', '0 0 24 60')
  await expect(page.locator('.editor-ruler-top')).toHaveAttribute('width', '192')
  await expect(page.locator('.editor-ruler-left')).toHaveAttribute('height', '480')
  await expect(page.locator('.object-boundary')).toHaveAttribute('x', '4')
  await expect(page.locator('.object-boundary')).toHaveAttribute('y', '4')
  await page.getByRole('button', { name: 'Undo', exact: true }).click()
  await expect(orientation).toHaveValue('horizontal')
  await expect(canvas).toHaveAttribute('viewBox', '0 0 60 24')
  await page.getByRole('button', { name: 'Redo', exact: true }).click()
  await expect(orientation).toHaveValue('vertical')
  await page.getByRole('button', { name: 'Save template', exact: true }).click()
  await expect(page.getByText('Revision 2', { exact: true })).toBeVisible()
  const saved = await (await request.get(`/api/templates/${template.id}`)).json()
  expect(saved.document.orientation).toBe('vertical')
  expect(saved.document.elements).toEqual(template.document.elements)
  await page.reload()
  await expect(orientation).toHaveValue('vertical')
  await expect(canvas).toHaveAttribute('viewBox', '0 0 24 60')
  await page.getByRole('button', { name: 'Add text', exact: true }).click()
  await expect(page.locator('.object-boundary').last().locator('..')).toHaveAttribute(
    'transform',
    /rotate\(0,/,
  )
  await page.getByRole('button', { name: 'Save template', exact: true }).click()
  await expect(page.getByText('Revision 3', { exact: true })).toBeVisible()
  await page
    .locator('.editor-board')
    .screenshot({ path: 'test-results/portrait-editor-desktop.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await orientation.scrollIntoViewIfNeeded()
  await page.screenshot({ path: 'test-results/portrait-settings-mobile.png' })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.locator('.editor-canvas-scroll').scrollIntoViewIfNeeded()
  await page.screenshot({ path: 'test-results/portrait-editor-mobile.png' })
  await page.goto('/#/templates/new')
  await expect(orientation).toHaveValue('horizontal')
})

test('portrait dragging, resizing and keyboard movement follow the screen axes', async ({
  page,
  request,
}) => {
  const response = await request.post('/api/templates', {
    data: {
      name: 'Portrait gestures',
      document: {
        orientation: 'vertical',
        length_mm: 60,
        elements: [
          { id: 'box', kind: 'rectangle', x: 4, y: 10, width: 8, height: 8, filled: true },
        ],
      },
    },
  })
  expect(response.ok()).toBeTruthy()
  const template = await response.json()
  await page.goto(`/#/templates/${template.id}/edit?element=box`)
  await expect(page.getByRole('button', { name: 'Save template', exact: true })).toBeEnabled()
  const boundary = page.locator('.object-boundary')
  await boundary.scrollIntoViewIfNeeded()
  const box = (await boundary.boundingBox())!
  await page.mouse.move(box.x + 8, box.y + 8)
  await page.mouse.down()
  await page.mouse.move(box.x + 24, box.y + 48)
  await expect(page.locator('.editor-drag-preview')).toBeVisible()
  await page.mouse.up()
  await expect(boundary).toHaveAttribute('x', '6')
  await expect(boundary).toHaveAttribute('y', '15')
  const handle = page.locator('.editor-canvas circle')
  const corner = (await handle.boundingBox())!
  await page.mouse.move(corner.x + corner.width / 2, corner.y + corner.height / 2)
  await page.mouse.down()
  await page.mouse.move(corner.x + corner.width / 2 + 16, corner.y + corner.height / 2 + 24)
  await page.mouse.up()
  await expect(boundary).toHaveAttribute('width', '10')
  await expect(boundary).toHaveAttribute('height', '11')
  await page.locator('.editor-canvas').focus()
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('ArrowRight')
  await expect(boundary).toHaveAttribute('x', '6.5')
  await expect(boundary).toHaveAttribute('y', '15.5')
  await page.getByRole('button', { name: 'Save template', exact: true }).click()
  await expect(page.getByText('Revision 2', { exact: true })).toBeVisible()
  const saved = await (await request.get(`/api/templates/${template.id}`)).json()
  expect(saved.document.elements[0]).toMatchObject({
    x: 6.5,
    y: 15.5,
    width: 10,
    height: 11,
    rotation: 0,
  })
})

test('portrait tape changes preserve horizontal placement and automatic length grows downwards', async ({
  page,
  request,
}) => {
  const profiles = []
  for (const width of [24, 12]) {
    const response = await request.post('/api/tape-profiles', {
      data: { name: `Portrait ${width}`, width_mm: width },
    })
    profiles.push(await response.json())
  }
  const response = await request.post('/api/templates', {
    data: {
      name: 'Portrait sizing',
      document: {
        orientation: 'vertical',
        width_mm: 24,
        length_mm: 160,
        auto_length: true,
        preview_tape: {
          id: profiles[0].id,
          name: profiles[0].name,
          foreground: '#000000',
          background: '#ffffff',
        },
        elements: [{ id: 'box', kind: 'rectangle', x: 10, y: 140, width: 4, height: 8 }],
      },
    },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  const template = await response.json()
  await page.goto(`/#/templates/${template.id}/edit`)
  const canvas = page.locator('.editor-canvas')
  await expect(canvas).toHaveAttribute('viewBox', '0 0 24 149')
  await chooseTapeProfile(page, profiles[1].id)
  await expect(canvas).toHaveAttribute('viewBox', '0 0 12 149')
  const boundary = page.locator('.object-boundary')
  await expect.poll(async () => Number(await boundary.getAttribute('x'))).toBeCloseTo(3.93, 1)
  await expect(boundary).toHaveAttribute('y', '140')
  await expect(boundary).toHaveAttribute('width', '4')
  await expect(boundary).toHaveAttribute('height', '8')
  await page.getByRole('button', { name: 'Undo', exact: true }).click()
  await expect(canvas).toHaveAttribute('viewBox', '0 0 24 149')
  await expect(boundary).toHaveAttribute('x', '10')
})
