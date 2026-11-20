import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import { createI18n } from 'vue-i18n'

const read = (path) => readFileSync(new URL(`../../${path}`, import.meta.url), 'utf8')
const license = read('LICENSE')
const identifier = 'PolyForm-Noncommercial-1.0.0'

test('published package metadata agrees on the noncommercial license', () => {
  assert.equal(JSON.parse(read('frontend/package.json')).license, identifier)
  assert.equal(JSON.parse(read('frontend/package-lock.json')).packages[''].license, identifier)
  assert.match(read('pyproject.toml'), new RegExp(`^license = "${identifier}"$`, 'm'))
  assert.match(license, /^# PolyForm Noncommercial License 1\.0\.0\n/)
  assert.doesNotMatch(license, /GNU AFFERO GENERAL PUBLIC LICENSE/)
})

test('license downloads stay localized and independent across component lifetimes', async () => {
  const { descriptor } = parse(read('frontend/src/components/SourceLink.vue'))
  const script = compileScript(descriptor, { id: 'license-test', inlineTemplate: true })
  const code = ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText
  const messages = Object.fromEntries(
    ['en', 'de'].map((locale) => [locale, JSON.parse(read(`lang/${locale}.json`))]),
  )
  const { global } = createI18n({ legacy: false, locale: 'en', messages })
  const active = new Map()
  let serial = 0
  function mount() {
    const cleanup = []
    const exports = {}
    vm.runInNewContext(code, {
      exports,
      Blob,
      URL: {
        createObjectURL: (blob) => {
          const url = `blob:license-${++serial}`
          active.set(url, blob)
          return url
        },
        revokeObjectURL: (url) => assert.equal(active.delete(url), true),
      },
      require: (name) => {
        if (name === 'vue') return { ...vue, onBeforeUnmount: (callback) => cleanup.push(callback) }
        if (name === '../../../LICENSE?raw') return { default: license }
        throw new Error(`Unexpected import: ${name}`)
      },
    })
    const render = exports.default.setup({}, { expose() {} })
    return {
      links: () => render({ $t: global.t }, []).children,
      unmount: () => cleanup.forEach((callback) => callback()),
    }
  }
  const first = mount()
  const second = mount()
  const firstUrl = first.links()[1].props.href
  const secondUrl = second.links()[1].props.href
  assert.notEqual(firstUrl, secondUrl)
  for (const [locale, accessibleName] of [
    ['en', 'Download PolyForm Noncommercial license'],
    ['de', 'PolyForm-Noncommercial-Lizenz herunterladen'],
  ]) {
    global.locale.value = locale
    const [source, download] = second.links()
    assert.equal(source.props.href, 'https://github.com/tarobits/taros-label-room')
    assert.equal(source.props.rel, 'noopener noreferrer')
    assert.equal(download.props.download, `${identifier}.txt`)
    assert.equal(download.props['aria-label'], accessibleName)
    assert.equal(download.children, 'PolyForm Noncommercial')
    assert.equal(download.props.href, secondUrl)
    assert.equal(active.get(secondUrl).type, 'text/plain;charset=utf-8')
    assert.equal(await active.get(secondUrl).text(), license)
  }
  first.unmount()
  assert.equal(active.has(firstUrl), false)
  assert.equal(await active.get(secondUrl).text(), license)
  second.unmount()
  assert.equal(active.size, 0)
  const remounted = mount()
  assert.equal(await active.get(remounted.links()[1].props.href).text(), license)
  remounted.unmount()
  assert.equal(active.size, 0)
})
