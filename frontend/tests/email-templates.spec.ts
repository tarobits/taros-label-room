import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

// Render the actual Python template; the browser only substitutes the MIME image URL.
const emails: Record<string, string> = JSON.parse(
  execFileSync(
    resolve('../.venv/bin/python'),
    [
      '-c',
      `
import base64, json
from app.i18n import render
from app.notifications.email_templates import ILLUSTRATION_CID, illustration, notification_illustration, render_email
emails = {}
for language in ('en', 'de'):
    for kind in ('verify', 'reset', 'changed', 'notification', 'warning', 'long'):
        kwargs = {}
        artwork = 'default'
        if kind in ('verify', 'reset'):
            artwork = 'check' if kind == 'verify' else 'password'
            heading = render(f'recoveryMessages.{kind}Action', language)
            body = render(f'emailMessages.{kind}Body', language)
            kwargs = dict(action_url='https://labels.example.invalid/#/account?token=test',
                action_label=heading, note=render('emailMessages.linkExpiry', language),
                footer=render('emailMessages.ignoreRequest', language))
        elif kind == 'changed':
            artwork = 'password'
            heading = render('emailMessages.passwordChanged', language)
            body = render('recoveryMessages.changedBody', language)
        else:
            artwork = notification_illustration('printer_blocked' if kind == 'warning' else 'general')
            heading = 'Printer' * 40 if kind == 'long' else 'Workshop printer'
            body = '<script>literal user content</script>' + 'Label' * 100
            kwargs = dict(action_url='https://labels.example.invalid/#/activity',
                action_label=render('emailMessages.openApp', language))
        html = render_email(heading, body, language, **kwargs)
        emails[f'{kind}-{language}'] = html.replace(f'cid:{ILLUSTRATION_CID}',
            'data:image/png;base64,' + base64.b64encode(illustration(artwork)).decode())
print(json.dumps(emails))
`,
    ],
    { cwd: resolve('..'), encoding: 'utf8', maxBuffer: 2 * 1024 * 1024 },
  ),
)

function luminance(color: string) {
  const components = color
    .match(/[\d.]+/g)!
    .slice(0, 3)
    .map(Number)
    .map((value) => {
      const channel = value / 255
      return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
    })
  return components[0]! * 0.2126 + components[1]! * 0.7152 + components[2]! * 0.0722
}

for (const width of [320, 390, 600, 900]) {
  test(`email themes remain readable at ${width}px with translated and hostile content`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 950 })
    for (const [name, html] of Object.entries(emails)) {
      await page.setContent(html)
      // Changing the appearance of an already-open email must update both text and surfaces.
      for (const colorScheme of ['light', 'dark'] as const) {
        await page.emulateMedia({ colorScheme })
        const result = await page.evaluate(() => {
          const paper = getComputedStyle(document.querySelector('.paper')!).backgroundColor
          const samples = [...document.querySelectorAll('h1,p,a,.brand,.footer')].map((element) => {
            const style = getComputedStyle(element)
            return [style.color, element.tagName === 'A' ? style.backgroundColor : paper]
          })
          return { paper, samples, overflow: document.documentElement.scrollWidth > innerWidth }
        })
        expect(result.overflow, `${name} ${colorScheme}`).toBe(false)
        expect(result.paper).toBe(colorScheme === 'dark' ? 'rgb(36, 38, 45)' : 'rgb(255, 255, 255)')
        for (const [foreground, background] of result.samples) {
          const a = luminance(foreground!)
          const b = luminance(background!)
          expect((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)).toBeGreaterThanOrEqual(4.5)
        }
      }
      await expect(page.locator('script')).toHaveCount(0)
      await expect(page.locator('img')).toHaveCount(1)
    }
  })
}

test('email actions remain usable when a client strips styles and images', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 950 })
  await page.emulateMedia({ colorScheme: 'light' })
  await page.setContent(emails['verify-de']!)
  await page
    .locator('style,img')
    .evaluateAll((elements) => elements.forEach((element) => element.remove()))
  const action = page.getByRole('link')
  await expect(action).toHaveText('Bestätige deine E-Mail-Adresse')
  await expect(action).toHaveAttribute(
    'href',
    'https://labels.example.invalid/#/account?token=test',
  )
  await action.focus()
  await expect(action).toBeFocused()
  const box = (await action.boundingBox())!
  expect(box.height).toBeGreaterThanOrEqual(44)
  expect(box.x).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(320)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
