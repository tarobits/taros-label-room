import { test, expect } from './fixtures'
import { showEditorSettings, showEditorTab } from './editor-settings'

test('new barcode boxes fit their bars and margin warnings clear after moving', async ({
  page,
  request,
}) => {
  const response = await request.post('/api/templates', {
    data: {
      name: 'Barcode margins',
      document: { auto_length: false, length_mm: 60, elements: [] },
    },
  })
  expect(response.ok()).toBeTruthy()
  const template = await response.json()
  await page.goto(`/#/templates/${template.id}/edit`)
  await page.getByRole('button', { name: 'Add barcode', exact: true }).click()
  await showEditorTab(page, 'Content')
  await page.getByRole('textbox', { name: 'Content', exact: true }).fill('42')
  await showEditorSettings(page, 'Position & size')
  await page.getByLabel('Width (mm)', { exact: true }).fill('14')
  await page.getByLabel('X (mm)', { exact: true }).fill('1')
  await page.getByLabel('X (mm)', { exact: true }).press('Tab')
  const warning = page.getByText(/scanning margins are obstructed/)
  await expect(warning).toBeVisible()
  await expect(page.locator('.object-boundary')).toHaveAttribute('width', '14')
  await warning.scrollIntoViewIfNeeded()
  await page.screenshot({ path: 'test-results/barcode-margin-warning.png' })
  await page.getByLabel('X (mm)', { exact: true }).fill('6')
  await page.getByLabel('X (mm)', { exact: true }).press('Tab')
  await expect(warning).toBeHidden()
  await page.locator('.editor-board').screenshot({ path: 'test-results/barcode-tight-box.png' })
  await page.getByRole('button', { name: 'Save template', exact: true }).click()
  await expect(page.getByText('Revision 2', { exact: true })).toBeVisible()
  const saved = await (await request.get(`/api/templates/${template.id}`)).json()
  expect(saved.document.elements[0]).toMatchObject({
    width: 14,
    x: 6,
    barcode_quiet_zone_in_box: false,
  })
  await page.reload()
  await expect(page.locator('.object-boundary')).toHaveAttribute('width', '14')
  await expect(warning).toBeHidden()
})

test('opening and saving a legacy barcode keeps its original geometry and sizing mode', async ({
  page,
  request,
}) => {
  const response = await request.post('/api/templates', {
    data: {
      name: 'Legacy barcode',
      document: {
        length_mm: 60,
        elements: [
          { id: 'old', kind: 'barcode', content: '42', x: 6, y: 4, width: 24, height: 12 },
        ],
      },
    },
  })
  expect(response.ok()).toBeTruthy()
  const template = await response.json()
  await page.goto(`/#/templates/${template.id}/edit?element=old`)
  await expect(page.locator('.object-boundary')).toHaveAttribute('width', '24')
  await showEditorTab(page, 'Content')
  await page.getByRole('textbox', { name: 'Content', exact: true }).fill('43')
  await page.getByRole('textbox', { name: 'Content', exact: true }).press('Tab')
  await page.getByRole('button', { name: 'Save template', exact: true }).click()
  await expect(page.getByText('Revision 2', { exact: true })).toBeVisible()
  const saved = await (await request.get(`/api/templates/${template.id}`)).json()
  expect(saved.document.elements[0]).toMatchObject({
    x: 6,
    y: 4,
    width: 24,
    height: 12,
    barcode_quiet_zone_in_box: true,
  })
})
