import { test as stationTest, type Page, type Locator } from '@playwright/test'
import { resolve } from 'node:path'
import { test, expect } from './fixtures'

const address = 'AA:BB:CC:DD:EE:FF'
async function bluetooth(page: Page, endpoint: string) {
  const device = { address, name: 'PT-P710BT1234', paired: false }
  let attempts = 0
  let finish: () => void = () => {}
  const pending = new Promise<void>((done) => {
    finish = done
  })
  await page.route(`**${endpoint}**`, async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/pair')) {
      expect(route.request().postDataJSON()).toEqual({ address })
      attempts++
      if (attempts === 1)
        return route.fulfill({ status: 503, json: { detail: 'Pairing interrupted' } })
      await pending
      device.paired = true
      return route.fulfill({ json: device })
    }
    return route.fulfill({ json: [device] })
  })
  return { finish }
}

async function pairWithRetry(page: Page, form: Locator, save: Locator, finish: () => void) {
  await form.getByRole('combobox', { name: 'Connection', exact: true }).selectOption('bluetooth')
  await expect(form.getByRole('button', { name: 'Find printers', exact: true })).toBeEnabled()
  await form.getByRole('button', { name: 'Find printers', exact: true }).click()
  await form.getByRole('combobox', { name: 'Bluetooth printer', exact: true }).selectOption(address)
  await form.getByRole('button', { name: 'Pair', exact: true }).click()
  await expect(form.getByRole('alert')).toHaveText('Pairing interrupted')
  await form.getByRole('button', { name: 'Pair', exact: true }).click()
  await expect(save).toBeDisabled()
  await expect(form.getByRole('combobox', { name: 'Connection', exact: true })).toBeDisabled()
  finish()
  await expect(form.getByRole('button', { name: 'Pair', exact: true })).toHaveCount(0)
  await expect(form.getByRole('alert')).toHaveCount(0)
  await expect(save).toBeEnabled()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    page.viewportSize()!.width,
  )
}

async function saveWhenIdle(page: Page, form: Locator, save: Locator) {
  // The live fixture polls hardware every 100 ms; a save may meet that lock.
  // Retry only the explicit busy response, as an operator can from the form.
  for (let attempt = 0; attempt < 5; attempt++) {
    const [response] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'PUT' && /\/api\/printers\//.test(response.url()),
      ),
      save.click(),
    ])
    if (response.ok()) {
      await expect(form).toBeHidden()
      return
    }
    expect(response.status()).toBe(409)
    expect((await response.json()).detail).toBe('Printer is busy; try again when it is idle')
  }
  throw new Error('Printer remained busy across five explicit save attempts')
}

for (const width of [1280, 390]) {
  test(`Bluetooth pairing and saved USB round trip at ${width}px`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 900 })
    const radio = await bluetooth(page, '/api/printer-bluetooth')
    const [printer] = await (await request.get('/api/printers')).json()
    await page.goto('/#/printers')
    const card = page
      .locator('.printer-device')
      .filter({ has: page.getByRole('heading', { name: printer.name, exact: true }) })
    await card.getByRole('button', { name: 'Configure', exact: true }).click()
    const form = page
      .locator('form')
      .filter({ has: page.getByRole('heading', { name: 'Edit printer', exact: true }) })
    const save = form.getByRole('button', { name: 'Save printer', exact: true })
    await pairWithRetry(page, form, save, radio.finish)
    await page.screenshot({
      path: `test-results/bluetooth-main-${width}.png`,
      fullPage: true,
      animations: 'disabled',
    })
    await saveWhenIdle(page, form, save)
    let updated = (await (await request.get('/api/printers')).json()).find(
      (item: { id: string }) => item.id === printer.id,
    )
    expect(updated.config.transport).toBe('bluetooth')
    expect(updated.config.bluetooth_address).toBe(address)
    await card.getByRole('button', { name: 'Configure', exact: true }).click()
    await expect(
      form.getByRole('combobox', { name: 'Bluetooth printer', exact: true }),
    ).toHaveValue(address)
    await expect(form.getByRole('combobox', { name: 'Connection', exact: true })).toBeEnabled()
    await form.getByRole('combobox', { name: 'Connection', exact: true }).selectOption('usb')
    await expect(form.getByLabel('USB serial number', { exact: true })).toHaveValue('UI-DESK')
    await saveWhenIdle(page, form, save)
    updated = (await (await request.get('/api/printers')).json()).find(
      (item: { id: string }) => item.id === printer.id,
    )
    expect(updated.config.transport).toBe('usb')
    await card.getByRole('button', { name: 'Configure', exact: true }).click()
    await form.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(form).toBeHidden()
  })
}

for (const width of [1024, 390]) {
  stationTest(`Companion local Bluetooth form at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 600 })
    const saved: { configuration: { transport: string; bluetooth_address: string } }[] = []
    await page.route('http://localhost:12345/**', async (route) => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/station')
        return route.fulfill({
          json: {
            name: 'Workshop station',
            online: true,
            locked: false,
            lock_remaining_seconds: 295,
            user: {
              id: 'owner',
              username: 'Owner',
              language: 'en',
              permissions: ['printers.manage'],
              printer_permissions: {},
              groups: {},
            },
            users: [],
            printers: [],
            removed_printers: [],
            csrf_token: 'fixture',
            stop_policy: {},
            default_language: 'en',
          },
        })
      if (path === '/api/station/printers') {
        saved.push(route.request().postDataJSON())
        return route.fulfill({ json: { ok: true } })
      }
      if (path === '/api/health') return route.fulfill({ json: { healthy: true } })
      if (path.startsWith('/api/')) return route.fulfill({ json: [] })
      if (path.startsWith('/assets/'))
        return route.fulfill({ path: resolve(`dist-companion${path}`) })
      if (path === '/')
        return route.fulfill({
          path: resolve('dist-companion/companion.html'),
          contentType: 'text/html',
        })
      return route.abort()
    })
    const radio = await bluetooth(page, '/api/station/bluetooth')
    await page.goto('http://localhost:12345/')
    await page.getByRole('button', { name: 'Add printer', exact: true }).click()
    const form = page.getByRole('dialog', { name: 'Add printer', exact: true })
    await form.getByLabel('Name', { exact: true }).fill('Wireless printer')
    const save = form.getByRole('button', { name: 'Add printer', exact: true })
    await pairWithRetry(page, form, save, radio.finish)
    await page.screenshot({
      path: `test-results/bluetooth-companion-${width}.png`,
      fullPage: true,
      animations: 'disabled',
    })
    await save.click()
    await expect(form).toBeHidden()
    expect(saved).toHaveLength(1)
    expect(saved[0]!.configuration).toMatchObject({
      transport: 'bluetooth',
      bluetooth_address: address,
    })
    await page.getByRole('button', { name: 'Add printer', exact: true }).click()
    await form.getByRole('button', { name: 'Close', exact: true }).click()
    await expect(form).toBeHidden()
  })
}
