import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

for (const role of ['main', 'companion']) {
  for (const locale of ['en', 'de']) {
    test(`${role} offers the current license in ${locale} on desktop and mobile`, async ({
      page,
    }) => {
      const dist = role === 'main' ? 'dist' : 'dist-companion'
      await page.addInitScript((language) => {
        localStorage.setItem('label-room.language', language)
      }, locale)
      await page.route('http://localhost:12345/**', async (route) => {
        const path = new URL(route.request().url()).pathname
        if (path === '/api/health') return route.fulfill({ json: { healthy: true } })
        if (path === '/api/auth/session')
          return route.fulfill({ json: { user: null, csrf_token: null, setup_required: false } })
        if (path === '/api/station')
          return route.fulfill({
            json: {
              name: 'Test station',
              version: '0.20.4',
              locked: true,
              online: true,
              user: null,
              users: [],
              printers: [],
              removed_printers: [],
              stop_policy: {},
            },
          })
        if (path.startsWith('/assets/')) return route.fulfill({ path: resolve(`${dist}${path}`) })
        if (path.startsWith('/fonts/')) return route.fulfill({ path: resolve(`../app${path}`) })
        if (path === '/')
          return route.fulfill({
            path: resolve(dist, role === 'main' ? 'index.html' : 'companion.html'),
            contentType: 'text/html',
          })
        return route.abort()
      })
      const errors: string[] = []
      page.on('pageerror', (error) => errors.push(error.message))
      await page.goto('http://localhost:12345/')
      const link = page.getByRole('link', {
        name:
          locale === 'en'
            ? 'Download PolyForm Noncommercial license'
            : 'PolyForm-Noncommercial-Lizenz herunterladen',
      })
      for (const width of [1280, 390]) {
        await page.setViewportSize({ width, height: 844 })
        await expect(link).toBeVisible()
        const source = page.locator('.source-links a').first()
        await link.scrollIntoViewIfNeeded()
        const [sourceBox, licenseBox] = await Promise.all([
          source.boundingBox(),
          link.boundingBox(),
        ])
        expect(sourceBox).not.toBeNull()
        expect(licenseBox).not.toBeNull()
        expect(licenseBox!.x).toBeGreaterThanOrEqual(0)
        expect(licenseBox!.x + licenseBox!.width).toBeLessThanOrEqual(width)
        expect(
          licenseBox!.x >= sourceBox!.x + sourceBox!.width + 8 ||
            licenseBox!.y >= sourceBox!.y + sourceBox!.height + 8,
        ).toBe(true)
        const downloaded = page.waitForEvent('download')
        await link.click()
        const download = await downloaded
        expect(download.suggestedFilename()).toBe('PolyForm-Noncommercial-1.0.0.txt')
        expect(await download.failure()).toBeNull()
        expect(readFileSync((await download.path())!, 'utf8')).toBe(
          readFileSync('../LICENSE', 'utf8'),
        )
        await page.screenshot({ path: `test-results/license-${role}-${locale}-${width}.png` })
      }
      expect(errors).toEqual([])
    })
  }
}
