import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const code = ts.transpileModule(
  readFileSync(new URL('../src/companion/foxoeDeck.ts', import.meta.url), 'utf8'),
  {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  },
).outputText

function fixture() {
  const messages = [],
    listeners = new Set(),
    timers = new Map()
  let sequence = 0
  const target = {
    location: { origin: 'https://station.test' },
    document: { hidden: false },
    crypto: { randomUUID: () => `request-${++sequence}` },
    addEventListener: (_, fn) => listeners.add(fn),
    removeEventListener: (_, fn) => listeners.delete(fn),
    postMessage: (message, origin) => {
      assert.equal(origin, target.location.origin)
      messages.push(message)
    },
  }
  const exports = {}
  vm.runInNewContext(code, {
    exports,
    require: () => ({ randomUUID: target.crypto.randomUUID }),
    setTimeout: (fn) => {
      const id = ++sequence
      timers.set(id, fn)
      return id
    },
    clearTimeout: (id) => timers.delete(id),
  })
  const deck = exports.createFoxoeDeck(target)
  const receive = (message, extra = {}) =>
    listeners.forEach((fn) =>
      fn({
        source: target,
        origin: target.location.origin,
        data: { source: 'foxoe:bridge', ...message },
        ...extra,
      }),
    )
  const last = () => messages.at(-1)
  const ack = () => receive({ type: 'ack', request: last().request })
  const press = (id, layout = last().layout.id) => receive({ type: 'action', id, layout })
  const tick = () => {
    const [id, fn] = timers.entries().next().value
    timers.delete(id)
    fn()
  }
  return { deck, target, messages, timers, listeners, receive, last, ack, press, tick }
}
const screen = (context, run, disabled = false) => ({
  context,
  buttons: [{ id: 'confirm', label: 'Unlock', icon: 'check', disabled, run }],
})

test('absent extension is inert; wire layouts contain no callbacks or private context and reserve Home', () => {
  const f = fixture()
  let calls = 0
  f.deck.setScreen({
    context: 'private-session-context',
    buttons: Array.from({ length: 16 }, (_, i) => ({
      id: `key.${i}`,
      label: 'A'.repeat(40),
      icon: 'keypad',
      run: () => calls++,
    })),
  })
  assert.equal(f.messages.length, 1)
  assert.equal(f.timers.size, 0)
  f.receive({ type: 'connection', connected: true })
  assert.equal(f.last().layout.buttons.length, 14)
  assert.equal(f.last().layout.buttons[0].label.length, 32)
  assert.ok(!JSON.stringify(f.messages).includes('private-session-context'))
  assert.equal(f.last().layout.buttons[0].run, undefined)
  f.press('key.0')
  assert.equal(calls, 0)
  f.ack()
  f.press('key.0')
  assert.equal(calls, 1)
  f.press('key.14')
  assert.equal(calls, 1)
  f.deck.dispose()
})

test('screen changes are serialized and old or unacknowledged actions never execute', () => {
  const f = fixture()
  const calls = []
  f.deck.setScreen(screen('alice', () => calls.push('alice')))
  f.receive({ type: 'connection', connected: true })
  const old = f.last()
  f.deck.setScreen(screen('bob', () => calls.push('bob')))
  f.deck.setScreen(screen('carol', () => calls.push('carol')))
  assert.equal(f.messages.length, 2)
  f.receive({ type: 'ack', request: old.request })
  const current = f.last()
  f.press('confirm', old.layout.id)
  f.press('confirm', current.layout.id)
  assert.equal(calls.length, 0)
  f.ack()
  f.press('confirm')
  assert.deepEqual(calls, ['carol'])
  f.receive({ type: 'ack', request: old.request })
  f.press('confirm', old.layout.id)
  assert.equal(calls.length, 1)
  f.deck.dispose()
})

test('disconnect cancels pending work and reconnect gives the screen a fresh identity', () => {
  const f = fixture()
  let calls = 0
  f.deck.setScreen(screen('pin', () => calls++))
  f.receive({ type: 'connection', connected: true })
  const old = f.last()
  f.receive({ type: 'connection', connected: false })
  assert.equal(f.timers.size, 0)
  f.receive({ type: 'ack', request: old.request })
  f.press('confirm', old.layout.id)
  f.receive({ type: 'connection', connected: true })
  assert.notEqual(f.last().layout.id, old.layout.id)
  f.ack()
  f.press('confirm', old.layout.id)
  assert.equal(calls, 0)
  f.press('confirm')
  assert.equal(calls, 1)
  f.deck.dispose()
})

test('timeout retries once and later valid screens recover without leaked timers', () => {
  const f = fixture()
  let calls = 0
  f.deck.setScreen(screen('pin', () => calls++))
  f.receive({ type: 'connection', connected: true })
  f.tick()
  f.tick()
  f.tick()
  assert.equal(f.messages.length, 3)
  assert.equal(f.timers.size, 0)
  f.press('confirm')
  assert.equal(calls, 0)
  f.deck.setScreen(screen('new-pin', () => calls++))
  f.ack()
  f.press('confirm')
  assert.equal(calls, 1)
  f.deck.dispose()
  assert.equal(f.timers.size, 0)
})

test('foreign frames, wrong origins, malformed messages, disabled keys and hidden pages are ignored', () => {
  const f = fixture()
  let calls = 0
  f.deck.setScreen(screen('pin', () => calls++, true))
  f.receive({ type: 'connection', connected: true }, { origin: 'https://other.test' })
  f.receive({ type: 'connection', connected: true }, { source: {} })
  f.receive({ type: 'connection', connected: true }, { data: null })
  assert.equal(f.messages.length, 1)
  f.receive({ type: 'connection', connected: true })
  f.ack()
  f.press('confirm')
  assert.equal(calls, 0)
  f.deck.setScreen(screen('ready', () => calls++))
  f.ack()
  f.target.document.hidden = true
  f.press('confirm')
  assert.equal(calls, 0)
  f.target.document.hidden = false
  f.press('unknown')
  f.press('confirm')
  assert.equal(calls, 1)
  f.deck.dispose()
  assert.equal(f.listeners.size, 0)
  const sent = f.messages.length
  f.receive({ type: 'connection', connected: true })
  f.deck.setScreen(screen('closed', () => calls++))
  assert.equal(f.messages.length, sent)
})
