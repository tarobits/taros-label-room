import { test, expect, type Page } from '@playwright/test'
import { resolve } from 'node:path'

type Layout = {
  id: string
  buttons: ({ id: string; label: string; disabled: boolean; icon: string } | null)[]
}
declare global {
  interface Window {
    foxoeFixture: { layouts: Layout[]; messages: unknown[]; connected: boolean }
  }
}
async function current(page: Page) {
  return page.evaluate(() => window.foxoeFixture.layouts.at(-1))
}
async function press(page: Page, id: string, layout?: string) {
  await page.evaluate(
    async ({ id, layout }) => {
      await new Promise(requestAnimationFrame)
      window.postMessage(
        {
          source: 'foxoe:bridge',
          type: 'action',
          id,
          layout: layout ?? window.foxoeFixture.layouts.at(-1)?.id,
        },
        location.origin,
      )
    },
    { id, layout },
  )
}
async function ready(page: Page, id: string) {
  await expect
    .poll(async () => (await current(page))?.buttons.some((b) => b?.id === id && !b.disabled))
    .toBe(true)
}
async function fixture(page: Page, many = false) {
  const printers = Array.from({ length: many ? 14 : 2 }, (_, i) => ({
    local_id: `local-${i}`,
    id: `printer-${i}`,
    name: `Printer ${i + 1}`,
    blocked: false,
    printing: false,
    can_stop: false,
    status: { connected: true, tape_width_mm: 24, error: null },
  }))
  const state = {
    locked: true,
    online: true,
    reachable: true,
    language: 'en',
    attempts: [] as string[],
    activity: 0,
  }
  await page.addInitScript(() => {
    // Plain HTTP LAN origins lack randomUUID, but the companion supports them.
    Object.defineProperty(crypto, 'randomUUID', { value: undefined })
    window.foxoeFixture = { layouts: [], messages: [], connected: true }
    window.addEventListener('message', (event) => {
      if (event.source !== window || event.data?.source !== 'foxoe:app') return
      const message = event.data
      window.foxoeFixture.messages.push(message)
      if (message.type === 'hello')
        window.postMessage(
          { source: 'foxoe:bridge', type: 'connection', connected: true },
          location.origin,
        )
      else if (message.type === 'layout' && window.foxoeFixture.connected) {
        window.foxoeFixture.layouts.push(message.layout)
        window.postMessage(
          { source: 'foxoe:bridge', type: 'ack', request: message.request },
          location.origin,
        )
      }
    })
  })
  await page.route('http://localhost:12345/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/station') {
      if (!state.reachable) {
        await route.fulfill({ status: 503, json: { detail: 'Offline' } })
        return
      }
      await route.fulfill({
        json: {
          name: 'FoxOE station',
          online: state.online,
          locked: state.locked,
          default_language: state.language,
          users: [{ id: 'alice', username: 'Alice' }],
          single_user_id: null,
          user: state.locked
            ? null
            : {
                id: 'alice',
                username: 'Alice',
                permissions: [],
                location_id: 'local',
                groups: {},
                printer_permissions: Object.fromEntries(
                  printers.slice(0, many ? 14 : 1).map((p) => [p.id, ['printer.view']]),
                ),
              },
          printers,
          removed_printers: [],
          csrf_token: 'fixture',
          stop_policy: {},
          lock_remaining_seconds: 300,
          pin_keypad: { enabled: true, shuffle: 'each_keypress' },
        },
      })
    } else if (path === '/api/station/unlock') {
      const body = route.request().postDataJSON()
      state.attempts.push(body.pin)
      if (body.pin !== '1234')
        await route.fulfill({ status: 422, json: { detail: 'Incorrect PIN' } })
      else {
        state.locked = false
        await route.fulfill({ json: { ok: true } })
      }
    } else if (path === '/api/station/lock') {
      state.locked = true
      await route.fulfill({ json: { ok: true } })
    } else if (path === '/api/station/activity') {
      state.activity++
      await route.fulfill({ json: { lock_remaining_seconds: 300 } })
    } else if (path === '/api/proxy/status') {
      await route.fulfill({
        json: {
          printer_id: new URL(route.request().url()).searchParams.get('printer_id'),
          needs_review: [],
          queues: [],
          connected: true,
          printing: null,
        },
      })
    } else if (path.startsWith('/api/proxy/')) await route.fulfill({ json: [] })
    else if (path.startsWith('/assets/'))
      await route.fulfill({ path: resolve(`dist-companion${path}`) })
    else if (path === '/')
      await route.fulfill({
        path: resolve('dist-companion/companion.html'),
        contentType: 'text/html',
      })
    else await route.abort()
  })
  await page.goto('http://localhost:12345/')
  return state
}

