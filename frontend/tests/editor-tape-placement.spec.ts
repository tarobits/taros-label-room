import { test, expect } from './fixtures'
import { chooseTapeProfile } from './tape-profile-picker-helpers'

for (const initialWidth of [12, 24]) {
  test(`changing ${initialWidth} mm tape preserves printable-area placement and undo`, async ({
    page,
    request,
  }) => {
    const profiles = []
    for (const width of [initialWidth, initialWidth === 12 ? 24 : 12, initialWidth]) {
      const response = await request.post('/api/tape-profiles', {
        data: { name: `Placement ${width} ${profiles.length}`, width_mm: width },
      })
      expect(response.ok()).toBeTruthy()
      profiles.push(await response.json())
    }
    // Production printable bands are quantized to printer rows (85 and 170 rows).
    const bands = {
      12: { top: (7 * 12) / 85, height: (70 * 12) / 85 },
      24: { top: (21 * 24) / 170, height: (128 * 24) / 170 },
    }
    const before = bands[initialWidth as 12 | 24]
    const after = bands[initialWidth === 12 ? 24 : 12]
    const positions = [0, 0.25, 0.5, 1]
    const elements = positions.map((position, index) => ({
      id: `box-${index}`,
      kind: 'rectangle',
      x: 4 + index * 10,
      y: before.top + position * (before.height - 4),
      width: 4,
      height: 4,
      rotation: index === 1 ? 90 : 0,
    }))
    const response = await request.post('/api/templates', {
      data: {
        name: 'Tape placement',
        document: {
          width_mm: initialWidth,
          length_mm: 60,
          preview_tape: {
            id: profiles[0].id,
            name: profiles[0].name,
            foreground: profiles[0].foreground,
            background: profiles[0].background,
          },
          elements,
        },
      },
    })
    expect(response.ok(), await response.text()).toBeTruthy()
    const template = await response.json()
    await page.goto(`/#/templates/${template.id}/edit`)
    const boundaries = page.locator('.object-boundary')
    await expect(boundaries).toHaveCount(elements.length)
    const check = async (band: typeof before) => {
      for (const [index, position] of positions.entries()) {
        await expect
          .poll(async () => Number(await boundaries.nth(index).getAttribute('y')))
          .toBeCloseTo(band.top + position * (band.height - 4), 6)
        await expect(boundaries.nth(index)).toHaveAttribute('x', String(elements[index]!.x))
        await expect(boundaries.nth(index)).toHaveAttribute('height', '4')
      }
    }
    await check(before)
    await chooseTapeProfile(page, profiles[1].id)
    await check(after)
    await page.getByRole('button', { name: 'Undo', exact: true }).click()
    await check(before)
    await page.getByRole('button', { name: 'Redo', exact: true }).click()
    await check(after)
    await chooseTapeProfile(page, profiles[0].id)
    await check(before)
    await chooseTapeProfile(page, profiles[2].id)
    await check(before)
    await page.getByRole('button', { name: 'Save template', exact: true }).click()
    await expect(page.getByText('Revision 2', { exact: true })).toBeVisible()
    await page.reload()
    await check(before)
    await page.locator('.editor-board').screenshot({
      path: `test-results/tape-placement-${initialWidth}.png`,
    })
  })
}

test('tape changes preserve oversized geometry and explicit layout constraints', async ({
  page,
  request,
}) => {
  // Exercise the fallback margin too, on printers without per-width printable bands.
  await page.route(/\/api\/(printers|destinations)$/, async (route) => {
    const response = await route.fetch()
    const printers = await response.json()
    for (const printer of printers) printer.capabilities.printable_widths_mm = null
    await route.fulfill({ json: printers })
  })
  const profiles = []
  for (const width of [12, 24]) {
    const response = await request.post('/api/tape-profiles', {
      data: { name: `Constrained ${width}`, width_mm: width },
    })
    expect(response.ok()).toBeTruthy()
    profiles.push(await response.json())
  }
  const response = await request.post('/api/templates', {
    data: {
      name: 'Constrained placement',
      document: {
        version: 2,
        width_mm: 12,
        length_mm: 60,
        out_of_bounds: 'clip',
        preview_tape: {
          id: profiles[0].id,
          name: profiles[0].name,
          foreground: '#000000',
          background: '#ffffff',
        },
        elements: [
          { id: 'full', kind: 'rectangle', x: 4, y: 1, width: 4, height: 10 },
          { id: 'large', kind: 'rectangle', x: 12, y: -1, width: 4, height: 14 },
          { id: 'bound', kind: 'rectangle', x: 20, y: 3, width: 4, height: 4 },
          {
            id: 'follower',
            kind: 'rectangle',
            x: 28,
            y: 2,
            width: 4,
            height: 2,
            distance_holder: { element_id: 'bound', axis: 'vertical', gap_mm: 1 },
          },
          { id: 'rotated', kind: 'rectangle', x: 36, y: 3, width: 6, height: 2, rotation: 90 },
        ].map((element) => ({ ...element, logic_name: element.id })),
        logic: {
          version: 1,
          variables: {},
          bindings: [{ target: 'bound', property: 'y', expression: { kind: 'literal', value: 3 } }],
        },
      },
    },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  const template = await response.json()
  await page.goto(`/#/templates/${template.id}/edit`)
  await expect(page.getByRole('button', { name: 'Save template', exact: true })).toBeEnabled()
  await chooseTapeProfile(page, profiles[1].id)
  await page.getByRole('button', { name: 'Save template', exact: true }).click()
  await expect(page.getByText('Revision 2', { exact: true })).toBeVisible()
  const saved = await (await request.get(`/api/templates/${template.id}`)).json()
  expect(saved.document.elements.map((element: { y: number }) => element.y)).toEqual([
    7, 5, 3, 2, 3,
  ])
  for (const [index, element] of saved.document.elements.entries()) {
    expect(element.width).toBe(template.document.elements[index].width)
    expect(element.height).toBe(template.document.elements[index].height)
  }
  expect(saved.document.logic).toEqual(template.document.logic)
  expect(saved.document.elements[3].distance_holder).toEqual(
    template.document.elements[3].distance_holder,
  )
  // The full-height case has no free travel, but remains finite and reversible.
  await chooseTapeProfile(page, profiles[0].id)
  await page.getByRole('button', { name: 'Save template', exact: true }).click()
  await expect(page.getByText('Revision 3', { exact: true })).toBeVisible()
  const restored = await (await request.get(`/api/templates/${template.id}`)).json()
  expect(restored.document.elements).toEqual(template.document.elements)
})
