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
    no_automatic_printing: false,
    status: { connected: true, tape_width_mm: 24, error: null },
  }))
  const state = {
    locked: true,
    online: true,
    reachable: true,
    language: 'en',
    attempts: [] as string[],
    activity: 0,
    printers,
    permissions: ['printer.view'],
    held: false,
    stopPolicy: {} as Record<string, boolean>,
    holds: [] as boolean[],
    stops: [] as { printer: string; mode: string }[],
    prints: [] as { copy_ids: string[] }[],
    failHold: false,
    holdWait: Promise.resolve(),
    shortage: false,
    queue: false,
    queueCopy: 'copy-1',
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
    if (path === '/api/health') {
      await route.fulfill({ json: { healthy: state.reachable } })
    } else if (path === '/api/station') {
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
                  printers.slice(0, many ? 14 : 1).map((p) => [p.id, state.permissions]),
                ),
              },
          printers,
          removed_printers: [],
          csrf_token: 'fixture',
          stop_policy: state.stopPolicy,
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
    } else if (path.endsWith('/stop')) {
      state.stops.push({ printer: path, mode: route.request().postDataJSON().mode })
      await route.fulfill({ json: { ok: true } })
    } else if (path === '/api/proxy/printer/automatic-printing') {
      state.holds.push(route.request().postDataJSON().held)
      await state.holdWait
      if (state.failHold) {
        state.failHold = false
        await route.fulfill({ status: 503, json: { detail: 'Hold failed' } })
      } else {
        state.held = route.request().postDataJSON().held
        await route.fulfill({ json: { ok: true } })
      }
    } else if (path === '/api/proxy/queue/print') {
      state.prints.push(route.request().postDataJSON())
      if (state.shortage)
        await route.fulfill({
          status: 409,
          json: {
            detail: {
              code: 'tape_shortage',
              message: 'Not enough tape',
              confirmation: 'fixture-shortage',
            },
          },
        })
      else await route.fulfill({ json: { ok: true } })
    } else if (path === '/api/proxy/queue/groups') {
      await route.fulfill({
        json: state.queue
          ? [
              {
                id: 'group',
                run_id: 'run',
                name: 'Test labels',
                jobs: [
                  {
                    id: 'job',
                    process_name: 'Test labels',
                    can_print: true,
                    profile_name: '24 mm',
                    labels: [{ id: state.queueCopy, state: 'queued' }],
                    payers: [],
                  },
                ],
              },
            ]
          : [],
      })
    } else if (path === '/api/proxy/status') {
      const id = new URL(route.request().url()).searchParams.get('printer_id')
      const printer = printers.find((p) => p.id === id)
      await route.fulfill({
        json: {
          printer_id: id,
          needs_review: [],
          queues: [],
          connected: true,
          printing: printer?.printing ? { id: 'printing' } : null,
          automatic_printing_held: state.held,
          no_automatic_printing: printer?.no_automatic_printing,
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
  await ready(page, 'lock')
  await press(page, 'lock')
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
  await ready(page, 'lock')
  state.online = true
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await ready(page, 'back')
})

async function refreshStation(page: Page) {
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
}

test('printer quick controls share handlers, recover after failure and reject stale or duplicate actions', async ({
  page,
}) => {
  const state = await fixture(page)
  state.locked = false
  state.permissions.push(
    'printer.hold_resume',
    'printer.start_own',
    'printer.stop_after_label',
    'printer.emergency_stop',
  )
  state.printers[0].can_stop = true
  state.queue = true
  await refreshStation(page)
  await ready(page, 'printer.0')
  await press(page, 'printer.0')
  await ready(page, 'hold')
  expect((await current(page))!.buttons[13]?.id).toBe('lock')
  expect((await current(page))!.buttons.length).toBe(14)
  await press(page, 'print')
  expect(state.prints).toEqual([])

  state.failHold = true
  await press(page, 'hold')
  await expect(page.getByRole('alert')).toContainText('Hold failed')
  await ready(page, 'hold')
  let releaseHold!: () => void
  state.holdWait = new Promise<void>((resolve) => {
    releaseHold = resolve
  })
  const beforeHold = (await current(page))!.id
  await press(page, 'hold')
  await expect.poll(() => state.holds.length).toBe(2)
  await press(page, 'hold', beforeHold)
  await press(page, 'lock', beforeHold)
  expect(state.locked).toBe(false)
  expect(state.holds.length).toBe(2)
  releaseHold()
  await ready(page, 'hold')
  await expect
    .poll(async () => (await current(page))!.buttons.find((b) => b?.id === 'hold')?.label)
    .toBe('Resume printer')
  await press(page, 'hold', beforeHold)
  expect(state.holds.length).toBe(2)
  await press(page, 'hold')
  await expect.poll(() => state.held).toBe(false)
  await ready(page, 'stop')
  await press(page, 'stop')
  await ready(page, 'emergency')
  await press(page, 'emergency')
  await expect.poll(() => state.stops.map((s) => s.mode)).toEqual(['after_current', 'emergency'])
  expect(state.stops.every((s) => s.printer === '/api/station/printers/local-0/stop')).toBe(true)

  await page.locator('.station-queue-group > .station-job input[type=checkbox]').check()
  await ready(page, 'print')
  await test.info().attach('foxoe-controls.json', {
    body: JSON.stringify(await current(page)),
    contentType: 'application/json',
  })
  state.shortage = true
  await press(page, 'print')
  await expect(page.getByRole('dialog')).toContainText('Not enough tape')
  await press(page, 'print')
  expect(state.prints).toEqual([{ copy_ids: ['copy-1'] }])
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click()
  await ready(page, 'print')
  state.shortage = false
  const oldPrint = (await current(page))!.id
  await press(page, 'print')
  await press(page, 'print', oldPrint)
  await expect.poll(() => state.prints.length).toBe(2)
  await ready(page, 'back')
  await press(page, 'back')
  await ready(page, 'printer.0')
  await press(page, 'print', oldPrint)
  expect(state.prints.length).toBe(2)
  await press(page, 'lock')
  await ready(page, 'unlock')
  expect(state.locked).toBe(true)
})

test('old print presses cannot target a replacement selection or survive permission removal', async ({
  page,
}) => {
  const state = await fixture(page)
  state.locked = false
  state.permissions.push('printer.start_own')
  state.queue = true
  await refreshStation(page)
  await ready(page, 'printer.0')
  await press(page, 'printer.0')
  const selection = page.locator('.station-queue-group > .station-job input[type=checkbox]')
  await selection.check()
  await ready(page, 'print')
  const old = (await current(page))!.id
  state.queueCopy = 'copy-2'
  await refreshStation(page)
  await expect(selection).not.toBeChecked()
  await selection.check()
  await ready(page, 'print')
  await press(page, 'print', old)
  expect(state.prints).toEqual([])
  const replacement = (await current(page))!.id
  state.permissions = ['printer.view']
  await refreshStation(page)
  await expect
    .poll(async () => (await current(page))!.buttons.some((b) => b?.id === 'print'))
    .toBe(false)
  await press(page, 'print', replacement)
  expect(state.prints).toEqual([])
  state.permissions.push('printer.start_own')
  await refreshStation(page)
  await ready(page, 'print')
  await press(page, 'print')
  await expect.poll(() => state.prints).toEqual([{ copy_ids: ['copy-2'] }])
})

test('quick controls enforce permissions, printer state and local offline stop policy', async ({
  page,
}) => {
  const state = await fixture(page)
  state.locked = false
  await refreshStation(page)
  await ready(page, 'printer.0')
  await press(page, 'printer.0')
  await ready(page, 'back')
  expect((await current(page))!.buttons.filter(Boolean).map((b) => b!.id)).toEqual(['back', 'lock'])
  for (const id of ['hold', 'print', 'stop', 'emergency']) await press(page, id)
  expect([state.holds, state.prints, state.stops]).toEqual([[], [], []])

  state.permissions.push(
    'printer.hold_resume',
    'printer.start_any',
    'printer.stop_after_label',
    'printer.emergency_stop',
  )
  state.printers[0].can_stop = true
  state.printers[0].no_automatic_printing = true
  state.printers[0].printing = true
  state.queue = true
  await refreshStation(page)
  await ready(page, 'emergency')
  expect((await current(page))!.buttons.some((b) => b?.id === 'hold')).toBe(false)
  await page.locator('.station-queue-group > .station-job input[type=checkbox]').check()
  expect((await current(page))!.buttons.find((b) => b?.id === 'print')?.disabled).toBe(true)
  state.printers[0].printing = false
  state.printers[0].blocked = true
  await refreshStation(page)
  await expect(page.locator('.station-printer').first()).toContainText('Investigation required')
  await press(page, 'print')
  expect(state.prints).toEqual([])
  state.printers[0].blocked = false
  await refreshStation(page)
  await ready(page, 'print')
  const old = (await current(page))!.id
  state.online = false
  state.stopPolicy.offline_emergency = true
  await refreshStation(page)
  await expect
    .poll(async () => (await current(page))!.buttons.find((b) => b?.id === 'print')?.disabled)
    .toBe(true)
  await press(page, 'print', old)
  await press(page, 'stop', old)
  expect(state.prints).toEqual([])
  expect(state.stops).toEqual([])
  await ready(page, 'emergency')
  await press(page, 'emergency')
  await expect.poll(() => state.stops.length).toBe(1)
  await ready(page, 'lock')
  await press(page, 'lock')
  await expect.poll(() => state.locked).toBe(true)
})