test('FoxOE unlocks through the shared shuffled PIN form, then exposes only permitted printers', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1024, height: 600 })
  const state = await fixture(page)
  await ready(page, 'unlock')
  await press(page, 'unlock')
  await ready(page, 'user.0')
  await press(page, 'user.0')
  await ready(page, 'digit.1')
  const input = page.getByLabel('PIN', { exact: true })
  await expect(input).toHaveAttribute('type', 'password')
  await press(page, 'confirm')
  expect(state.attempts).toEqual([])
  for (const digit of '1111') {
    const old = (await current(page))!
    await press(page, `digit.${digit}`)
    await expect.poll(async () => (await current(page))?.id).not.toBe(old.id)
    const touchDigits = await page.locator('.pin-digit').allTextContents()
    expect(
      (await current(page))!.buttons.filter((b) => b?.id.startsWith('digit.')).map((b) => b!.label),
    ).toEqual([...touchDigits.slice(0, 6), touchDigits[9], ...touchDigits.slice(6, 9)])
  }
  await ready(page, 'confirm')
  await press(page, 'confirm')
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('Incorrect PIN')
  await expect(input).toHaveValue('')
  for (const digit of '1234') {
    const old = (await current(page))!.id
    await ready(page, `digit.${digit}`)
    await press(page, `digit.${digit}`)
    await expect.poll(async () => (await current(page))?.id).not.toBe(old)
  }
  await page.screenshot({ path: 'test-results/foxoe-pin.png', animations: 'disabled' })
  const oldPin = (await current(page))!.id
  await ready(page, 'confirm')
  await press(page, 'confirm')
  await press(page, 'confirm', oldPin)
  await ready(page, 'printer.0')
  expect(state.attempts).toEqual(['1111', '1234'])
  expect((await current(page))!.buttons.some((b) => b?.label === 'Printer 2')).toBe(false)
  expect(JSON.stringify(await page.evaluate(() => window.foxoeFixture.messages))).not.toContain(
    '1234',
  )
  await press(page, 'digit.1', oldPin)
  await press(page, 'printer.0')
  await expect(page.getByRole('button', { name: 'Close controls', exact: true })).toBeVisible()
  expect(state.activity).toBe(1)
  await page.screenshot({ path: 'test-results/foxoe-printer.png', animations: 'disabled' })
  await page.getByRole('button', { name: 'Lock', exact: true }).click()
  await ready(page, 'unlock')
  await expect(page.getByRole('button', { name: 'Close controls', exact: true })).toHaveCount(0)
})

test('pagination, modal blocking, offline state and reconnect invalidate old deck actions', async ({
  page,
}) => {
  const state = await fixture(page, true)
  state.locked = false
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await ready(page, 'printer.0')
  await ready(page, 'next')
  await press(page, 'next')
  await ready(page, 'printer.12')
  expect((await current(page))!.buttons.length).toBe(14)
  const old = (await current(page))!.id
  await page.evaluate(() => {
    const dialog = document.createElement('dialog')
    dialog.id = 'fixture-dialog'
    document.body.append(dialog)
    dialog.showModal()
  })
  await expect
    .poll(async () => (await current(page))?.buttons.every((b) => !b || b.disabled))
    .toBe(true)
  await press(page, 'printer.12', old)
  await expect(page.getByRole('button', { name: 'Close controls', exact: true })).toHaveCount(0)
  await page.evaluate(() => document.getElementById('fixture-dialog')!.remove())
  await ready(page, 'printer.12')
  const beforeDisconnect = (await current(page))!.id
  await page.evaluate(() => {
    window.foxoeFixture.connected = false
    window.postMessage(
      { source: 'foxoe:bridge', type: 'connection', connected: false },
      location.origin,
    )
  })
  await press(page, 'printer.12', beforeDisconnect)
  await expect(page.getByRole('button', { name: 'Close controls', exact: true })).toHaveCount(0)
  await page.evaluate(() => {
    window.foxoeFixture.connected = true
    window.postMessage(
      { source: 'foxoe:bridge', type: 'connection', connected: true },
      location.origin,
    )
  })
  await expect.poll(async () => (await current(page))!.id).not.toBe(beforeDisconnect)
  await press(page, 'printer.12', beforeDisconnect)
  await expect(page.getByRole('button', { name: 'Close controls', exact: true })).toHaveCount(0)
  await press(page, 'printer.12')
  await expect(page.getByRole('button', { name: 'Close controls', exact: true })).toBeVisible()
  state.online = false
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await expect
    .poll(async () => (await current(page))?.buttons.every((b) => !b || b.disabled))
    .toBe(true)
  state.online = true
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await ready(page, 'printer.12')
})
